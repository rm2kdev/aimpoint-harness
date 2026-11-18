"""LLM backends. Every provider takes OpenAI-style chat messages + tools and returns one assistant
turn, streaming text deltas through `on_text` as they arrive."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

OnText = Optional[Callable[[str], Awaitable[None]]]


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class Turn:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "arguments": json-str}]
    finish_reason: str = "stop"
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})

    def as_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                 for c in self.tool_calls]
        return msg


async def sse_events(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Yield (event, data) pairs from an SSE response."""
    event, data = "", []
    async for line in resp.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


async def raise_for_status(resp: httpx.Response, who: str) -> None:
    if resp.status_code >= 400:
        body = (await resp.aread()).decode(errors="replace")[:1000]
        raise ProviderError(f"{who} HTTP {resp.status_code}: {body}", status=resp.status_code)


def text_of(content) -> str:
    """Flatten OpenAI message content (str or parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return str(content)


def parse_args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {}


async def list_models(profile) -> list[str]:
    """Model ids the profile's credential can use (availability differs per plan/account)."""
    kind = profile.provider_spec["kind"]
    async with httpx.AsyncClient(timeout=30) as client:
        if kind == "openai":
            h = {"Authorization": f"Bearer {profile.api_key}"} if profile.api_key else {}
            r = await client.get(f"{profile.base_url}/models", headers=h)
            await raise_for_status(r, profile.provider)
            return sorted(m["id"] for m in r.json().get("data", []))
        if kind == "anthropic":
            from .anthropic import AnthropicProvider
            h = await AnthropicProvider(profile)._headers()
            r = await client.get(f"{profile.base_url}/v1/models?limit=100", headers=h)
            await raise_for_status(r, profile.provider)
            return [m["id"] for m in r.json().get("data", [])]
        if kind == "codex":
            from .codex import CodexProvider
            h = await CodexProvider(profile)._headers("models", False)
            r = await client.get(f"{profile.base_url}/models?client_version=99.0.0", headers=h)
            await raise_for_status(r, "codex")
            return [m.get("slug") for m in r.json().get("models", []) if m.get("slug")]
    return []


def build_provider(profile):
    kind = profile.provider_spec["kind"]
    if kind == "openai":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(profile)
    if kind == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(profile)
    if kind == "codex":
        from .codex import CodexProvider
        return CodexProvider(profile)
    raise ProviderError(f"Unsupported provider kind {kind!r}", 500)
