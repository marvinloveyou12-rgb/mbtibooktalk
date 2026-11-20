from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import xml.etree.ElementTree as ET
import asyncio
import json
import os
import re
import time as _time
import threading as _threading
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

from .database import init_db, count_items, seed_from_json as _seed, get_conn as _get_conn
from .database import count_items as _count_items
from .crawler import crawl_state, run_crawl
from .search_engine import hybrid_search, build_index, invalidate_cache
from .search_engine import build_index as _build_index
from .claude_chat import get_librarian_response
from . import community_store as _cs

app = FastAPI(title="도서 큐레이션 API")

# ── 실시간 접속자 추적 ────────────────────────────────────────
_presence: dict[str, float] = {}
_PRESENCE_TTL = 90  # 90초 이상 ping 없으면 오프라인 처리

# ── CORS 화이트리스트 ────────────────────────────────────────
_cors_default = (
    "http://localhost:8000,http://127.0.0.1:8000,"
    "https://dongle0516-mbtibooktalk.hf.space,"
    "https://marvinloveyou12-rgb.github.io"
)
_cors_env = os.getenv("CORS_ALLOW_ORIGINS", _cors_default).strip()
_cors_origins = ["*"] if _cors_env == "*" else [
    o.strip() for o in _cors_env.split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── 앱 시작 시 DB 초기화 + 인덱스 복구 ──────────────────────
init_db()

def _startup():
    _seed()
    if _count_items() > 0:
        # word-level TF-IDF로 교체 후 첫 기동 시 기존 char_wb 캐시 자동 무효화
        from pathlib import Path as _P
        import pickle as _pk
        cache = _P(__file__).parent / "tfidf_cache.pkl"
        if cache.exists():
            try:
                with open(cache, "rb") as _f:
                    _meta = _pk.load(_f)
                # vectorizer analyzer가 char_wb이면 낡은 캐시 → 삭제 후 재빌드
                _old_vec = _meta.get("vectorizer")
                if _old_vec and getattr(_old_vec, "analyzer", None) == "char_wb":
                    cache.unlink()
                    print("[startup] char_wb 캐시 감지 → 삭제 후 word-level 재빌드")
            except Exception:
                cache.unlink(missing_ok=True)
        _build_index()
        # 시맨틱 인덱스(가용 시) — 백그라운드 빌드
        try:
            from .semantic_engine import build_index as _sem_build, is_available
            if is_available():
                _sem_build()
        except Exception as e:
            print(f"[startup] semantic index 빌드 스킵: {e}")

_threading.Thread(target=_startup, daemon=True).start()
_cs.start()


@app.on_event("shutdown")
def _flush_community():
    _cs.flush()


# ── YES24 Open API ───────────────────────────────────────────
_YES24_KEY = os.getenv("YES24_API_KEY", "")
_YES24_BASE = "https://apis.yes24.com/v1/goods"
_GOOGLE_BOOKS_URL  = "https://www.googleapis.com/books/v1/volumes"
_TAG_RE = re.compile(r"<[^>]+>")


def _yes24_book(b: dict) -> dict:
    cd = b.get("contentDetail") or {}
    desc = cd.get("bookIntroduction") or cd.get("bookSummary") or ""
    return {
        "title":     b.get("title", ""),
        "author":    b.get("author", ""),
        "publisher": b.get("publisher", ""),
        "isbn":      b.get("isbn13") or "",
        "cover":     b.get("cover") or "",
        "link":      b.get("link", ""),
        "desc":      _TAG_RE.sub("", desc).strip(),
        "source":    "yes24",
    }


async def _yes24_get(path: str, params: dict) -> list[dict]:
    if not _YES24_KEY:
        print("[yes24] YES24_API_KEY 미설정")
        return []
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{_YES24_BASE}/{path}", params=params,
                                 headers={"X-Api-Key": _YES24_KEY})
        if r.status_code != 200:
            print(f"[yes24] {path} HTTP {r.status_code}: {r.text[:200]}")
            return []
        return [_yes24_book(b) for b in (r.json().get("data") or {}).get("items") or []]
    except Exception as e:
        print(f"[yes24] {path} error: {e}")
        return []


async def _yes24_search(q: str, page: int = 1, page_size: int = 50) -> list[dict]:
    return await _yes24_get("itemList", {
        "query": q, "category": "BOOK", "sort": "RELATION",
        "page": page, "pageSize": page_size,
    })


