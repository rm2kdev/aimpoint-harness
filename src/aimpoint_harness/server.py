"""HTTP API, wire-compatible with the Hermes API server.

    GET  /health, /v1/health                    liveness (no auth)
    GET  /v1/models                             the profile's model
    GET  /v1/capabilities                       machine-readable surface (probe the key with this)
    GET  /v1/skills                             the profile's skills
    GET  /v1/toolsets                           the profile's MCP servers + tools
    POST /v1/chat/completions                   OpenAI Chat Completions (stream or not)
    GET  /api/sessions[/{id}[/messages]]        the profile's stored conversations
    DELETE /api/sessions/{id}
    GET  /api/profiles                          (Aimpoint extension) profiles on this host

Every route is mirrored under /p/<profile>/...; un-prefixed routes use the default profile.
Sessions: send `X-Hermes-Session-Id` (or `X-Aimpoint-Session-Id`) to keep history server-side;
without it the body's `messages` is the history (plain OpenAI semantics) and the conversation is
stored under an id derived from its opening, returned in the `X-Hermes-Session-Id` header.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, paths
from . import skills as skills_mod
from .agent import Runtimes, run_agent
from .auth import AuthError
from .envfile import read_env
from .profiles import list_profiles, load_profile, profile_exists
from .providers import ProviderError, text_of
from .sessions import derive_session_id, lock_for

log = logging.getLogger("aimpoint.server")
SESSION_HEADERS = ("X-Hermes-Session-Id", "X-Aimpoint-Session-Id")


def _err(message: str, status: int, code: str = "error", type_: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, "code": code}}, status_code=status)


def global_settings() -> dict[str, str]:
    import os
    env = dict(os.environ)
    env.update(read_env(paths.global_env_path()))
    return env


def default_profile_name() -> str:
    return global_settings().get("AIMPOINT_DEFAULT_PROFILE") or paths.DEFAULT_PROFILE


def _presented_key(request: Request) -> str:
    raw = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()


def _profile_from(request: Request) -> str | Response:
    name = request.path_params.get("profile") or default_profile_name()
    name = paths.normalize_profile_name(name)
    if not profile_exists(name):
        return _err("Unknown or unconfigured profile", 404, "profile_not_found")
    return name


def _check_auth(request: Request, profile: str | None) -> Response | None:
    """A profile's own API_SERVER_KEY, when set, is the only key it accepts; otherwise the global one."""
    expected = ""
    if profile:
        expected = load_profile(profile).server_key
    if not expected:
        expected = global_settings().get("API_SERVER_KEY", "")
    if not expected:
        return _err("No API_SERVER_KEY configured. Run: aimpoint key generate", 401, "no_key_configured",
                    "authentication_error")
    if not hmac.compare_digest(_presented_key(request).encode(), expected.encode()):
        return _err("Invalid API key", 401, "invalid_api_key", "authentication_error")
    return None


def _guard(handler):
    async def wrapped(request: Request):
        prof = _profile_from(request)
        if isinstance(prof, Response):
            return prof
        denied = _check_auth(request, prof)
        if denied:
            return denied
        try:
            return await handler(request, prof)
        except FileNotFoundError as e:
            return _err(str(e), 404, "not_found")
        except ValueError as e:
            return _err(str(e), 400, "invalid_request")
    return wrapped


# ── handlers ──
async def health(request: Request):
    return JSONResponse({"status": "ok", "platform": "aimpoint-harness", "version": __version__})


async def models(request: Request, profile: str):
    p = load_profile(profile)
    now = int(time.time())
    return JSONResponse({"object": "list", "data": [
        {"id": p.model, "object": "model", "created": now, "owned_by": "aimpoint", "permission": [],
         "root": p.model, "parent": None, "profile": p.name, "provider": p.provider}]})


