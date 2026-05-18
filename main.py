import os
import re
import json
import io
import time
from datetime import datetime
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
import httpx
from anthropic import AsyncAnthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
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
GOOGLE_DRIVE_KB_FOLDER_ID = _clean(os.getenv("GOOGLE_DRIVE_KB_FOLDER_ID"))
SLACK_TOKEN = _clean(os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_USER_TOKEN"))
PINECONE_API_KEY = _clean(os.getenv("PINECONE_API_KEY"))
PINECONE_INDEX_NAME = _clean(os.getenv("PINECONE_INDEX_NAME")) or "knowledge-base"
VOYAGE_API_KEY = _clean(os.getenv("VOYAGE_API_KEY"))
RAILWAY_URL = _clean(os.getenv("RAILWAY_PUBLIC_DOMAIN") or "web-production-87eec.up.railway.app")

REDIRECT_URI = f"https://{RAILWAY_URL}/auth/google/callback"
GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/drive.readonly'
]

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
conversation_history = deque(maxlen=20)

# Pinecone + Voyage 초기화
pc = Pinecone(api_key=PINECONE_API_KEY) if PINECONE_API_KEY else None
pinecone_index = pc.Index(PINECONE_INDEX_NAME) if pc else None
voyage_client = voyageai.Client(api_key=VOYAGE_API_KEY) if VOYAGE_API_KEY else None

# 이미 인덱싱한 파일 추적 (메모리 - 재배포되면 초기화됨)
indexed_files_cache = set()


# ==================== 텍스트 추출 ====================

def extract_text_from_pdf(file_bytes: bytes) -> list:
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if text.strip():
            pages.append({"page": i + 1, "text": text})
    return pages


def extract_text_from_pptx(file_bytes: bytes) -> list:
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
    elif name_lower.endswith(".txt") or name_lower.endswith(".md"):
        text = file_bytes.decode('utf-8', errors='ignore')
        chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
        return [{"page": i+1, "text": c} for i, c in enumerate(chunks)]
    else:
        return []


# ==================== RAG ====================

def chunk_text(text: str, max_chars: int = 1500) -> list:
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
    if not voyage_client:
        return []
    result = voyage_client.embed(texts=texts, model="voyage-3", input_type="document")
    return result.embeddings


async def index_document(filename: str, file_bytes: bytes, source: str = "telegram") -> dict:
    if not pinecone_index or not voyage_client:
        return {"error": "Pinecone 또는 Voyage 미설정"}
    
    pages = extract_text(filename, file_bytes)
    if not pages:
        return {"error": f"지원 안 하거나 빈 파일: {filename}"}
    
    vectors_to_upsert = []
    chunk_count = 0
    
    for page_info in pages:
        page_num = page_info["page"]
        text = page_info["text"]
        chunks = chunk_text(text, max_chars=1500)
        if not chunks:
            continue
        
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
                    "source": source,
                    "indexed_at": datetime.now().isoformat()
                }
            })
            chunk_count += 1
    
    for i in range(0, len(vectors_to_upsert), 100):
        batch = vectors_to_upsert[i:i+100]
        pinecone_index.upsert(vectors=batch)
    
    return {"success": True, "filename": filename, "pages": len(pages), "chunks": chunk_count}


def search_knowledge_base(query: str, top_k: int = 5) -> dict:
    if not pinecone_index or not voyage_client:
        return {"error": "Pinecone 또는 Voyage 미설정"}
    
    query_emb = voyage_client.embed(texts=[query], model="voyage-3", input_type="query").embeddings[0]
    results = pinecone_index.query(vector=query_emb, top_k=top_k, include_metadata=True)
    
    return {"count": len(results.matches), "matches": [{
        "filename": m.metadata.get("filename", ""),
        "page": m.metadata.get("page", 0),
        "score": round(m.score, 3),
        "text": m.metadata.get("text", ""),
        "source": m.metadata.get("source", "")
    } for m in results.matches]}


def list_indexed_documents() -> dict:
    if not pinecone_index:
        return {"error": "Pinecone 미설정"}
    stats = pinecone_index.describe_index_stats()
    return {"total_chunks": stats.get("total_vector_count", 0)}


