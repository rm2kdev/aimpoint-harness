"""ChatGPT Plus/Pro subscription via the Codex backend (Responses API at chatgpt.com/backend-api/codex)."""
from __future__ import annotations

import json
import uuid

import httpx

from .. import __version__, auth
from . import OnText, ProviderError, Turn, raise_for_status, sse_events, text_of


def _user_parts(content) -> list[dict]:
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append({"type": "input_text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"] if isinstance(part.get("image_url"), dict) else part.get("image_url", "")
                out.append({"type": "input_image", "image_url": url})
        return out or [{"type": "input_text", "text": ""}]
    return [{"type": "input_text", "text": text_of(content)}]


def to_responses_input(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            items.append({"type": "message", "role": "user", "content": _user_parts(m.get("content"))})
        elif role == "assistant":
            if text_of(m.get("content")):
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text_of(m.get("content"))}]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                items.append({"type": "function_call", "call_id": tc["id"], "name": fn.get("name", ""),
                              "arguments": fn.get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                          "output": text_of(m.get("content")) or "(no output)"})
    return items


class CodexProvider:
    def __init__(self, profile):
        self.p = profile

    async def _headers(self, session_id: str, force_refresh: bool) -> dict:
        token = await auth.get_access_token("codex", self.p.api_key, force_refresh=force_refresh)
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream",
             "originator": self.p.get("AIMPOINT_CODEX_ORIGINATOR", "aimpoint_harness"),
             "User-Agent": f"AimpointHarness/{__version__}", "session_id": session_id}
        h.update(auth.codex_account_headers(token))
        return h

    async def complete(self, system: str, messages: list[dict], tools: list[dict], on_text: OnText,
                       model: str | None = None) -> Turn:
        p = self.p
        body: dict = {"model": model or p.model, "instructions": system or "You are a helpful assistant.",
                      "input": to_responses_input(messages), "stream": True, "store": False}
        if tools:
            body["tools"] = [{"type": "function", "name": t["function"]["name"],
                              "description": t["function"].get("description", ""),
                              "parameters": t["function"].get("parameters") or {"type": "object", "properties": {}},
                              "strict": False} for t in tools]
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        if p.get("AIMPOINT_REASONING_EFFORT"):
            body["reasoning"] = {"effort": p.get("AIMPOINT_REASONING_EFFORT")}
        sid = str(uuid.uuid4())
        for attempt in (0, 1):
            try:
                return await self._stream(body, await self._headers(sid, attempt == 1), on_text)
            except ProviderError as e:
                if e.status == 401 and attempt == 0 and auth.load_store().get("codex"):
                    continue
                raise
        raise ProviderError("unreachable")

    async def _stream(self, body: dict, headers: dict, on_text: OnText) -> Turn:
        p = self.p
        turn = Turn()
        timeout = httpx.Timeout(connect=20, read=float(p.get_int("AIMPOINT_READ_TIMEOUT", 600)), write=60, pool=20)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{p.base_url}/responses", json=body, headers=headers) as resp:
                await raise_for_status(resp, "codex")
                async for event, data in sse_events(resp):
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type", event)
                    if t == "response.output_text.delta":
                        turn.content += ev.get("delta", "")
                        if on_text and ev.get("delta"):
                            await on_text(ev["delta"])
                    elif t == "response.output_item.done":
                        item = ev.get("item", {})
                        if item.get("type") == "function_call":
                            turn.tool_calls.append({"id": item.get("call_id") or item.get("id"),
                                                    "name": item.get("name", ""),
                                                    "arguments": item.get("arguments") or "{}"})
                    elif t in ("response.completed", "response.incomplete"):
                        u = (ev.get("response") or {}).get("usage") or {}
                        turn.usage = {"prompt_tokens": u.get("input_tokens", 0),
                                      "completion_tokens": u.get("output_tokens", 0)}
                        if t == "response.incomplete":
                            turn.finish_reason = "length"
                    elif t in ("response.failed", "error"):
                        err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                        raise ProviderError(f"codex: {err}")
        if turn.tool_calls:
            turn.finish_reason = "tool_calls"
        return turn
