import os
import re
import json
from datetime import datetime
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
import httpx
from anthropic import AsyncAnthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

app = FastAPI()

# ==================== 환경변수 ====================

_clean = lambda s: re.sub(r'\s+', '', s or '')
TELEGRAM_BOT_TOKEN = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
AUTHORIZED_CHAT_ID = int(_clean(os.getenv("AUTHORIZED_CHAT_ID")) or "0")
ANTHROPIC_API_KEY = _clean(os.getenv("ANTHROPIC_API_KEY"))
GOOGLE_CLIENT_ID = _clean(os.getenv("GOOGLE_CLIENT_ID"))
GOOGLE_CLIENT_SECRET = _clean(os.getenv("GOOGLE_CLIENT_SECRET"))
GOOGLE_REFRESH_TOKEN = _clean(os.getenv("GOOGLE_REFRESH_TOKEN"))
SLACK_TOKEN = _clean(os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_USER_TOKEN"))
RAILWAY_URL = _clean(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "web-production-87eec.up.railway.app")

REDIRECT_URI = f"https://{RAILWAY_URL}/auth/google/callback"
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
conversation_history = deque(maxlen=20)


# ==================== Google Calendar ====================

def get_google_credentials():
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


def list_calendars():
    """접근 가능한 모든 캘린더 목록 (본인 + 공유받은 팀원들)"""
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 인증이 안 됐어요"}
    
    service = build('calendar', 'v3', credentials=creds)
    cal_list = service.calendarList().list().execute()
    
    calendars = []
    for c in cal_list.get('items', []):
        calendars.append({
            "id": c['id'],
            "name": c.get('summary', '제목 없음'),
            "primary": c.get('primary', False),
            "access_role": c.get('accessRole', ''),
        })
    return {"count": len(calendars), "calendars": calendars}


def get_calendar_events(start_date: str, end_date: str, calendar_id: str = "primary"):
    """캘린더 일정 조회. calendar_id: 'primary' = 본인, 또는 다른 사람 이메일."""
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 인증이 안 됐어요"}
    
    service = build('calendar', 'v3', credentials=creds)
    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=f"{start_date}T00:00:00+09:00",
            timeMax=f"{end_date}T23:59:59+09:00",
            singleEvents=True,
            orderBy='startTime',
            maxResults=50,
        ).execute()
    except Exception as e:
        return {"error": f"캘린더 조회 실패: {str(e)[:200]}"}
    
    events = events_result.get('items', [])
    simplified = []
    for e in events:
        simplified.append({
            "title": e.get('summary', '제목 없음'),
            "start": e['start'].get('dateTime', e['start'].get('date')),
            "end": e['end'].get('dateTime', e['end'].get('date')),
            "description": (e.get('description') or '')[:200],
            "location": e.get('location', ''),
        })
    return {"calendar_id": calendar_id, "count": len(simplified), "events": simplified}


def create_calendar_event(title: str, start_datetime: str, end_datetime: str, description: str = ""):
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


# ==================== Slack ====================

