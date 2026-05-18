import os
import re
import json
from datetime import datetime
from collections import deque
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
import httpx
from anthropic import AsyncAnthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

app = FastAPI()

# 환경변수 정리
_clean = lambda s: re.sub(r'\s+', '', s or '')
TELEGRAM_BOT_TOKEN = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
AUTHORIZED_CHAT_ID = int(_clean(os.getenv("AUTHORIZED_CHAT_ID")) or "0")
ANTHROPIC_API_KEY = _clean(os.getenv("ANTHROPIC_API_KEY"))
GOOGLE_CLIENT_ID = _clean(os.getenv("GOOGLE_CLIENT_ID"))
GOOGLE_CLIENT_SECRET = _clean(os.getenv("GOOGLE_CLIENT_SECRET"))
GOOGLE_REFRESH_TOKEN = _clean(os.getenv("GOOGLE_REFRESH_TOKEN"))
RAILWAY_URL = _clean(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "web-production-87eec.up.railway.app")

REDIRECT_URI = f"https://{RAILWAY_URL}/auth/google/callback"
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
conversation_history = deque(maxlen=20)


# ==================== Google Calendar ====================

def get_google_credentials():
    """리프레시 토큰으로 Google Calendar 인증"""
    if not GOOGLE_REFRESH_TOKEN:
        return None
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GOOGLE_SCOPES,
    )
    creds.refresh(GoogleRequest())
    return creds


def get_calendar_events(start_date: str, end_date: str):
    """캘린더 일정 조회"""
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 인증이 안 됐어요. /auth/google 방문 필요"}
    
    service = build('calendar', 'v3', credentials=creds)
    events_result = service.events().list(
        calendarId='primary',
        timeMin=f"{start_date}T00:00:00+09:00",
        timeMax=f"{end_date}T23:59:59+09:00",
        singleEvents=True,
        orderBy='startTime',
        maxResults=50,
    ).execute()
    
    events = events_result.get('items', [])
    simplified = []
    for e in events:
        simplified.append({
            "title": e.get('summary', '제목 없음'),
            "start": e['start'].get('dateTime', e['start'].get('date')),
            "end": e['end'].get('dateTime', e['end'].get('date')),
            "description": e.get('description', '')[:200] if e.get('description') else '',
            "location": e.get('location', ''),
        })
    return {"count": len(simplified), "events": simplified}


def create_calendar_event(title: str, start_datetime: str, end_datetime: str, description: str = ""):
    """캘린더에 일정 추가"""
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 인증이 안 됐어요"}
    
    service = build('calendar', 'v3', credentials=creds)
    event = {
        'summary': title,
        'description': description,
        'start': {'dateTime': start_datetime, 'timeZone': 'Asia/Seoul'},
        'end': {'dateTime': end_datetime, 'timeZone': 'Asia/Seoul'},
    }
    created = service.events().insert(calendarId='primary', body=event).execute()
    return {"success": True, "event_link": created.get('htmlLink'), "title": title}


# ==================== Claude Tools ====================

CLAUDE_TOOLS = [
    {
        "name": "get_calendar_events",
        "description": "사장님의 Google Calendar에서 특정 기간 일정을 조회합니다. '내일 일정', '이번 주 미팅' 같은 질문에 사용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "조회 시작 날짜 (YYYY-MM-DD 형식)"
                },
                "end_date": {
                    "type": "string",
                    "description": "조회 종료 날짜 (YYYY-MM-DD 형식)"
                }
            },
            "required": ["start_date", "end_date"]
        }
    },
    {
        "name": "create_calendar_event",
        "description": "Google Calendar에 새 일정을 추가합니다. 사장님이 '일정 잡아줘', '미팅 추가해' 같은 요청을 할 때 사용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "일정 제목"},
                "start_datetime": {
                    "type": "string",
                    "description": "시작 시간 (ISO 8601 형식, 예: 2026-05-20T14:00:00)"
                },
                "end_datetime": {
                    "type": "string",
                    "description": "종료 시간 (ISO 8601 형식)"
                },
                "description": {"type": "string", "description": "일정 설명 (선택)"}
            },
            "required": ["title", "start_datetime", "end_datetime"]
        }
    }
]


def execute_tool(tool_name: str, tool_input: dict):
    """Claude가 호출한 도구 실행"""
    try:
        if tool_name == "get_calendar_events":
            return get_calendar_events(**tool_input)
        elif tool_name == "create_calendar_event":
            return create_calendar_event(**tool_input)
        else:
            return {"error": f"알 수 없는 도구: {tool_name}"}
    except Exception as e:
        return {"error": str(e)}