# 표지·소개 조회 결과 캐시 (일일 호출 한도 절약)
_book_info_cache: dict[str, dict] = {}


async def _yes24_info(q: str, isbn: str) -> dict:
    key = isbn or q
    if key in _book_info_cache:
        return _book_info_cache[key]
    items = []
    if isbn:
        items = await _yes24_get("itemDetail", {"searchType": "ISBN13", "query": isbn})
    if not items and q:
        # 세트 상품은 ISBN이 비어 있어 단권보다 앞에 나오는 경우가 많으므로 단권을 우선한다
        found = await _yes24_search(q, 1, 10)
        items = [b for b in found if b["isbn"]] or found
    b = items[0] if items else {}
    info = {"cover": b.get("cover", ""), "desc": b.get("desc", ""), "link": b.get("link", "")}
    if len(_book_info_cache) > 5000:
        _book_info_cache.clear()
    _book_info_cache[key] = info
    return info


async def _google_books_search(q: str, max_results: int = 40) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(_GOOGLE_BOOKS_URL, params={
                "q": q, "langRestrict": "ko", "maxResults": min(max_results, 40),
            })
        items = r.json().get("items", [])
        return [{
            "title":     v.get("title", ""),
            "author":    ", ".join(v.get("authors", [])),
            "publisher": v.get("publisher", ""),
            "isbn":      next((x["identifier"] for x in v.get("industryIdentifiers", []) if x["type"] == "ISBN_13"), ""),
            "cover":     (v.get("imageLinks", {}).get("thumbnail") or "").replace("http://", "https://"),
            "link":      v.get("infoLink", ""),
            "source":    "google",
        } for i in items for v in [i.get("volumeInfo", {})]]
    except Exception as e:
        print(f"[book-search] google error: {e}")
        return []


# ── 국립중앙도서관 외부 API ──────────────────────────────────
API_KEY = os.getenv("NL_API_KEY")
SEARCH_URL = "https://www.nl.go.kr/NL/search/openApi/search.do"
SASEO_URL  = "https://www.nl.go.kr/NL/search/openApi/saseoApi.do"
NL_QA_VIEW_URL = "https://www.nl.go.kr/NL/contents/N30501000000.do?schM=view&ackRecKey={rec_key}"

# Claude 답변 본문에서 『도서명』을 추출해 도서 검색 API와 연동하기 위한 정규식
_BOOK_TITLE_RE = re.compile(r"『([^』]{1,60})』")


# ── KDC 분류 매핑 ───────────────────────────────────────────
KDC_MAP = {
    "0": {"name": "총류",   "color": "#6C757D",
          "keywords": ["도서관","백과사전","정보학","컴퓨터","프로그래밍","인공지능","데이터","소프트웨어","IT","딥러닝"]},
    "1": {"name": "철학",   "color": "#8B5CF6",
          "keywords": ["철학","윤리","논리","심리","심리학","인식론","형이상학","명상","마음","의식","행복"]},
    "2": {"name": "종교",   "color": "#F59E0B",
          "keywords": ["종교","불교","기독교","이슬람","성경","불경","신앙","신학","사찰","기도"]},
    "3": {"name": "사회과학","color": "#3B82F6",
          "keywords": ["사회","경제","정치","법","교육","경영","복지","행정","무역","통계","금융","부동산","토지","세금"]},
    "4": {"name": "자연과학","color": "#10B981",
          "keywords": ["과학","수학","물리","화학","생물","천문","지구","환경","우주","생태","자연"]},
    "5": {"name": "기술과학","color": "#EF4444",
          "keywords": ["기술","공학","의학","농업","건축","제조","전기","기계","의료","건강","요리","음식"]},
    "6": {"name": "예술",   "color": "#EC4899",
          "keywords": ["예술","음악","미술","영화","사진","디자인","스포츠","게임","그림","조각","춤","패션"]},
    "7": {"name": "언어",   "color": "#14B8A6",
          "keywords": ["언어","한국어","영어","일본어","중국어","문법","번역","어학","외국어"]},
    "8": {"name": "문학",   "color": "#F97316",
          "keywords": ["문학","소설","시","수필","희곡","동화","만화","웹툰","픽션","이야기","단편","장편"]},
    "9": {"name": "역사",   "color": "#84CC16",
          "keywords": ["역사","지리","한국사","세계사","전쟁","문화","문명","고고학","고대","근대","조선","일제"]},
}

