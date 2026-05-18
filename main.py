import os
import re
import json
import io
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
from pinecone import Pinecone
import voyageai
from pypdf import PdfReader
from pptx import Presentation
from docx import Document

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
PINECONE_API_KEY = _clean(os.getenv("PINECONE_API_KEY"))
PINECONE_INDEX_NAME = _clean(os.getenv("PINECONE_INDEX_NAME")) or "knowledge-base"
VOYAGE_API_KEY = _clean(os.getenv("VOYAGE_API_KEY"))
RAILWAY_URL = _clean(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "web-production-87eec.up.railway.app")

REDIRECT_URI = f"https://{RAILWAY_URL}/auth/google/callback"
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
conversation_history = deque(maxlen=20)

# Pinecone + Voyage 초기화
pc = Pinecone(api_key=PINECONE_API_KEY) if PINECONE_API_KEY else None
pinecone_index = pc.Index(PINECONE_INDEX_NAME) if pc else None
voyage_client = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None


# ==================== 텍스트 추출 ====================

def extract_text_from_pdf(file_bytes: bytes) -> list:
    """PDF에서 페이지별 텍스트 추출"""
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            pages.append({"page": i + 1, "text": text})
    return pages


def extract_text_from_pptx(file_bytes: bytes) -> list:
    """PPTX에서 슬라이드별 텍스트 추출"""
    prs = Presentation(io.BytesIO(file_bytes))
    slides = []
    for i, slide in enumerate(prs.slides):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                texts.append(shape.text)
        combined = "\n".join(texts).strip()
        if combined:
            slides.append({"page": i + 1, "text": combined})
    return slides


def extract_text_from_docx(file_bytes: bytes) -> list:
    """DOCX에서 문단별 텍스트 추출"""
    doc = Document(io.BytesIO(file_bytes))
    paragraphs = []
    current_chunk = []
    chunk_idx = 1
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            current_chunk.append(text)
            if len(" ".join(current_chunk)) > 800:
                paragraphs.append({"page": chunk_idx, "text": "\n".join(current_chunk)})
                current_chunk = []
                chunk_idx += 1
    if current_chunk:
        paragraphs.append({"page": chunk_idx, "text": "\n".join(current_chunk)})
    return paragraphs


def extract_text(filename: str, file_bytes: bytes) -> list:
    name_lower = filename.lower()
    if name_lower.endswith(".pdf"):
        return extract_text_from_pdf(file_bytes)
    elif name_lower.endswith(".pptx"):
        return extract_text_from_pptx(file_bytes)
    elif name_lower.endswith(".docx"):
        return extract_text_from_docx(file_bytes)
    elif name_lower.endswith(".txt"):
        text = file_bytes.decode('utf-8', errors='ignore')
        chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
        return [{"page": i+1, "text": c} for i, c in enumerate(chunks)]
    else:
        return []


# ==================== RAG 임베딩 + 저장 ====================

def chunk_text(text: str, max_chars: int = 1500) -> list:
    """긴 텍스트를 청크로 분할"""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    current = ""
    for sentence in re.split(r'(?<=[.!?。!?])\s+', text):
        if len(current) + len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
            current = sentence
        else:
            current += " " + sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


def embed_texts(texts: list) -> list:
    """Voyage AI로 텍스트 임베딩"""
    if not voyage_client:
        return []
    result = voyage_client.embed(
        texts=texts,
        model="voyage-3",
        input_type="document"
    )
    return result.embeddings


async def index_document(filename: str, file_bytes: bytes) -> dict:
    """문서를 추출 → 청크 → 임베딩 → Pinecone 저장"""
    if not pinecone_index or not voyage_client:
        return {"error": "Pinecone 또는 Voyage 미설정"}
    
    pages = extract_text(filename, file_bytes)
    if not pages:
        return {"error": f"지원하지 않는 파일 형식 또는 빈 파일: {filename}"}
    
    # 청크 만들기
    vectors_to_upsert = []
    chunk_count = 0
    
    for page_info in pages:
        page_num = page_info["page"]
        text = page_info["text"]
        chunks = chunk_text(text, max_chars=1500)
        
        if not chunks:
            continue
        
        # 임베딩
        embeddings = embed_texts(chunks)
        
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            vector_id = f"{filename}::page{page_num}::chunk{i}"
            vectors_to_upsert.append({
                "id": vector_id,
                "values": emb,
                "metadata": {
                    "filename": filename,
                    "page": page_num,
                    "text": chunk[:2000],
                    "indexed_at": datetime.now().isoformat()
                }
            })
            chunk_count += 1
    
    # Pinecone에 배치 업로드 (한 번에 100개씩)
    for i in range(0, len(vectors_to_upsert), 100):
        batch = vectors_to_upsert[i:i+100]
        pinecone_index.upsert(vectors=batch)
    
    return {
        "success": True,
        "filename": filename,
        "pages": len(pages),
        "chunks": chunk_count
    }


