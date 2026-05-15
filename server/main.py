from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import xml.etree.ElementTree as ET
import asyncio
import os
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

from .database import init_db, count_items
from .crawler import crawl_state, run_crawl
from .search_engine import hybrid_search, build_index, invalidate_cache
from .claude_chat import get_librarian_response

app = FastAPI(title="도서 큐레이션 API")

# 실시간 접속자 추적 (세션ID → 마지막 ping 시각)
import time as _time
_presence: dict[str, float] = {}
_PRESENCE_TTL = 90  # 90초 이상 ping 없으면 오프라인 처리

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# DB 초기화 및 인덱스 복구 (앱 시작 시)
init_db()
import threading as _threading
from .search_engine import build_index as _build_index
from .database import count_items as _count_items, seed_from_json as _seed

def _startup():
    _seed()
    if _count_items() > 0:
        _build_index()

_threading.Thread(target=_startup, daemon=True).start()


# ── 사서 Q&A 엔드포인트 ─────────────────────────────────────

class AskRequest(BaseModel):
    query: str
    top_k: int = 8


@app.post("/api/librarian/ask")
async def librarian_ask(req: AskRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="질문을 입력해주세요.")

    results = await asyncio.get_event_loop().run_in_executor(
        None, hybrid_search, req.query, req.top_k
    )
    chat = await asyncio.get_event_loop().run_in_executor(
        None, get_librarian_response, req.query, results
    )

    return {
        "query": req.query,
        "response": chat["response"],
        "used_claude": chat["used_claude"],
        "source_count": chat["source_count"],
        "sources": [{
            "rec_key": r.get("rec_key"),
            "question": r.get("question", "")[:150],
            "answer": r.get("answer", "")[:400],
            "subject": r.get("subject", ""),
            "answer_date": r.get("answer_date", ""),
            "answer_lib": r.get("answer_lib", ""),
            "search_type": r.get("search_type", ""),
            "score": round(r.get("combined_score", r.get("score", 0)), 4),
        } for r in results]
    }


@app.post("/api/crawl/start")
async def crawl_start():
    if crawl_state["running"]:
        return {"status": "already_running", **crawl_state}

    def _rebuild():
        invalidate_cache()
        build_index()

    asyncio.create_task(run_crawl(rebuild_index_fn=_rebuild))
    return {"status": "started", **crawl_state}


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



API_KEY = os.getenv("NL_API_KEY")
SEARCH_URL = "https://www.nl.go.kr/NL/search/openApi/search.do"
SASEO_URL  = "https://www.nl.go.kr/NL/search/openApi/saseoApi.do"

# KDC 10진 분류 → 키워드 매핑
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

# KDC → 사서추천도서 drCode
DR_CODE_MAP = {
    "0": 5, "1": 6, "2": 6, "3": 5,
    "4": 4, "5": 4, "6": 6, "7": 6,
    "8": 11, "9": 6,
}

DR_CODE_NAME = {4: "자연과학", 5: "사회과학", 6: "인문과학", 11: "문학"}


def classify_keyword(keyword: str) -> Tuple[Optional[str], dict]:
    """키워드로 KDC 분류 추론. (kdc_code, kdc_info) 반환"""
    scores = {kdc: 0 for kdc in KDC_MAP}
    for kdc, info in KDC_MAP.items():
        for kw in info["keywords"]:
            if keyword == kw:       # 완전 일치
                scores[kdc] += 10
            elif kw in keyword:     # 사전 단어가 입력에 포함
                scores[kdc] += 3
            elif keyword in kw:     # 입력이 사전 단어에 포함 (부분 일치)
                scores[kdc] += 2

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return None, {"name": "미분류", "color": "#9CA3AF"}
    return best, KDC_MAP[best]


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


def strip_html(text: str) -> str:
    """HTML 태그 및 엔티티를 제거하고 순수 텍스트만 반환"""
    import re, html
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


# 상위 디렉터리(mbtibooktalk 루트)를 정적으로 서빙
_root = os.path.join(os.path.dirname(__file__), "..")
app.mount("/", StaticFiles(directory=_root, html=True), name="static")