async def slack_api_call(method: str, payload: dict = None):
    """Slack API 호출 헬퍼"""
    url = f"https://slack.com/api/{method}"
    headers = {
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json; charset=utf-8"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        if payload is None:
            response = await client.get(url, headers=headers)
        else:
            response = await client.post(url, headers=headers, json=payload)
        return response.json()


async def search_slack_channel(channel_name_or_keyword: str, limit: int = 30):
    """채널을 찾아서 최근 메시지 조회 (검색 대용)"""
    if not SLACK_TOKEN:
        return {"error": "Slack 토큰이 없어요"}
    
    # 1. 채널 목록에서 이름이 비슷한 채널 찾기
    list_result = await slack_api_call("conversations.list", None)
    channels = list_result.get("channels", [])
    
    matching = [c for c in channels if channel_name_or_keyword.lower() in c.get("name", "").lower()]
    if not matching:
        return {"error": f"'{channel_name_or_keyword}' 이름과 매칭되는 채널 없음", "available_channels": [c["name"] for c in channels[:20]]}
    
    results = {}
    for ch in matching[:3]:  # 매칭된 채널 최대 3개
        history = await slack_api_call("conversations.history", {
            "channel": ch["id"],
            "limit": limit
        })
        messages = history.get("messages", [])
        simplified = []
        for m in messages:
            simplified.append({
                "user": m.get("user", "unknown"),
                "text": m.get("text", "")[:500],
                "ts": m.get("ts", ""),
            })
        results[ch["name"]] = simplified
    return {"channels_found": list(results.keys()), "messages": results}


async def get_slack_channel_messages(channel_id: str, hours_ago: int = 24):
    """특정 채널의 최근 N시간 메시지"""
    if not SLACK_TOKEN:
        return {"error": "Slack 토큰이 없어요"}
    
    import time
    oldest = time.time() - (hours_ago * 3600)
    
    result = await slack_api_call("conversations.history", {
        "channel": channel_id,
        "oldest": str(oldest),
        "limit": 100
    })
    
    messages = result.get("messages", [])
    simplified = [{
        "user": m.get("user", "unknown"),
        "text": m.get("text", "")[:500],
        "ts": m.get("ts", "")
    } for m in messages]
    return {"count": len(simplified), "messages": simplified}


async def send_slack_message(channel_id: str, text: str):
    """슬랙으로 메시지 전송"""
    if not SLACK_TOKEN:
        return {"error": "Slack 토큰이 없어요"}
    
    result = await slack_api_call("chat.postMessage", {
        "channel": channel_id,
        "text": text
    })
    return {"success": result.get("ok"), "ts": result.get("ts"), "error": result.get("error")}


async def list_slack_channels():
    """봇이 접근 가능한 모든 채널 목록"""
    if not SLACK_TOKEN:
        return {"error": "Slack 토큰이 없어요"}
    
    result = await slack_api_call("conversations.list", None)
    channels = result.get("channels", [])
    return {
        "count": len(channels),
        "channels": [{"id": c["id"], "name": c["name"], "is_member": c.get("is_member", False)} for c in channels[:50]]
    }


# ==================== Claude Tools ====================

CLAUDE_TOOLS = [
    {
        "name": "list_calendars",
        "description": "사장님이 접근 가능한 모든 캘린더(본인 + 팀원 공유받은 캘린더) 목록을 가져옵니다. '팀원 일정 알려줘' 같은 질문 시 먼저 호출.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_calendar_events",
        "description": "Google Calendar 일정 조회. 본인 일정은 calendar_id='primary'. 팀원 일정은 list_calendars로 먼저 이메일 ID 확인 후 그 ID 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "calendar_id": {"type": "string", "description": "'primary' 또는 팀원 이메일", "default": "primary"}
            },
            "required": ["start_date", "end_date"]
        }
    },
    {
        "name": "create_calendar_event",
        "description": "본인 캘린더에 새 일정 추가.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_datetime": {"type": "string", "description": "ISO 8601, 예: 2026-05-20T14:00:00"},
                "end_datetime": {"type": "string", "description": "ISO 8601"},
                "description": {"type": "string"}
            },
            "required": ["title", "start_datetime", "end_datetime"]
        }
    },
    {
        "name": "list_slack_channels",
        "description": "봇이 접근 가능한 슬랙 채널 목록. '슬랙에 무슨 채널 있어?' 또는 채널 ID 모를 때 사용.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "search_slack_channel",
        "description": "키워드/채널명으로 슬랙 채널 찾고 그 채널 최근 메시지 조회. '#프로젝트-reboundx에서 무슨 얘기 있었어?' 같은 질문에 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name_or_keyword": {"type": "string", "description": "채널 이름 일부 (예: 'reboundx', '디자인')"},
                "limit": {"type": "integer", "description": "메시지 개수 (기본 30)", "default": 30}
            },
            "required": ["channel_name_or_keyword"]
        }
    },
    {
        "name": "get_slack_channel_messages",
        "description": "특정 채널의 최근 N시간 메시지. channel_id를 알 때 사용 (list_slack_channels로 먼저 확인).",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "C로 시작하는 채널 ID"},
                "hours_ago": {"type": "integer", "description": "몇 시간 전부터 (기본 24)", "default": 24}
            },
            "required": ["channel_id"]
        }
    },
    {
        "name": "send_slack_message",
        "description": "슬랙 채널에 메시지 발송. 사장님이 명시적으로 '메시지 보내', '슬랙 알려' 요청할 때만 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "C로 시작하는 채널 ID"},
                "text": {"type": "string", "description": "보낼 메시지 내용"}
            },
            "required": ["channel_id", "text"]
        }
    }
]


async def execute_tool(tool_name: str, tool_input: dict):
    try:
        if tool_name == "list_calendars":
            return list_calendars()
        elif tool_name == "get_calendar_events":
            return get_calendar_events(**tool_input)
        elif tool_name == "create_calendar_event":
            return create_calendar_event(**tool_input)
        elif tool_name == "list_slack_channels":
            return await list_slack_channels()
        elif tool_name == "search_slack_channel":
            return await search_slack_channel(**tool_input)
        elif tool_name == "get_slack_channel_messages":
            return await get_slack_channel_messages(**tool_input)
        elif tool_name == "send_slack_message":
            return await send_slack_message(**tool_input)
        else:
            return {"error": f"알 수 없는 도구: {tool_name}"}
    except Exception as e:
        return {"error": str(e)}


