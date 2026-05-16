"""Claude API integration — librarian-style response generation.

개선 사항(2026-05, 3차):
- 하이브리드 응답 모드: 검색 품질에 따라 RAG / 일반 지식 자동 분기
  · 강한 매칭(점수 ≥ MIN_RAG_SCORE) → RAG 모드(인용 답변)
  · 매칭 없음·약한 매칭            → 일반 지식 모드(사서 톤 + 출처 안내)
- 시스템 프롬프트 2종으로 분리하고, 일반 지식 모드에선 안내 문구 자동 삽입
- 컨텍스트 8건, MAX_TOKENS 2200, 인용 [n] 정규식 추출은 유지
"""

import os
import re
from typing import Optional

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2200
CONTEXT_TOP_N = 8

# RAG 모드 진입 임계값 — 환경변수로 튜닝 가능.
# combined_score(RRF)는 일반적으로 0~0.02 범위이므로 기본 0.008 사용.
MIN_RAG_SCORE = float(os.getenv("MIN_RAG_SCORE", "0.008"))

# ── 시스템 프롬프트 ─────────────────────────────────────────
SYSTEM_PROMPT_RAG = """당신은 국립중앙도서관 소속 전문 사서이자, 전북대학교 중앙도서관 이용자 응대 챗봇입니다.

[응대 원칙]
1. 따뜻하고 격려하는 사서 말투를 유지하되, 공공기관 응대에 어울리는 정중한 문어체를 사용합니다.
2. 답변은 반드시 한국어로 작성하고, 도서 제목은 『 』, 저자명은 〈 〉로 표기합니다.
3. 본문에서 참고자료를 인용할 때는 반드시 [1], [2]와 같은 인용 번호를 문장 끝에 표기합니다.
4. 참고자료에 없는 정보를 단정적으로 답하지 않습니다. 추측이 필요한 경우 "사서 의견으로는…" 표현을 사용합니다.

[답변 구성]
가. 인사 및 질문 요지 확인 (1~2문장)
나. 핵심 답변 — 자료 근거와 함께 도서·정보 제시 (3~5문장, 인용 번호 표기)
다. 추천 도서 목록 — 최소 2종 이상, "제목 / 저자 / 핵심 키워드 / 추천 이유" 형식
라. 추가 안내 — 전북대 도서관 소장 여부 확인 권유, 관련 KDC 분류, 후속 질문 유도

[금지]
- 참고자료에 등장하지 않는 가공의 도서·저자명 생성
- 출처 인용 번호 없이 단정적 사실 진술
- 정치·종교·민감 주제에 대한 개인적 견해 표명
"""

SYSTEM_PROMPT_GENERAL = """당신은 국립중앙도서관 소속 전문 사서이자, 전북대학교 중앙도서관 이용자 응대 챗봇입니다.

이번 질의는 국립중앙도서관 「사서에게 물어보세요」 DB에 직접 매칭되는 사례가 없거나, 매칭 신뢰도가 낮은 사항입니다.
따라서 사서의 일반 전문 지식과 도서관 운영 상식을 바탕으로 답변하되, 다음 원칙을 반드시 지키십시오.

[응대 원칙]
1. 답변 첫 줄에 다음 안내 문구를 그대로 포함합니다.
   "※ 본 답변은 국립중앙도서관 Q&A DB에 직접 매칭되는 자료가 없어, 사서의 일반 지식을 바탕으로 안내드립니다."
2. 이후 본문은 사서의 따뜻하고 정중한 문어체로 작성합니다.
3. 도서 제목은 『 』, 저자명은 〈 〉로 표기합니다.
4. 시설·이용·정책 관련 질의일 경우, 전북대학교 중앙도서관 일반 운영 관행을 참고해 안내하되,
   세부 시간·층수·정책은 "각 도서관 안내데스크 또는 홈페이지에서 최종 확인을 권합니다"라고 첨언합니다.
5. 가공된 통계·법령 조항·정확한 수치는 단정하지 마십시오.

[답변 구성]
가. 안내 문구(고정) + 인사 (1~2문장)
나. 핵심 답변 — 일반 지식 기반 정보 제공 (3~5문장)
다. 관련 도서·자료가 떠오르면 2~3종 제시 (선택)
라. 추가 안내 — 정확한 정보 확인 경로(홈페이지·안내데스크·전화) 안내, 후속 질문 유도

[금지]
- 존재하지 않는 도서·저자·통계 생성
- 임의의 학내 규정·시간표 단정
- 정치·종교·민감 주제 의견 제시
"""


