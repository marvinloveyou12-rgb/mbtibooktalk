"""Claude API integration — librarian-style response generation."""

import os
from typing import Optional

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500

SYSTEM_PROMPT = """당신은 국립중앙도서관의 친절하고 전문적인 사서입니다.
이용자의 질문을 받으면 따뜻하고 격려하는 말투로, 구체적인 도서와 정보를 포함하여 답변합니다.
참고 자료를 바탕으로 답변하되, 사서만의 전문 지식과 공감 능력을 발휘하세요.
답변은 반드시 한국어로 작성하고, 자연스럽고 친근한 문어체를 사용하세요.
도서 제목은 『 』로 감싸고, 핵심 내용은 단락을 나눠 읽기 쉽게 정리해 주세요."""


def _format_context(results: list[dict]) -> str:
    parts = []
    for i, r in enumerate(results[:5], 1):
        q = r.get("question", "")[:200]
        a = r.get("answer", "")[:600]
        subj = r.get("subject", "")
        parts.append(f"[참고자료 {i}] 주제분야: {subj}\n질문: {q}\n사서 답변:\n{a}")
    return "\n\n".join(parts)


def get_librarian_response(user_query: str, search_results: list[dict]) -> dict:
    """
    Returns {"response": str, "used_claude": bool, "source_count": int}
    Falls back to a summary if Claude API is unavailable.
    """
    context = _format_context(search_results)
    source_count = len(search_results)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not _HAS_ANTHROPIC or not api_key:
        # Fallback: return a structured summary without Claude
        if not search_results:
            fallback = "죄송합니다. 관련 자료를 찾지 못했습니다. 다른 키워드로 검색해 보시겠어요?"
        else:
            top = search_results[0]
            fallback = (
                f"📚 관련 질문을 {source_count}건 찾았습니다.\n\n"
                f"가장 유사한 질문: {top.get('question', '')}\n\n"
                f"사서 답변 요약:\n{top.get('answer', '')[:400]}..."
            )
        return {"response": fallback, "used_claude": False, "source_count": source_count}

    client = anthropic.Anthropic(api_key=api_key)

    user_content = f"""이용자 질문: {user_query}

아래는 국립중앙도서관 지식정보DB에서 찾은 관련 Q&A 자료입니다:

{context}

위 자료를 참고하여, 이용자의 질문에 사서처럼 따뜻하고 전문적으로 답변해 주세요.
구체적인 도서 추천이 포함되면 더욱 좋습니다."""

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    return {
        "response": message.content[0].text,
        "used_claude": True,
        "source_count": source_count,
    }
