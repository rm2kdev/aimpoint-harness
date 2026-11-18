"""Anthropic Messages API: API key (`anthropic`) or Claude Pro/Max subscription OAuth (`claude-code`)."""
from __future__ import annotations

import json
import shutil
import subprocess

import httpx

from .. import auth
from . import OnText, ProviderError, Turn, parse_args, raise_for_status, sse_events, text_of

CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"
_cc_version: str | None = None


def _claude_cli_version() -> str:
    """Anthropic rejects OAuth traffic whose claude-cli version is too old; use the installed one."""
    global _cc_version
    if _cc_version is None:
        _cc_version = "2.1.74"
        exe = shutil.which("claude")
        if exe:
            try:
                out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
                if out and out[0].isdigit():
                    _cc_version = out.split()[0]
            except Exception:
                pass
    return _cc_version


def _image_block(url: str) -> dict:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _user_blocks(content) -> list[dict]:
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"] if isinstance(part.get("image_url"), dict) else part.get("image_url", "")
                out.append(_image_block(url))
        return out or [{"type": "text", "text": "(empty)"}]
    return [{"type": "text", "text": text_of(content) or "(empty)"}]


def to_anthropic(messages: list[dict]) -> list[dict]:
    out: list[dict] = []

    def push(role: str, blocks: list[dict]):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "user":
            push("user", _user_blocks(m.get("content")))
        elif role == "assistant":
            blocks = []
            if text_of(m.get("content")):
                blocks.append({"type": "text", "text": text_of(m.get("content"))})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn.get("name", ""),
                               "input": parse_args(fn.get("arguments", "{}"))})
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                           "content": text_of(m.get("content")) or "(no output)"}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]})
    return out


class AnthropicProvider:
    def __init__(self, profile):
        self.p = profile
        self.oauth = bool(profile.provider_spec.get("oauth"))

    async def _headers(self, force_refresh: bool = False) -> dict:
        h = {"anthropic-version": "2023-06-01", "content-type": "application/json", "accept": "text/event-stream"}
        if self.oauth:
            token = await auth.get_access_token("claude-code", self.p.api_key, force_refresh=force_refresh)
            h.update({"authorization": f"Bearer {token}", "anthropic-beta": _OAUTH_BETAS,
                      "user-agent": f"claude-cli/{_claude_cli_version()} (external, cli)", "x-app": "cli"})
        else:
            if not self.p.api_key:
                raise ProviderError("ANTHROPIC_API_KEY is not set (aimpoint provider set anthropic)", 401)
            h.update({"x-api-key": self.p.api_key,
                      "anthropic-beta": "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"})
        return h

    async def complete(self, system: str, messages: list[dict], tools: list[dict], on_text: OnText,
                       model: str | None = None) -> Turn:
        p = self.p
        sys_blocks = []
        if self.oauth:  # subscription traffic must identify as Claude Code
            sys_blocks.append({"type": "text", "text": CLAUDE_CODE_PREFIX})
        if system:
            sys_blocks.append({"type": "text", "text": system})
        if sys_blocks:
            sys_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        body: dict = {"model": model or p.model, "max_tokens": p.get_int("AIMPOINT_MAX_TOKENS", 16000),
                      "stream": True, "messages": to_anthropic(messages)}
        if sys_blocks:
            body["system"] = sys_blocks
        if tools:
            body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                             for t in tools]
        if p.get("AIMPOINT_TEMPERATURE"):
            body["temperature"] = float(p.get("AIMPOINT_TEMPERATURE"))

        for attempt in (0, 1):
            try:
                return await self._stream(body, await self._headers(force_refresh=attempt == 1), on_text)
            except ProviderError as e:
                if e.status == 401 and self.oauth and attempt == 0 and auth.load_store().get("claude-code"):
                    continue  # expired/revoked access token: refresh once and retry
                raise
        raise ProviderError("unreachable")

    async def _stream(self, body: dict, headers: dict, on_text: OnText) -> Turn:
        p = self.p
        turn = Turn()
        blocks: dict[int, dict] = {}
        timeout = httpx.Timeout(connect=20, read=float(p.get_int("AIMPOINT_READ_TIMEOUT", 600)), write=60, pool=20)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{p.base_url}/v1/messages", json=body, headers=headers) as resp:
                await raise_for_status(resp, p.provider)
                async for event, data in sse_events(resp):
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type", event)
                    if t == "message_start":
                        u = ev.get("message", {}).get("usage", {})
                        turn.usage["prompt_tokens"] = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                                       + u.get("cache_creation_input_tokens", 0))
                    elif t == "content_block_start":
                        blocks[ev["index"]] = dict(ev.get("content_block", {}), _json="")
                    elif t == "content_block_delta":
                        d, b = ev.get("delta", {}), blocks.get(ev.get("index"), {})
                        if d.get("type") == "text_delta":
                            turn.content += d.get("text", "")
                            if on_text and d.get("text"):
                                await on_text(d["text"])
                        elif d.get("type") == "input_json_delta":
                            b["_json"] = b.get("_json", "") + d.get("partial_json", "")
                    elif t == "message_delta":
                        if ev.get("delta", {}).get("stop_reason"):
                            turn.finish_reason = ev["delta"]["stop_reason"]
                        turn.usage["completion_tokens"] = ev.get("usage", {}).get("output_tokens", 0)
                    elif t == "error":
                        raise ProviderError(f"{p.provider}: {ev.get('error')}")
        for _, b in sorted(blocks.items()):
            if b.get("type") == "tool_use":
                turn.tool_calls.append({"id": b["id"], "name": b["name"], "arguments": b.get("_json") or "{}"})
        turn.finish_reason = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length",
                              "stop_sequence": "stop"}.get(turn.finish_reason, turn.finish_reason)
        return turn