DR_CODE_MAP = {
    "0": 5, "1": 6, "2": 6, "3": 5,
    "4": 4, "5": 4, "6": 6, "7": 6,
    "8": 11, "9": 6,
}

DR_CODE_NAME = {4: "자연과학", 5: "사회과학", 6: "인문과학", 11: "문학"}


def classify_keyword(keyword: str) -> Tuple[Optional[str], dict]:
    scores = {kdc: 0 for kdc in KDC_MAP}
    for kdc, info in KDC_MAP.items():
        for kw in info["keywords"]:
            if keyword == kw:
                scores[kdc] += 10
            elif kw in keyword:
                scores[kdc] += 3
            elif keyword in kw:
                scores[kdc] += 2
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None, {"name": "미분류", "color": "#9CA3AF"}
    return best, KDC_MAP[best]


def strip_html(text: str) -> str:
    import html as _html
    text = _html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_search_xml(xml_text: str, keyword: str) -> Tuple[list, int]:
    try:
        root = ET.fromstring(xml_text)
        total = int(root.findtext(".//total") or 0)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title_info") or ""
            items.append({
                "id":          item.findtext("id"),
                "title":       title,
                "author":      item.findtext("author_info") or "-",
                "publisher":   item.findtext("pub_info") or "-",
                "year":        item.findtext("pub_year_info") or "-",
                "call_no":     item.findtext("call_no") or "-",
                "kdc_name":    item.findtext("kdc_name_1s") or "-",
                "kdc_code":    item.findtext("kdc_code_1s") or "",
                "type":        item.findtext("type_name") or "-",
                "detail_link": "https://www.nl.go.kr" + (item.findtext("detail_link") or ""),
            })
        return items, total
    except Exception:
        return [], 0


def parse_saseo_xml(xml_text: str) -> list:
    try:
        root = ET.fromstring(xml_text)
        items = []
        for item in root.findall(".//item"):
            image = item.findtext("mokchFilePath") or item.findtext("recomfilepath") or ""
            image = image.strip().replace("http://", "https://")
            content = strip_html(item.findtext("recomcontens") or "")
            items.append({
                "recom_no":  item.findtext("recomNo"),
                "title":     item.findtext("recomtitle") or "-",
                "author":    item.findtext("recomauthor") or "-",
                "publisher": item.findtext("recompublisher") or "-",
                "call_no":   item.findtext("recomcallno") or "-",
                "image":     image.strip(),
                "content":   content,
                "isbn":      item.findtext("recomisbn") or "",
                "category":  item.findtext("drCodeName") or "",
            })
        return items
    except Exception:
        return []


# ── 사서 Q&A 엔드포인트 (RAG) ───────────────────────────────
class AskRequest(BaseModel):
    query: str
    top_k: int = 8


async def _enrich_with_books(answer_text: str, max_books: int = 5) -> list[dict]:
    """Claude 답변에서 『…』로 표기된 도서명을 추출해 국중도 검색 API로 메타데이터 보강."""
    if not API_KEY or not answer_text:
        return []
    titles = []
    seen = set()
    for t in _BOOK_TITLE_RE.findall(answer_text):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            titles.append(t)
        if len(titles) >= max_books:
            break
    if not titles:
        return []

    async with httpx.AsyncClient(timeout=4.0) as client:
        async def _one(title: str) -> Optional[dict]:
            try:
                r = await client.get(SEARCH_URL, params={
                    "key": API_KEY, "kwd": title,
                    "pageNum": 1, "pageSize": 1,
                    "type_name": "도서", "sort": "S",
                })
                items, _ = parse_search_xml(r.text, title)
                if items:
                    items[0]["matched_title"] = title
                    return items[0]
            except Exception:
                return None
            return None
        results = await asyncio.gather(*[_one(t) for t in titles])
    return [r for r in results if r]