def search_knowledge_base(query: str, top_k: int = 5) -> dict:
    """질문으로 관련 청크 검색"""
    if not pinecone_index or not voyage_client:
        return {"error": "Pinecone 또는 Voyage 미설정"}
    
    query_emb = voyage_client.embed(
        texts=[query],
        model="voyage-3",
        input_type="query"
    ).embeddings[0]
    
    results = pinecone_index.query(
        vector=query_emb,
        top_k=top_k,
        include_metadata=True
    )
    
    matches = []
    for match in results.matches:
        matches.append({
            "filename": match.metadata.get("filename", ""),
            "page": match.metadata.get("page", 0),
            "score": round(match.score, 3),
            "text": match.metadata.get("text", "")
        })
    return {"count": len(matches), "matches": matches}


def list_indexed_documents() -> dict:
    """인덱스된 모든 문서 목록"""
    if not pinecone_index:
        return {"error": "Pinecone 미설정"}
    
    stats = pinecone_index.describe_index_stats()
    total = stats.get("total_vector_count", 0)
    
    # 파일명별 청크 개수 집계 (샘플링)
    files = {}
    try:
        # Pinecone는 메타데이터 별도 쿼리가 필요. 간단히 statistics만 반환
        return {"total_chunks": total, "note": "전체 청크 수만 표시 (파일명별 집계는 검색 시 확인)"}
    except Exception as e:
        return {"total_chunks": total, "error": str(e)}


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
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 미인증"}
    service = build('calendar', 'v3', credentials=creds)
    cal_list = service.calendarList().list().execute()
    return {"count": len(cal_list.get('items', [])), 
            "calendars": [{"id": c['id'], "name": c.get('summary'), "primary": c.get('primary', False)} 
                          for c in cal_list.get('items', [])]}


def get_calendar_events(start_date: str, end_date: str, calendar_id: str = "primary"):
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 미인증"}
    service = build('calendar', 'v3', credentials=creds)
    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=f"{start_date}T00:00:00+09:00",
            timeMax=f"{end_date}T23:59:59+09:00",
            singleEvents=True, orderBy='startTime', maxResults=50,
        ).execute()
    except Exception as e:
        return {"error": f"캘린더 조회 실패: {str(e)[:200]}"}
    
    events = events_result.get('items', [])
    return {"calendar_id": calendar_id, "count": len(events), "events": [{
        "title": e.get('summary', '제목 없음'),
        "start": e['start'].get('dateTime', e['start'].get('date')),
        "end": e['end'].get('dateTime', e['end'].get('date')),
        "description": (e.get('description') or '')[:200],
        "location": e.get('location', ''),
    } for e in events]}


def create_calendar_event(title: str, start_datetime: str, end_datetime: str, description: str = ""):
    creds = get_google_credentials()
    if not creds:
        return {"error": "Google 미인증"}
    service = build('calendar', 'v3', credentials=creds)
    created = service.events().insert(calendarId='primary', body={
        'summary': title, 'description': description,
        'start': {'dateTime': start_datetime, 'timeZone': 'Asia/Seoul'},
        'end': {'dateTime': end_datetime, 'timeZone': 'Asia/Seoul'},
    }).execute()
    return {"success": True, "event_link": created.get('htmlLink'), "title": title}


# ==================== Slack ====================

async def slack_api_call(method: str, payload: dict = None):
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json; charset=utf-8"}
    async with httpx.AsyncClient(timeout=30) as client:
        if payload is None:
            response = await client.get(url, headers=headers)
        else:
            response = await client.post(url, headers=headers, json=payload)
        return response.json()


async def search_slack_channel(channel_name_or_keyword: str, limit: int = 30):
    if not SLACK_TOKEN:
        return {"error": "Slack 미설정"}
    list_result = await slack_api_call("conversations.list", None)
    channels = list_result.get("channels", [])
    matching = [c for c in channels if channel_name_or_keyword.lower() in c.get("name", "").lower()]
    if not matching:
        return {"error": f"매칭 채널 없음", "available_channels": [c["name"] for c in channels[:20]]}
    results = {}
    for ch in matching[:3]:
        history = await slack_api_call("conversations.history", {"channel": ch["id"], "limit": limit})
        results[ch["name"]] = [{"user": m.get("user"), "text": m.get("text", "")[:500], "ts": m.get("ts")}
                                for m in history.get("messages", [])]
    return {"channels_found": list(results.keys()), "messages": results}