# ==================== Claude 대화 ====================

SYSTEM_PROMPT = f"""당신은 ReboundX HQ 대표(magon)의 개인 비서 AI입니다.

오늘 날짜: {datetime.now().strftime('%Y-%m-%d (%A)')}
타임존: Asia/Seoul

회사 컨텍스트:
- 회사: Umbrella X HQ
- 제품: ReboundX (리베이트), Terminal (베타 출시 준비 중), Lux
- 주요 인물: 정예진, 이승원, 권은서(Althea), magon

당신은 다음 도구를 사용할 수 있습니다:
- get_calendar_events: 캘린더 일정 조회
- create_calendar_event: 캘린더 일정 추가

스타일:
- 한국어, 존댓말
- 간결하고 명확하게
- 도구 호출 결과를 자연스럽게 정리해서 보고
- 일정이 없으면 "없습니다" 명확히 답변
- 이모지 적절히 사용
"""


async def get_claude_response(user_message: str) -> str:
    """대화 + 도구 사용 (agentic loop)"""
    conversation_history.append({"role": "user", "content": user_message})
    
    max_iterations = 5
    
    for _ in range(max_iterations):
        try:
            response = await claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=CLAUDE_TOOLS,
                messages=list(conversation_history),
            )
        except Exception as e:
            print(f"[claude error] {e}")
            return f"⚠️ Claude 호출 에러: {str(e)[:200]}"
        
        # Claude 응답을 히스토리에 저장
        conversation_history.append({
            "role": "assistant",
            "content": [block.model_dump() for block in response.content]
        })
        
        if response.stop_reason == "tool_use":
            # 도구 호출 처리
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[tool] calling {block.name} with {block.input}")
                    result = execute_tool(block.name, dict(block.input))
                    print(f"[tool] result: {str(result)[:200]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            
            conversation_history.append({
                "role": "user",
                "content": tool_results
            })
            # 루프 계속 - Claude가 결과 보고 답변 또는 추가 도구 호출
        else:
            # Claude 답변 완료
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks) if text_blocks else "응답이 비어있어요 🤔"
    
    return "⚠️ 도구 호출이 너무 많이 반복돼서 중단했어요"


# ==================== Telegram Webhook ====================

async def send_telegram_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        })


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "message" not in data:
        return {"ok": True}
    
    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    
    if chat_id != AUTHORIZED_CHAT_ID:
        return {"ok": True}
    
    if text.strip() == "/reset":
        conversation_history.clear()
        await send_telegram_message(chat_id, "🔄 대화 기억 초기화")
        return {"ok": True}
    
    if text.strip() == "/help":
        await send_telegram_message(chat_id, 
            "*명령어*\n/reset - 기억 초기화\n/auth - Google 인증 URL\n\n캘린더 조회·추가 가능합니다.")
        return {"ok": True}
    
    if text.strip() == "/auth":
        await send_telegram_message(chat_id, 
            f"브라우저에서 다음 URL 방문:\nhttps://{RAILWAY_URL}/auth/google")
        return {"ok": True}
    
    reply = await get_claude_response(text)
    await send_telegram_message(chat_id, reply)
    return {"ok": True}


# ==================== Google OAuth ====================

@app.get("/auth/google")
def auth_google():
    flow = Flow.from_client_config({
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def auth_google_callback(code: str):
    flow = Flow.from_client_config({
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = REDIRECT_URI
    flow.fetch_token(code=code)
    
    refresh_token = flow.credentials.refresh_token
    
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;padding:40px;max-width:700px;margin:auto">
    <h1>✅ 인증 성공!</h1>
    <p>아래 값을 <b>Railway → Variables</b>에 <b>GOOGLE_REFRESH_TOKEN</b>으로 추가하세요:</p>
    <pre style="background:#f0f0f0;padding:20px;border-radius:8px;word-break:break-all;font-size:14px">{refresh_token}</pre>
    <p style="color:#666">추가 후 Deploy 클릭, 1분 대기 후 텔레그램에서 "내일 일정 뭐 있어?" 테스트.</p>
    </body></html>
    """)


@app.get("/")
def root():
    return {
        "status": "running",
        "google_authed": bool(GOOGLE_REFRESH_TOKEN),
        "history": len(conversation_history)
    }
