"""
OpenAI-compatible FastAPI server for DeepSeek.

Point any OpenAI client at http://localhost:8000/v1 :

    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")
    r = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "Hello!"}],
    )

Endpoints:
    GET  /v1/models
    POST /v1/chat/completions   (stream=true supported)
    GET  /healthz

Requests under /v1 are rate limited per client IP (default 30/min, set via
RATE_LIMIT_PER_MINUTE); /healthz is exempt.
"""

from __future__ import annotations

import threading
import time
import os
import zipfile
import shutil
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from deepseek.auth import LoginRequired, Session, DEFAULT_SESSION_FILE
from deepseek.client import DeepSeekClient

from .config import (
    MODEL_MAP,
    RATE_LIMIT_PER_MINUTE,
    SERVER_INTERACTIVE_LOGIN,
    CLEANUP_EPHEMERAL_CHATS,
    is_known_model,
    resolve_model_type,
    resolve_virtual_model,
)
from .openai_format import completion_response, messages_to_prompt, stream_chunks
from .ratelimit import RateLimiter, install_rate_limit
from .schemas import ChatCompletionRequest

load_dotenv()

app = FastAPI(title="DeepSeek OpenAI-compatible API", version="0.1.0")
install_rate_limit(app, RateLimiter(limit=RATE_LIMIT_PER_MINUTE, window=60.0))

# One shared client (and its signed-in session) built lazily on first use.
_client: DeepSeekClient | None = None
_client_lock = threading.Lock()

# Track active in-flight chat completion requests
_active_completions = 0
_active_completions_lock = threading.Lock()

_playwright_ready: bool | None = None


def is_playwright_ready() -> bool:
    """Check if Playwright is installed and has the Chromium browser executable ready."""
    global _playwright_ready
    if _playwright_ready is True:
        return True
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exec_path = Path(p.chromium.executable_path)
            if exec_path.exists():
                _playwright_ready = True
                return True
    except Exception:
        pass
    return False


