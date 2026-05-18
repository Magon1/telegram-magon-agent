import os
import re
from collections import deque
from fastapi import FastAPI, Request
import httpx
from anthropic import AsyncAnthropic

app = FastAPI()

# 환경변수 정리
_clean = lambda s: re.sub(r'\s+', '', s or '')
TELEGRAM_BOT_TOKEN = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
AUTHORIZED_CHAT_ID = int(_clean(os.getenv("AUTHORIZED_CHAT_ID")) or "0")
ANTHROPIC_API_KEY = _clean(os.getenv("ANTHROPIC_API_KEY"))

# Claude 클라이언트
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# 대화 기억 (최근 20턴)
conversation_history = deque(maxlen=20)

SYSTEM_PROMPT = """당신은 ReboundX 대표(magon)의 개인 비서 AI입니다.

회사 컨텍스트:
- 회사: ReboundX
- 제품: ReboundX (리베이트), Terminal (베타 출시 준비 중), Lux
- 주요 인물: 정예진, 이승원, 권은서(althea), magon

대화 스타일:
- 한국어, 존댓말 사용
- 친근하지만 프로페셔널한 톤
- 답변은 간결하게 (텔레그램에서 읽기 좋게)
- 사장님이 묻는 질문에 맞춰 명확하게 답변
- 모르면 모른다고 솔직하게 답변
- 이모지는 적절히 (1~2개씩)

현재는 도구 사용 없이 일반 대화만 가능합니다. 
앞으로 캘린더/슬랙/노션 조회 기능이 추가될 예정이며, 
그때는 사장님이 요청하시면 도구를 호출할 수 있게 됩니다.
"""


async def send_telegram_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
        )
        print(f"[telegram] status={response.status_code}")


async def get_claude_response(user_message: str) -> str:
    """대화 기억과 함께 Claude 호출"""
    conversation_history.append({
        "role": "user",
        "content": user_message
    })

    try:
        message = await claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=list(conversation_history)
        )
        assistant_reply = message.content[0].text

        conversation_history.append({
            "role": "assistant",
            "content": assistant_reply
        })

        return assistant_reply
    except Exception as e:
        print(f"[claude error] {e}")
        return f"⚠️ 에러가 발생했어요: {str(e)[:200]}"


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    print(f"[webhook] {data}")

    if "message" not in data:
        return {"ok": True}

    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if chat_id != AUTHORIZED_CHAT_ID:
        return {"ok": True}

    # 특수 명령어
    if text.strip() == "/reset":
        conversation_history.clear()
        await send_telegram_message(chat_id, "🔄 대화 기억 초기화됐습니다.")
        return {"ok": True}

    if text.strip() == "/help":
        help_text = """*명령어*
/reset - 대화 기억 초기화
/help - 도움말

그 외 아무 말이나 보내시면 Claude가 답합니다."""
        await send_telegram_message(chat_id, help_text)
        return {"ok": True}

    # 일반 대화 → Claude
    reply = await get_claude_response(text)
    await send_telegram_message(chat_id, reply)

    return {"ok": True}


@app.get("/")
def root():
    return {"status": "running", "history_length": len(conversation_history)}