async def get_slack_channel_messages(channel_id: str, hours_ago: int = 24):
    if not SLACK_TOKEN:
        return {"error": "Slack 미설정"}
    import time
    result = await slack_api_call("conversations.history", {
        "channel": channel_id, "oldest": str(time.time() - hours_ago * 3600), "limit": 100
    })
    return {"count": len(result.get("messages", [])), "messages": [
        {"user": m.get("user"), "text": m.get("text", "")[:500], "ts": m.get("ts")}
        for m in result.get("messages", [])
    ]}


async def send_slack_message(channel_id: str, text: str):
    if not SLACK_TOKEN:
        return {"error": "Slack 미설정"}
    result = await slack_api_call("chat.postMessage", {"channel": channel_id, "text": text})
    return {"success": result.get("ok"), "error": result.get("error")}


async def list_slack_channels():
    if not SLACK_TOKEN:
        return {"error": "Slack 미설정"}
    result = await slack_api_call("conversations.list", None)
    return {"count": len(result.get("channels", [])), "channels": [
        {"id": c["id"], "name": c["name"], "is_member": c.get("is_member", False)}
        for c in result.get("channels", [])[:50]
    ]}


# ==================== Claude Tools ====================

CLAUDE_TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "사장님이 업로드한 문서(PDF, IR 덱, 딜 자료, 회의록 등)에서 정보 검색. 회사 내부 자료에 대한 질문은 무조건 이걸 먼저 호출.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드 또는 질문"},
                "top_k": {"type": "integer", "description": "결과 개수 (기본 5)", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "list_indexed_documents",
        "description": "지식 베이스에 등록된 문서 통계",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "list_calendars",
        "description": "접근 가능한 모든 캘린더 (본인 + 팀원 공유받은 것)",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_calendar_events",
        "description": "캘린더 일정 조회. calendar_id='primary'는 본인, 또는 팀원 이메일.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "calendar_id": {"type": "string", "default": "primary"}
            },
            "required": ["start_date", "end_date"]
        }
    },
    {
        "name": "create_calendar_event",
        "description": "본인 캘린더에 일정 추가",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_datetime": {"type": "string"},
                "end_datetime": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["title", "start_datetime", "end_datetime"]
        }
    },
    {
        "name": "list_slack_channels",
        "description": "슬랙 채널 목록",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "search_slack_channel",
        "description": "키워드로 채널 찾고 최근 메시지 조회",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_name_or_keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 30}
            },
            "required": ["channel_name_or_keyword"]
        }
    },
    {
        "name": "get_slack_channel_messages",
        "description": "특정 채널 N시간 메시지",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "hours_ago": {"type": "integer", "default": 24}
            },
            "required": ["channel_id"]
        }
    },
    {
        "name": "send_slack_message",
        "description": "슬랙 메시지 발송 (사장님 명시적 요청 시만)",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "text": {"type": "string"}
            },
            "required": ["channel_id", "text"]
        }
    }
]


async def execute_tool(tool_name: str, tool_input: dict):
    try:
        if tool_name == "search_knowledge_base":
            return search_knowledge_base(**tool_input)
        elif tool_name == "list_indexed_documents":
            return list_indexed_documents()
        elif tool_name == "list_calendars":
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


# ==================== Claude ====================

SYSTEM_PROMPT = f"""당신은 ReboundX 대표 magon님의 개인 비서 AI입니다.

오늘 날짜: {datetime.now().strftime('%Y-%m-%d (%A)')}
타임존: Asia/Seoul

회사 컨텍스트:
- 회사: ReboundX
- 제품: ReboundX (리베이트 서비스), Terminal (5/22 이후 MVP 배포 예정)
- 주요 인물: 정예진, 이승원, 권은서(Althea), magon

도구:
[지식 베이스] search_knowledge_base, list_indexed_documents
[캘린더] list_calendars, get_calendar_events, create_calendar_event
[슬랙] list_slack_channels, search_slack_channel, get_slack_channel_messages, send_slack_message

원칙:
- 회사 내부 문서·IR 덱·딜 자료 관련 질문 → search_knowledge_base 먼저
- 검색 결과 있으면 파일명과 페이지 함께 답변 (예: "IR_Deck_2026.pdf p.12에 따르면...")
- 팀원 일정은 list_calendars로 누구 공유받았나 확인 후 조회
- 슬랙 채널 ID 모를 때 list_slack_channels 먼저
- 슬랙 메시지 발송은 명시적 요청 시만

스타일: 한국어, 존댓말, 간결, 정확. 모르는 건 모른다고 답변.
"""