async def capabilities(request: Request, profile: str):
    p = load_profile(profile)
    prefix = "" if not request.path_params.get("profile") else f"/p/{profile}"
    endpoints = {
        "health": ("GET", "/health"), "models": ("GET", "/v1/models"),
        "chat_completions": ("POST", "/v1/chat/completions"), "skills": ("GET", "/v1/skills"),
        "toolsets": ("GET", "/v1/toolsets"), "sessions": ("GET", "/api/sessions"),
        "session": ("GET", "/api/sessions/{session_id}"),
        "session_messages": ("GET", "/api/sessions/{session_id}/messages"),
        "session_delete": ("DELETE", "/api/sessions/{session_id}")}
    return JSONResponse({
        "object": "hermes.api_server.capabilities", "platform": "aimpoint-harness", "version": __version__,
        "profile": p.name, "model": p.model, "provider": p.provider,
        "auth": {"type": "bearer", "required": True},
        "runtime": {"mode": "server_agent", "tool_execution": "server",
                    "description": "Tools (MCP servers, skills) execute on the harness host; client-supplied "
                                   "function tools are handed back as OpenAI tool_calls."},
        "features": {"chat_completions": True, "chat_completions_streaming": True, "responses_api": False,
                     "run_submission": False, "session_continuity_header": "X-Hermes-Session-Id",
                     "client_tools": True, "profiles": True},
        "endpoints": {k: {"method": m, "path": prefix + path} for k, (m, path) in endpoints.items()}})


async def list_skills(request: Request, profile: str):
    p = load_profile(profile)
    return JSONResponse({"object": "list", "data": [
        {"name": s.name, "description": s.description} for s in skills_mod.discover(p.skills_dir)]})


async def toolsets(request: Request, profile: str):
    rt = await request.app.state.runtimes.get(profile)
    return JSONResponse({"object": "list", "data": [
        {"name": name, **st} for name, st in rt.pool.status().items()]})


async def sessions_list(request: Request, profile: str):
    rt = await request.app.state.runtimes.get(profile)
    return JSONResponse({"object": "list", "data": rt.store.list()})


async def session_get(request: Request, profile: str):
    rt = await request.app.state.runtimes.get(profile)
    s = rt.store.load(request.path_params["session_id"])
    if not s:
        return _err("Session not found", 404, "session_not_found")
    if request.url.path.endswith("/messages"):
        return JSONResponse({"object": "list", "session_id": s["id"], "data": s.get("messages", [])})
    return JSONResponse({k: v for k, v in s.items() if k != "messages"})


async def session_delete(request: Request, profile: str):
    rt = await request.app.state.runtimes.get(profile)
    if not rt.store.delete(request.path_params["session_id"]):
        return _err("Session not found", 404, "session_not_found")
    return JSONResponse({"deleted": True, "id": request.path_params["session_id"]})


async def profiles_list(request: Request):
    denied = _check_auth(request, None)
    if denied:
        return denied
    out = []
    for name in list_profiles():
        p = load_profile(name)
        out.append({"name": name, "provider": p.provider, "model": p.model,
                    "base_path": "" if name == default_profile_name() else f"/p/{name}",
                    "own_key": bool(p.server_key)})
    return JSONResponse({"object": "list", "data": out})


def _split_messages(messages: list) -> tuple[str, list[dict]]:
    system, conv = [], []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "developer", "user", "assistant", "tool"):
            raise ValueError("Each message needs a role of system, developer, user, assistant or tool")
        if m["role"] in ("system", "developer"):
            system.append(text_of(m.get("content")))
        else:
            conv.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")})
    return "\n\n".join(s for s in system if s), conv


def _trailing_new(conv: list[dict]) -> list[dict]:
    """Messages after the last assistant turn: the new user message and/or client tool results."""
    for i in range(len(conv) - 1, -1, -1):
        if conv[i]["role"] == "assistant":
            return conv[i + 1:]
    return conv[-1:] if conv else []