def get_client() -> DeepSeekClient:
    """Build (once) the shared client and its signed-in session.

    Session resolution: cached file → headless capture off the persistent
    profile. If neither works and SERVER_INTERACTIVE_LOGIN is on (the default),
    it opens a visible browser window so you can sign in — the triggering
    request blocks until you finish. If interactive login is off, it raises
    `LoginRequired`, which the endpoint turns into an actionable 503.

    This touches Playwright's sync API, so callers must invoke it OFF the event
    loop (via run_in_threadpool); calling it inside the asyncio loop raises
    "Playwright Sync API inside the asyncio loop"."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = DeepSeekClient(allow_interactive=SERVER_INTERACTIVE_LOGIN)
    return _client


def _error(message: str, status: int = 500, err_type: str = "server_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type}},
    )


security_scheme = HTTPBearer(auto_error=False)
import_key_scheme = APIKeyHeader(name="X-Import-Key", auto_error=False)


def check_api_key(token: HTTPAuthorizationCredentials | None) -> JSONResponse | None:
    """Validate that the request has a valid Authorization: Bearer <API_KEY> header
    if SERVER_API_KEY is configured in the environment."""
    api_key = os.getenv("SERVER_API_KEY")
    if not api_key:
        return None

    if not token or not token.credentials:
        return _error(
            "Missing or invalid Authorization header. Expected 'Bearer <API_KEY>'.",
            status=401,
            err_type="invalid_request_error"
        )

    if token.credentials != api_key:
        return _error(
            "Incorrect API key provided.",
            status=401,
            err_type="invalid_request_error"
        )
    return None


@app.get("/healthz")
@app.get("/health")
def healthz(token: HTTPAuthorizationCredentials | None = Security(security_scheme)):
    from deepseek.auth import SESSION_MAX_AGE
    
    is_authorized = True
    if os.getenv("SERVER_API_KEY"):
        err = check_api_key(token)
        if err:
            is_authorized = False

    cached_session = Session.load(DEFAULT_SESSION_FILE)
    status = "ok"
    session_data = None

    root = Path(__file__).resolve().parent.parent
    profile_dir = root / "session" / "profile"
    wasm_path = root / "deepseek" / "sha3_wasm_bg.wasm"

    if cached_session:
        is_expired = cached_session.age >= SESSION_MAX_AGE
        if is_expired:
            status = "warning"
        
        remaining = max(0.0, SESSION_MAX_AGE - cached_session.age)
        captured_dt = datetime.fromtimestamp(cached_session.captured_at, tz=timezone.utc)
        expires_dt = datetime.fromtimestamp(cached_session.captured_at + SESSION_MAX_AGE, tz=timezone.utc)
        
        if is_authorized:
            session_data = {
                "loaded": True,
                "age_seconds": round(cached_session.age, 1),
                "max_age_seconds": SESSION_MAX_AGE,
                "remaining_seconds": round(remaining, 1),
                "expired": is_expired,
                "captured_at_iso": captured_dt.isoformat(),
                "expires_at_iso": expires_dt.isoformat(),
                "token_preview": f"{cached_session.token[:8]}..." if cached_session.token else None,
                "cookies_count": len(cached_session.cookies) if cached_session.cookies else 0,
                "user_agent": cached_session.user_agent
            }
        else:
            session_data = {
                "loaded": True,
                "expired": is_expired,
                "remaining_seconds": round(remaining, 1)
            }
    else:
        status = "error"
        session_data = {
            "loaded": False,
            "message": "No session found on disk."
        }

    system_data = None
    if is_authorized:
        system_data = {
            "browser_profile_exists": profile_dir.exists(),
            "pow_wasm_exists": wasm_path.exists(),
            "playwright_browser_installed": is_playwright_ready(),
            "active_completions": _active_completions
        }

    return {
        "status": status,
        "session": session_data,
        "system": system_data
    }


@app.get("/v1/models")
def list_models(token: HTTPAuthorizationCredentials | None = Security(security_scheme)):
    err = check_api_key(token)
    if err:
        return err
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created, "owned_by": "deepseek"}
            for name in MODEL_MAP
        ],
    }


@app.post("/v1/session/import")
async def import_session(
    file: UploadFile = File(...),
    x_import_key: str | None = Security(import_key_scheme)
):
    import_key = os.getenv("SESSION_IMPORT_KEY") or os.getenv("SERVER_API_KEY")
    if import_key and x_import_key != import_key:
        return _error("Forbidden: Invalid X-Import-Key", status=403, err_type="forbidden")

    root = Path(__file__).resolve().parent.parent
    session_dir = root / "session"

    filename = file.filename or ""
    if not (filename.endswith(".zip") or filename.endswith(".json")):
        return _error("Unsupported file format. Please upload a .zip or .json file.", status=400, err_type="invalid_request_error")

    try:
        if filename.endswith(".json"):
            session_dir.mkdir(parents=True, exist_ok=True)
            session_file = session_dir / "session.json"
            content = await file.read()
            try:
                json.loads(content)
            except Exception:
                return _error("Invalid JSON content", status=400, err_type="invalid_request_error")
            session_file.write_bytes(content)
        else:
            temp_zip_path = root / "temp_session.zip"
            with open(temp_zip_path, "wb") as f:
                shutil.copyfileobj(file.file, f)

            try:
                with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
                    for member in zip_ref.namelist():
                        member_path = Path(member)
                        if member_path.is_absolute() or ".." in member_path.parts:
                            return _error(f"Security error: Invalid path in ZIP: {member}", status=400, err_type="invalid_request_error")

                    if session_dir.exists():
                        shutil.rmtree(session_dir)
                    session_dir.mkdir(parents=True, exist_ok=True)
                    zip_ref.extractall(session_dir)
            finally:
                if temp_zip_path.exists():
                    os.remove(temp_zip_path)

        global _client
        with _client_lock:
            _client = None

        return {"status": "success", "message": "Session successfully imported"}
    except Exception as e:
        return _error(f"Failed to import session: {str(e)}", status=500, err_type="server_error")


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    token: HTTPAuthorizationCredentials | None = Security(security_scheme)
):
    err = check_api_key(token)
    if err:
        return err
    if not req.messages:
        return _error("`messages` must not be empty", status=400, err_type="invalid_request_error")

    if not is_known_model(req.model):
        return _error(
            f"The model `{req.model}` does not exist. Available models: "
            f"{', '.join(MODEL_MAP)}",
            status=404, err_type="model_not_found",
        )

    global _active_completions
    with _active_completions_lock:
        _active_completions += 1

    try:
        # Resolve virtual model settings
        model_config = resolve_virtual_model(req.model)
        model_type = None if req.conversation_id else model_config["model_type"]
        thinking_enabled = model_config["thinking"] or bool(req.thinking)
        search_enabled = model_config["search"] or bool(req.search)

        prompt = messages_to_prompt(req.messages)

        try:
            # Off the event loop: get_client() uses Playwright's sync API, which
            # errors if run inside the asyncio loop.
            client = await run_in_threadpool(get_client)
        except LoginRequired as e:
            return _error(str(e), status=503, err_type="login_required")
        except Exception as e:  # session/login failure
            return _error(f"Failed to initialise DeepSeek session: {e}")

        if req.stream:
            def gen():
                global _active_completions
                stream = None
                try:
                    stream = client.stream(
                        prompt, conversation_id=req.conversation_id,
                        model=model_type, thinking=thinking_enabled, search=search_enabled,
                    )
                    yield from stream_chunks(req.model, stream)
                finally:
                    if CLEANUP_EPHEMERAL_CHATS and not req.conversation_id and stream is not None:
                        try:
                            client.delete_chat_session(stream.session_id)
                        except Exception as e:
                            print(f"[server] Warning: Failed to delete ephemeral session: {e}")
                    with _active_completions_lock:
                        _active_completions -= 1

            return StreamingResponse(gen(), media_type="text/event-stream")

        try:
            reply = await run_in_threadpool(
                client.chat,
                prompt,
                conversation_id=req.conversation_id,
                model=model_type,
                thinking=thinking_enabled,
                search=search_enabled,
            )
            if CLEANUP_EPHEMERAL_CHATS and not req.conversation_id:
                try:
                    session_id, _, _ = reply.conversation_id.partition(":")
                    await run_in_threadpool(client.delete_chat_session, session_id)
                except Exception as e:
                    print(f"[server] Warning: Failed to delete ephemeral session: {e}")
        except Exception as e:
            return _error(f"DeepSeek request failed: {e}")

        return completion_response(
            req.model,
            reply.text,
            prompt,
            reply.conversation_id,
            reasoning_content=reply.thinking_text,
        )
    finally:
        if not req.stream:
            with _active_completions_lock:
                _active_completions -= 1