@app.post("/api/librarian/ask")
async def librarian_ask(req: AskRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="질문을 입력해주세요.")

    # 1) 하이브리드 검색 (벡터 7 : 키워드 3, RRF 정규화)
    results = await asyncio.get_event_loop().run_in_executor(
        None, hybrid_search, req.query, req.top_k
    )

    # 2) Claude 사서 응답 생성 (RAG)
    chat = await asyncio.get_event_loop().run_in_executor(
        None, get_librarian_response, req.query, results
    )

    # 3) 응답 본문 『도서명』 → 국중도 도서검색 API 메타데이터 보강
    book_cards = await _enrich_with_books(chat["response"])

    vec_engine = results[0].get("vector_engine", "tfidf") if results else "tfidf"

    return {
        "query": req.query,
        "response": chat["response"],
        "used_claude": chat["used_claude"],
        "source_count": chat["source_count"],
        "cited_indices": chat.get("cited_indices", []),
        "response_mode": chat.get("response_mode", "rag"),
        "response_tier": chat.get("response_tier", "high"),
        "top_score": chat.get("top_score", 0.0),
        "norm_score": chat.get("norm_score", 0.0),
        "data4lib_count": chat.get("data4lib_count", 0),
        "weights": {
            "vector": float(os.getenv("HYBRID_VECTOR_WEIGHT", "0.7")),
            "keyword": float(os.getenv("HYBRID_KEYWORD_WEIGHT", "0.3")),
        },
        "vector_engine": vec_engine,
        "sources": [{
            "rec_key": r.get("rec_key"),
            "question": r.get("question", "")[:150],
            "answer": r.get("answer", "")[:400],
            "subject": r.get("subject", ""),
            "answer_date": r.get("answer_date", ""),
            "answer_lib": r.get("answer_lib", ""),
            "search_type": r.get("search_type", ""),
            "score": round(r.get("combined_score", r.get("score", 0)), 4),
            "kw_rrf": round(r.get("kw_rrf", 0), 5),
            "vec_rrf": round(r.get("vec_rrf", 0), 5),
            "source_url": NL_QA_VIEW_URL.format(rec_key=r.get("rec_key", "")),
        } for r in results],
        "book_cards": book_cards,
    }


class FeedbackRequest(BaseModel):
    query: str
    response_tier: str
    top_score: float = 0.0
    norm_score: float = 0.0
    helpful: bool


@app.post("/api/librarian/feedback")
async def librarian_feedback(req: FeedbackRequest):
    """이용자 피드백 저장 — 임계값 자동 튜닝 데이터 수집용."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (query, response_tier, top_score, norm_score, helpful) "
            "VALUES (?, ?, ?, ?, ?)",
            (req.query[:500], req.response_tier, req.top_score, req.norm_score, int(req.helpful)),
        )
        conn.commit()
    return {"status": "ok"}


@app.post("/api/crawl/start")
async def crawl_start(force: bool = Query(False, description="True면 기존 답변까지 전수 재수집")):
    if crawl_state["running"]:
        return {"status": "already_running", **crawl_state}

    def _rebuild():
        invalidate_cache()
        build_index()
        try:
            from .semantic_engine import build_index as _sem_build, invalidate_cache as _sem_inv
            _sem_inv()
            _sem_build()
        except Exception as e:
            print(f"[crawl] semantic index 재빌드 스킵: {e}")

    asyncio.create_task(run_crawl(rebuild_index_fn=_rebuild, force=force))
    return {"status": "started", "force": force, **crawl_state}


@app.post("/api/presence/ping")
async def presence_ping(sid: str = Query(...)):
    now = _time.time()
    _presence[sid] = now
    cutoff = now - _PRESENCE_TTL
    stale = [k for k, v in _presence.items() if v < cutoff]
    for k in stale:
        del _presence[k]
    return {"online": len(_presence)}


@app.get("/api/presence/count")
async def presence_count():
    cutoff = _time.time() - _PRESENCE_TTL
    return {"online": sum(1 for v in _presence.values() if v >= cutoff)}


@app.get("/api/img-proxy")
async def img_proxy(url: str = Query(...)):
    """HTTP 이미지를 HTTPS 환경에서 사용하기 위한 프록시."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url)
        ct = r.headers.get("content-type", "image/jpeg")
        return Response(content=r.content, media_type=ct)
    except Exception:
        raise HTTPException(status_code=404, detail="image not found")


@app.get("/api/crawl/status")
async def crawl_status():
    return {"db_count": count_items(), **crawl_state}