async def chat_completions(request: Request, profile: str):
    try:
        body = await request.json()
    except Exception:
        return _err("Invalid JSON in request body", 400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _err("Missing or invalid 'messages' field", 400)
    client_system, conv = _split_messages(messages)
    if not conv or conv[-1]["role"] not in ("user", "tool"):
        return _err("The last message must be a user message (or tool results)", 400)

    rt = await request.app.state.runtimes.get(profile)
    p = rt.profile
    provided = next((request.headers.get(h, "").strip() for h in SESSION_HEADERS if request.headers.get(h)), "")
    if provided and (not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", provided)):
        return _err("Invalid session ID", 400, "invalid_session_id")
    first_user = next((text_of(m.get("content")) for m in conv if m["role"] == "user"), "")
    sid = provided or derive_session_id(client_system, first_user, p.name)

    requested_model = str(body.get("model") or "")
    model = None
    if p.get_bool("AIMPOINT_ALLOW_MODEL_OVERRIDE", False) and requested_model and requested_model not in (
            p.model, p.name, "hermes-agent", "aimpoint", "default"):
        model = requested_model
    model_name = model or p.model
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    tools = body.get("tools") if p.get_bool("AIMPOINT_ALLOW_CLIENT_TOOLS", True) else None
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
    created = int(time.time())
    headers = {"X-Hermes-Session-Id": sid, "X-Aimpoint-Profile": p.name}

    async def prepare() -> list[dict]:
        if provided:
            stored = rt.store.load(sid)
            if stored:
                return stored["messages"] + _trailing_new(conv)
        return conv

    async def finish(history: list[dict], result) -> None:
        rt.store.save(sid, history + result.new_messages, title=first_user)

    def error_payload(e: Exception) -> tuple[str, int, str]:
        if isinstance(e, AuthError):
            return str(e), 502, "provider_auth_error"
        if isinstance(e, ProviderError):
            return str(e), (e.status if e.status in (400, 401, 403, 404, 429) else 502), "provider_error"
        log.exception("agent run failed")
        return f"{type(e).__name__}: {e}", 500, "internal_error"

    if not stream:
        async with lock_for(p.name, sid):
            history = await prepare()
            try:
                result = await run_agent(rt, history, client_system=client_system, client_tools=tools, model=model)
            except Exception as e:  # noqa: BLE001
                msg, status, code = error_payload(e)
                return _err(msg, status, code, "api_error")
            await finish(history, result)
        message: dict = {"role": "assistant", "content": result.content}
        if result.tool_calls:
            message["tool_calls"] = result.tool_calls
        u = result.usage
        return JSONResponse({
            "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
            "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
            "usage": {"prompt_tokens": u["prompt_tokens"], "completion_tokens": u["completion_tokens"],
                      "total_tokens": u["prompt_tokens"] + u["completion_tokens"]}}, headers=headers)

    queue: asyncio.Queue = asyncio.Queue()

    def chunk(delta: dict, finish_reason=None) -> bytes:
        payload = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model_name,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def on_text(delta: str):
        await queue.put(chunk({"content": delta}))

    async def on_tool(ev: dict):
        await queue.put(f"event: hermes.tool.progress\ndata: {json.dumps(ev)}\n\n".encode())

    async def worker():
        try:
            async with lock_for(p.name, sid):
                history = await prepare()
                result = await run_agent(rt, history, client_system=client_system, client_tools=tools,
                                         model=model, on_text=on_text, on_tool=on_tool)
                await finish(history, result)
            if result.tool_calls:
                await queue.put(chunk({"tool_calls": [dict(tc, index=i) for i, tc in enumerate(result.tool_calls)]}))
            await queue.put(chunk({}, result.finish_reason))
            if include_usage:
                u = result.usage
                await queue.put(("data: " + json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model_name,
                    "choices": [], "usage": {**u, "total_tokens": u["prompt_tokens"] + u["completion_tokens"]}})
                    + "\n\n").encode())
        except Exception as e:  # noqa: BLE001
            msg, _, code = error_payload(e)
            await queue.put(f"data: {json.dumps({'error': {'message': msg, 'type': 'api_error', 'code': code}})}\n\n".encode())
        finally:
            await queue.put(b"data: [DONE]\n\n")
            await queue.put(None)

    task = asyncio.create_task(worker())  # runs to completion (and saves) even if the client disconnects

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def build_app() -> Starlette:
    table = [
        ("GET", "/v1/models", models), ("GET", "/v1/capabilities", capabilities),
        ("GET", "/v1/skills", list_skills), ("GET", "/v1/toolsets", toolsets),
        ("POST", "/v1/chat/completions", chat_completions),
        ("GET", "/api/sessions", sessions_list), ("GET", "/api/sessions/{session_id}", session_get),
        ("GET", "/api/sessions/{session_id}/messages", session_get),
        ("DELETE", "/api/sessions/{session_id}", session_delete),
    ]
    routes = [Route("/health", health), Route("/v1/health", health),
              Route("/p/{profile}/health", health), Route("/p/{profile}/v1/health", health),
              Route("/api/profiles", profiles_list)]
    for method, path, handler in table:
        g = _guard(handler)
        routes.append(Route(path, g, methods=[method]))
        routes.append(Route("/p/{profile}" + path, g, methods=[method]))

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtimes = Runtimes()
        yield
        await app.state.runtimes.close()

    return Starlette(routes=routes, lifespan=lifespan)