# ==================== Google Drive 자동 동기화 ====================

def get_drive_service():
    creds = get_google_credentials()
    if not creds:
        return None
    return build('drive', 'v3', credentials=creds)


async def sync_drive_folder():
    """KB 폴더의 새 파일을 자동 인덱싱"""
    if not GOOGLE_DRIVE_KB_FOLDER_ID:
        return {"error": "GOOGLE_DRIVE_KB_FOLDER_ID 미설정"}
    
    service = get_drive_service()
    if not service:
        return {"error": "Drive 인증 안 됨"}
    
    # 폴더 내 파일 목록
    query = f"'{GOOGLE_DRIVE_KB_FOLDER_ID}' in parents and trashed=false"
    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType, size, modifiedTime)",
        pageSize=100
    ).execute()
    
    files = results.get('files', [])
    indexed = []
    skipped = []
    failed = []
    
    for f in files:
        file_id = f['id']
        filename = f['name']
        
        # 이미 처리한 파일은 스킵
        cache_key = f"drive::{file_id}::{f.get('modifiedTime', '')}"
        if cache_key in indexed_files_cache:
            skipped.append(filename)
            continue
        
        # 지원하는 확장자만
        if not filename.lower().endswith(('.pdf', '.pptx', '.docx', '.txt', '.md')):
            skipped.append(f"{filename} (지원 안 함)")
            continue
        
        try:
            # 다운로드
            request = service.files().get_media(fileId=file_id)
            file_bytes = io.BytesIO()
            downloader = MediaIoBaseDownload(file_bytes, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            
            # 인덱싱
            result = await index_document(filename, file_bytes.getvalue(), source="drive")
            if result.get("success"):
                indexed.append(f"{filename} ({result['pages']}p, {result['chunks']}c)")
                indexed_files_cache.add(cache_key)
            else:
                failed.append(f"{filename}: {result.get('error')}")
        except Exception as e:
            failed.append(f"{filename}: {str(e)[:100]}")
    
    return {
        "indexed_count": len(indexed),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "indexed": indexed,
        "failed": failed[:5]  # 처음 5개만
    }


# ==================== Slack 파일 처리 ====================

async def index_slack_file(file_id: str) -> dict:
    """슬랙 파일을 받아서 인덱싱"""
    if not SLACK_TOKEN:
        return {"error": "Slack 토큰 없음"}
    
    # 파일 정보 조회
    file_info = await slack_api_call("files.info", {"file": file_id})
    if not file_info.get("ok"):
        return {"error": f"파일 정보 조회 실패: {file_info.get('error')}"}
    
    file_data = file_info["file"]
    filename = file_data.get("name", "unknown")
    download_url = file_data.get("url_private_download") or file_data.get("url_private")
    
    if not download_url:
        return {"error": "다운로드 URL 없음"}
    
    # 다운로드 (Bearer 토큰 필요)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(
            download_url,
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"}
        )
        if resp.status_code != 200:
            return {"error": f"다운로드 실패: HTTP {resp.status_code}"}
        file_bytes = resp.content
    
    return await index_document(filename, file_bytes, source="slack")


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
        return {"error": "매칭 채널 없음", "available_channels": [c["name"] for c in channels[:20]]}
    results = {}
    for ch in matching[:3]:
        history = await slack_api_call("conversations.history", {"channel": ch["id"], "limit": limit})
        results[ch["name"]] = [{"user": m.get("user"), "text": m.get("text", "")[:500], "ts": m.get("ts")}
                                for m in history.get("messages", [])]
    return {"channels_found": list(results.keys()), "messages": results}


async def get_slack_channel_messages(channel_id: str, hours_ago: int = 24):
    if not SLACK_TOKEN:
        return {"error": "Slack 미설정"}
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
        "description": "사장님이 업로드한 문서(PDF, IR 덱, 딜 자료, 회의록 등)에서 정보 검색. 회사 내부 자료 관련 질문은 무조건 이걸 먼저.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "list_indexed_documents",
        "description": "지식 베이스 통계 (총 청크 수)",