def _format_context(results: list[dict]) -> str:
    parts = []
    for i, r in enumerate(results[:CONTEXT_TOP_N], 1):
        q = r.get("question", "")[:250]
        a = r.get("answer", "")[:800]
        subj = r.get("subject", "")
        ans_lib = r.get("answer_lib", "")
        ans_date = r.get("answer_date", "")
        header = f"[{i}] 주제분야: {subj} | 답변도서관: {ans_lib} | 답변일: {ans_date}"
        parts.append(f"{header}\n질문: {q}\n사서답변:\n{a}")
    return "\n\n".join(parts)


def _classify_response_mode(results: list[dict]) -> str:
    """검색 결과 품질에 따라 'rag' 또는 'general' 반환."""
    if not results:
        return "general"
    top_score = results[0].get("combined_score", results[0].get("score", 0.0))
    return "rag" if top_score >= MIN_RAG_SCORE else "general"


def _fallback_response(user_query: str, results: list[dict], mode: str) -> str:
    """Claude API 미사용 시 모드별 fallback 응답."""
    if mode == "rag" and results:
        lines = [f"📚 관련 Q&A {len(results)}건을 확인하였습니다.\n"]
        for i, r in enumerate(results[:3], 1):
            q = (r.get("question") or "").strip().replace("\n", " ")[:120]
            a = (r.get("answer") or "").strip().replace("\n", " ")[:220]
            lines.append(f"[{i}] {q}\n   → {a}...")
        lines.append("\n※ Claude API 키가 설정되지 않아 자동 요약본을 제공해 드렸습니다.")
        return "\n".join(lines)

    # general 모드 fallback
    return (
        "※ 본 답변은 국립중앙도서관 Q&A DB에 직접 매칭되는 자료가 없어, "
        "사서의 일반 안내로 갈음합니다.\n\n"
        f"문의하신 「{user_query}」에 대해 자세한 안내가 필요하시면, "
        "전북대학교 중앙도서관 홈페이지 또는 안내데스크(063-270-2454)로 "
        "문의해 주시기 바랍니다."
    )


def get_librarian_response(user_query: str, search_results: list[dict]) -> dict:
    """이용자 질의 + 검색 결과 → 사서 응답.

    Returns
    -------
    dict
        - response (str): 사서 답변 본문
        - used_claude (bool): Claude API 사용 여부
        - source_count (int): 컨텍스트에 투입된 자료 건수
        - cited_indices (list[int]): 응답 본문에서 인용된 [n] 번호 목록
        - response_mode (str): 'rag' | 'general'
        - top_score (float): 최상위 결과 점수(분기 판정 근거)
    """
    mode = _classify_response_mode(search_results)
    top_score = (search_results[0].get("combined_score", search_results[0].get("score", 0.0))
                 if search_results else 0.0)
    source_count = len(search_results)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not _HAS_ANTHROPIC or not api_key:
        return {
            "response": _fallback_response(user_query, search_results, mode),
            "used_claude": False,
            "source_count": source_count,
            "cited_indices": [],
            "response_mode": mode,
            "top_score": round(float(top_score), 5),
        }

    client = anthropic.Anthropic(api_key=api_key)

    if mode == "rag":
        context = _format_context(search_results)
        user_content = (
            f"이용자 질문: {user_query}\n\n"
            f"아래는 국립중앙도서관 「사서에게 물어보세요」 DB에서 추출한 관련 Q&A 자료입니다.\n"
            f"각 자료의 [1], [2] 번호는 답변 본문에서 인용 번호로 사용해 주십시오.\n\n"
            f"{context}\n\n"
            f"위 참고자료를 근거로, 시스템 지침에 따라 인용 번호를 표기하며 답변해 주십시오."
        )
        system_prompt = SYSTEM_PROMPT_RAG
    else:
        # general 모드 — 검색 결과가 있더라도 약한 매칭이므로 참고만 하도록 안내
        weak_hint = ""
        if search_results:
            top3 = "\n".join(
                f"- (참고) {r.get('question','')[:100]}" for r in search_results[:3]
            )
            weak_hint = (
                "\n참고용 약한 매칭 자료(인용 번호로 사용하지 마십시오):\n" + top3 + "\n"
            )
        user_content = (
            f"이용자 질문: {user_query}\n"
            f"{weak_hint}\n"
            f"이 질문은 국립중앙도서관 Q&A DB에 직접적인 답변 사례가 없습니다. "
            f"시스템 지침의 안내 문구를 반드시 첫 줄에 포함하고, "
            f"사서의 일반 전문 지식으로 답변해 주십시오."
        )
        system_prompt = SYSTEM_PROMPT_GENERAL

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    text = message.content[0].text

    cited = sorted({int(m) for m in re.findall(r"\[(\d{1,2})\]", text)}) if mode == "rag" else []

    return {
        "response": text,
        "used_claude": True,
        "source_count": source_count,
        "cited_indices": cited,
        "response_mode": mode,
        "top_score": round(float(top_score), 5),
    }