@app.get("/api/librarian/stats")
async def librarian_stats():
    return {
        "db_count": count_items(),
        "crawl_stage": crawl_state["stage"],
        "crawl_running": crawl_state["running"],
    }


# ── 도서 분류 + 검색 결과 엔드포인트 ─────────────────────────
@app.get("/api/analyze")
async def analyze(
    keyword: str = Query(..., description="검색 키워드"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=5, le=50),
):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API 키가 설정되지 않았습니다.")

    kdc_code, kdc_info = classify_keyword(keyword)
    dr_code = DR_CODE_MAP.get(kdc_code, 11) if kdc_code else 11

    async with httpx.AsyncClient(timeout=15.0) as client:
        search_resp = await client.get(SEARCH_URL, params={
            "key":       API_KEY,
            "kwd":       keyword,
            "pageNum":   page,
            "pageSize":  page_size,
            "type_name": "도서",
            "sort":      "S",
        })

    search_books, total = parse_search_xml(search_resp.text, keyword)

    return {
        "keyword": keyword,
        "kdc": {
            "code":    kdc_code,
            "name":    kdc_info.get("name", "미분류"),
            "color":   kdc_info.get("color", "#9CA3AF"),
        },
        "pagination": {
            "page":        page,
            "page_size":   page_size,
            "total":       total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
        },
        "search_results": search_books,
    }


# ── 도서 검색 프록시 (YES24 + Google Books) ──────────────────
@app.get("/api/book-search")
async def book_search_proxy(
    q: str = Query(..., description="검색어"),
    filter_type: str = Query("all", description="all|title|author|publisher"),
    max_results: int = Query(30, ge=1, le=100),
):
    field_map = {"title": "title", "author": "author", "publisher": "publisher"}

    if filter_type in field_map:
        google_q = {"title": f"intitle:{q}", "author": f"inauthor:{q}", "publisher": f"inpublisher:{q}"}[filter_type]
    else:
        google_q = q

    results = await asyncio.gather(
        _yes24_search(q, 1, 50), _yes24_search(q, 2, 50), _google_books_search(google_q),
    )
    items = [item for sub in results for item in sub]

    # YES24는 필드별 검색이 없으므로 결과에서 해당 필드로 거른다
    if filter_type in field_map:
        f, needle = field_map[filter_type], q.replace(" ", "").lower()
        items = [b for b in items if needle in (b[f] or "").replace(" ", "").lower()]

    seen, books = set(), []
    for b in items:
        key = b["isbn"] or (b["title"] + b["author"])
        if key not in seen:
            seen.add(key)
            books.append(b)

    return {"books": books[:max_results]}


# ── 표지·책 소개 조회 (YES24) ────────────────────────────────
@app.get("/api/book-info")
async def book_info(q: str = Query("", description="제목+저자"), isbn: str = Query("")):
    if not q and not isbn:
        raise HTTPException(status_code=400, detail="q 또는 isbn 필요")
    return await _yes24_info(q.strip(), isbn.strip())


# ── 커뮤니티 데이터 (추천 도서·후기·하트·참가자) ──────────────
_MBTI_RE = re.compile(r"^[EI][NS][TF][JP]$")


class RecIn(BaseModel):
    type: str
    title: str
    author: str = ""
    isbn: str = ""
    cover: str = ""
    publisher: str = ""
    review: str = ""
    userKey: str


class ReviewIn(BaseModel):
    bkey: str
    text: str
    mbti: str = "MBTI"
    title: str = ""
    userKey: str


class HeartIn(BaseModel):
    key: str
    userKey: str
    on: bool
    type: str = ""


class ParticipateIn(BaseModel):
    type: str
    userKey: str


class ImportIn(BaseModel):
    userKey: str
    recs: dict = {}
    reviews: dict = {}
    hearts: list = []


def _require_ready():
    # 원격 복원 전에 쓰면 복원 시 덮어써져 유실되므로 잠시 거절
    if not _cs.is_ready():
        raise HTTPException(status_code=503, detail="저장소 준비 중입니다. 잠시 후 다시 시도해 주세요.")


def _check_type(t: str):
    if not _MBTI_RE.match(t or ""):
        raise HTTPException(status_code=400, detail="잘못된 MBTI 유형")


@app.get("/api/community")
async def community_snapshot():
    return _cs.snapshot()