# ==================== Claude 대화 ====================

SYSTEM_PROMPT = f"""당신은 ReboundX 대표 magon님의 개인 비서 AI입니다.

오늘 날짜: {datetime.now().strftime('%Y-%m-%d (%A)')}
타임존: Asia/Seoul

회사 컨텍스트:
- 회사: ReboundX
- 제품:
  - ReboundX (리베이트 서비스)
  - Terminal (5/22 이후 MVP 배포 예정)
- 주요 인물: 정예진, 이승원, 권은서(Althea), magon

당신은 다음 도구를 사용할 수 있습니다:

[캘린더]
- list_calendars: 접근 가능한 모든 캘린더 (본인 + 팀원 공유받은 것)
- get_calendar_events: 특정 기간 일정 조회 (calendar_id로 본인/팀원 구분)
- create_calendar_event: 본인 캘린더에 일정 추가

[슬랙]
- list_slack_channels: 채널 목록
- search_slack_channel: 채널명으로 찾아서 메시지 조회
- get_slack_channel_messages: 특정 채널 N시간 메시지
- send_slack_message: 메시지 발송 (사장님 명시적 요청 시만)

도구 사용 원칙:
- 팀원 일정 묻는 경우 → list_calendars로 먼저 누구 공유받았나 확인 → 해당 이메일로 get_calendar_events
- 슬랙 채널 ID 모를 때 → list_slack_channels 또는 search_slack_channel 먼저
- 슬랙 메시지 발송은 사장님이 명시적으로 요청할 때만 (자동 발송 금지)

스타일:
- 한국어, 존댓말
- 간결하고 명확하게
- 도구 결과를 정리해서 보고
- 정보 없으면 "없습니다" 명확히
- 이모지 적절히
"""


async def get_claude_response(user_message: str) -> str:
    conversation_history.append({"role": "user", "content": user_message})
    
    max_iterations = 8
    
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
        
        conversation_history.append({
            "role": "assistant",
            "content": [block.model_dump() for block in response.content]
        })
        
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[tool] {block.name} input={block.input}")
                    result = await execute_tool(block.name, dict(block.input))
                    print(f"[tool] result={str(result)[:300]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            conversation_history.append({"role": "user", "content": tool_results})
        else:
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks) if text_blocks else "응답이 비어있어요 🤔"
    
    return "⚠️ 도구 호출이 너무 많이 반복돼서 중단했어요"


# ==================== Telegram ====================

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
            "*명령어*\n/reset - 기억 초기화\n/auth - Google 인증 URL\n\n캘린더+슬랙 조회·추가 가능")
        return {"ok": True}
    
    if text.strip() == "/auth":
        await send_telegram_message(chat_id,
            f"브라우저에서: https://{RAILWAY_URL}/auth/google")
        return {"ok": True}
    
    reply = await get_claude_response(text)
    await send_telegram_message(chat_id, reply)
    return {"ok": True}


# ==================== Google OAuth ====================

_oauth_flow_instance = None


def _create_google_flow():
    return Flow.from_client_config({
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }, scopes=GOOGLE_SCOPES)


@app.get("/auth/google")
def auth_google():
    global _oauth_flow_instance
    _oauth_flow_instance = _create_google_flow()
    _oauth_flow_instance.redirect_uri = REDIRECT_URI
    auth_url, _ = _oauth_flow_instance.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def auth_google_callback(code: str):
    global _oauth_flow_instance
    if _oauth_flow_instance is None:
        return HTMLResponse("<h1>⚠️ 먼저 /auth/google 방문하세요</h1>")
    
    _oauth_flow_instance.fetch_token(code=code)
    refresh_token = _oauth_flow_instance.credentials.refresh_token
    _oauth_flow_instance = None
    
    if not refresh_token:
        return HTMLResponse(
            "<h1>⚠️ Refresh Token 비어있음</h1>"
            "<p><a href='https://myaccount.google.com/permissions'>Google 권한 페이지</a>에서 "
            "'Personal Agent' 삭제 후 다시 시도</p>"
        )
    
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;padding:40px;max-width:700px;margin:auto">
    <h1>✅ 인증 성공!</h1>
    <p>Railway에 <b>GOOGLE_REFRESH_TOKEN</b>으로 추가:</p>
    <pre style="background:#f0f0f0;padding:20px;border-radius:8px;word-break:break-all">{refresh_token}</pre>
    </body></html>
    """)


@app.get("/")
def root():
    return {
        "status": "running",
        "google_authed": bool(GOOGLE_REFRESH_TOKEN),
        "slack_authed": bool(SLACK_TOKEN),
        "history": len(conversation_history)
    }