async def get_claude_response(user_message: str) -> str:
    conversation_history.append({"role": "user", "content": user_message})
    
    for _ in range(10):
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
            return f"⚠️ Claude 에러: {str(e)[:200]}"
        
        conversation_history.append({
            "role": "assistant",
            "content": [block.model_dump() for block in response.content]
        })
        
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"[tool] {block.name}")
                    result = await execute_tool(block.name, dict(block.input))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)[:8000]
                    })
            conversation_history.append({"role": "user", "content": tool_results})
        else:
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks) if text_blocks else "응답 비어있음 🤔"
    
    return "⚠️ 도구 호출 너무 많음"


# ==================== Telegram ====================

async def send_telegram_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})


async def download_telegram_file(file_id: str) -> tuple:
    """텔레그램에서 파일 다운로드"""
    async with httpx.AsyncClient(timeout=60) as client:
        # 파일 경로 받기
        info = await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}
        )
        file_path = info.json()["result"]["file_path"]
        
        # 파일 내용 다운로드
        resp = await client.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )
        return file_path.split("/")[-1], resp.content


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "message" not in data:
        return {"ok": True}
    
    message = data["message"]
    chat_id = message["chat"]["id"]
    
    if chat_id != AUTHORIZED_CHAT_ID:
        return {"ok": True}
    
    # 파일 업로드 처리
    if "document" in message:
        doc = message["document"]
        file_id = doc["file_id"]
        filename = doc.get("file_name", "unknown")
        
        await send_telegram_message(chat_id, f"📥 *{filename}* 받았어요. 처리 중...")
        
        try:
            real_filename, file_bytes = await download_telegram_file(file_id)
            # 사용자가 보낸 원래 파일명 우선
            result = await index_document(filename, file_bytes)
            
            if result.get("success"):
                await send_telegram_message(chat_id, 
                    f"✅ *{filename}* 인덱싱 완료\n"
                    f"📄 {result['pages']}페이지 → {result['chunks']}개 청크\n\n"
                    f"이제 이 문서에 대해 질문할 수 있어요.")
            else:
                await send_telegram_message(chat_id, f"❌ 실패: {result.get('error')}")
        except Exception as e:
            await send_telegram_message(chat_id, f"❌ 처리 에러: {str(e)[:200]}")
        return {"ok": True}
    
    text = message.get("text", "")
    
    if text.strip() == "/reset":
        conversation_history.clear()
        await send_telegram_message(chat_id, "🔄 대화 기억 초기화")
        return {"ok": True}
    
    if text.strip() == "/help":
        await send_telegram_message(chat_id,
            "*기능*\n"
            "📅 캘린더 조회/추가\n"
            "💬 슬랙 채널 조회/발송\n"
            "📚 PDF/PPTX/DOCX 업로드 → 지식 베이스 추가\n"
            "🔍 업로드한 문서 검색\n\n"
            "*명령어*\n/reset - 기억 초기화\n/auth - Google 인증")
        return {"ok": True}
    
    if text.strip() == "/auth":
        await send_telegram_message(chat_id, f"https://{RAILWAY_URL}/auth/google")
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
        access_type='offline', prompt='consent', include_granted_scopes='true'
    )
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def auth_google_callback(code: str):
    global _oauth_flow_instance
    if _oauth_flow_instance is None:
        return HTMLResponse("<h1>먼저 /auth/google 방문</h1>")
    _oauth_flow_instance.fetch_token(code=code)
    refresh_token = _oauth_flow_instance.credentials.refresh_token
    _oauth_flow_instance = None
    return HTMLResponse(f"<h1>✅ 인증 성공</h1><pre>{refresh_token}</pre>")


@app.get("/")
def root():
    return {
        "status": "running",
        "google": bool(GOOGLE_REFRESH_TOKEN),
        "slack": bool(SLACK_TOKEN),
        "pinecone": bool(pinecone_index),
        "voyage": bool(voyage_client),
        "history": len(conversation_history)
    }