@app.get("/api/community/status")
async def community_status():
    return _cs.status()


@app.post("/api/community/recs")
async def community_add_rec(body: RecIn):
    _require_ready()
    _check_type(body.type)
    if not body.title.strip() or not body.userKey:
        raise HTTPException(status_code=400, detail="title, userKey 필요")
    return _cs.add_rec(body.type, body.model_dump())


@app.get("/api/community/reviews")
async def community_reviews(bkey: str = Query(...)):
    return {"reviews": _cs.get_reviews(bkey)}


@app.post("/api/community/reviews")
async def community_add_review(body: ReviewIn):
    _require_ready()
    if not body.text.strip() or not body.bkey or not body.userKey:
        raise HTTPException(status_code=400, detail="bkey, text, userKey 필요")
    return _cs.add_review(body.bkey[:100], body.model_dump())


@app.post("/api/community/heart")
async def community_heart(body: HeartIn):
    _require_ready()
    if not body.key or not body.userKey:
        raise HTTPException(status_code=400, detail="key, userKey 필요")
    mbti = body.type if _MBTI_RE.match(body.type or "") else ""
    return {"count": _cs.set_heart(body.key[:200], body.userKey[:64], body.on, mbti)}


@app.post("/api/community/participate")
async def community_participate(body: ParticipateIn):
    _require_ready()
    _check_type(body.type)
    _cs.participate(body.type, body.userKey[:64])
    return {"ok": True}


@app.post("/api/community/import")
async def community_import(body: ImportIn):
    _require_ready()
    recs = {t: v for t, v in body.recs.items() if _MBTI_RE.match(t) and isinstance(v, list)}
    reviews = {k[:100]: v for k, v in body.reviews.items() if isinstance(v, list)}
    hearts = [h for h in body.hearts if isinstance(h, str)][:500]
    return {"added": _cs.import_local(body.userKey[:64], recs, reviews, hearts)}


# ── 관리자 (커뮤니티 글 삭제) ───────────────────────────────
import hmac as _hmac
from fastapi import Header, Request

_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
_admin_fails: dict[str, list[float]] = {}  # IP → 최근 실패 시각
_ADMIN_MAX_FAILS, _ADMIN_WINDOW = 5, 600     # 10분에 5번 틀리면 잠시 차단


def _require_admin(request: Request, key: str):
    if not _ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD가 설정되지 않았습니다.")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    now = _time.time()
    fails = [t for t in _admin_fails.get(ip, []) if now - t < _ADMIN_WINDOW]
    if len(fails) >= _ADMIN_MAX_FAILS:
        raise HTTPException(status_code=429, detail="비밀번호를 여러 번 틀렸습니다. 10분 뒤 다시 시도해 주세요.")
    if not _hmac.compare_digest((key or "").encode(), _ADMIN_PASSWORD.encode()):
        _admin_fails[ip] = fails + [now]
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")
    _admin_fails.pop(ip, None)


class AdminDeleteIn(BaseModel):
    kind: str   # rec | review
    group: str  # rec: MBTI 유형, review: 도서 키
    id: str


@app.get("/api/admin/items")
async def admin_items(request: Request, x_admin_key: str = Header("")):
    _require_admin(request, x_admin_key)
    return _cs.admin_list()


@app.post("/api/admin/delete")
async def admin_delete(body: AdminDeleteIn, request: Request, x_admin_key: str = Header("")):
    _require_admin(request, x_admin_key)
    _require_ready()
    if body.kind not in ("rec", "review"):
        raise HTTPException(status_code=400, detail="kind는 rec 또는 review")
    if not _cs.admin_delete(body.kind, body.group, body.id):
        raise HTTPException(status_code=404, detail="이미 삭제되었거나 없는 항목입니다.")
    return {"ok": True}


# HTML·JS에 캐시 헤더가 없으면 브라우저가 배포 전 옛 페이지를 계속 쓰므로 매번 재검증하게 한다
@app.middleware("http")
async def _no_stale_pages(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and (path.endswith("/") or path.endswith((".html", ".js"))):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# 상위 디렉터리(mbtibooktalk 루트)를 정적으로 서빙
_root = os.path.join(os.path.dirname(__file__), "..")
app.mount("/", StaticFiles(directory=_root, html=True), name="static")
