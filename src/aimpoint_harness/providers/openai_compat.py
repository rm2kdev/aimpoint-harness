"""OpenAI Chat Completions wire format: OpenRouter, OpenAI, Ollama, LM Studio, vLLM, LiteLLM..."""
from __future__ import annotations

import json

import httpx

from . import OnText, ProviderError, Turn, raise_for_status, sse_events


class OpenAICompatProvider:
    def __init__(self, profile):
        self.p = profile

    async def complete(self, system: str, messages: list[dict], tools: list[dict], on_text: OnText,
                       model: str | None = None) -> Turn:
        p = self.p
        body: dict = {"model": model or p.model, "stream": True, "stream_options": {"include_usage": True},
                      "messages": ([{"role": "system", "content": system}] if system else []) + messages}
        if tools:
            body["tools"] = tools
        if p.get("AIMPOINT_MAX_TOKENS"):
            body["max_tokens"] = p.get_int("AIMPOINT_MAX_TOKENS", 8192)
        if p.get("AIMPOINT_TEMPERATURE"):
            body["temperature"] = float(p.get("AIMPOINT_TEMPERATURE"))
        if p.get("AIMPOINT_REASONING_EFFORT"):
            body["reasoning_effort"] = p.get("AIMPOINT_REASONING_EFFORT")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if p.api_key:
            headers["Authorization"] = f"Bearer {p.api_key}"
        elif p.provider in ("openrouter", "openai"):
            raise ProviderError(f"{p.provider_spec['key_env']} is not set (aimpoint provider set {p.provider})", 401)
        if p.provider == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/aimpoint/aimpoint-harness",
                            "X-Title": "Aimpoint Harness"})

        turn = Turn()
        calls: dict[int, dict] = {}
        timeout = httpx.Timeout(connect=20, read=float(p.get_int("AIMPOINT_READ_TIMEOUT", 600)), write=60, pool=20)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{p.base_url}/chat/completions", json=body, headers=headers) as resp:
                await raise_for_status(resp, p.provider)
                async for _, data in sse_events(resp):
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise ProviderError(f"{p.provider}: {chunk['error']}")
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        turn.usage = {"prompt_tokens": u.get("prompt_tokens", 0) or 0,
                                      "completion_tokens": u.get("completion_tokens", 0) or 0}
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            turn.content += delta["content"]
                            if on_text:
                                await on_text(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            turn.finish_reason = choice["finish_reason"]
        turn.tool_calls = [dict(c, id=c["id"] or f"call_{i}") for i, c in sorted(calls.items()) if c["name"]]
        if turn.tool_calls:
            turn.finish_reason = "tool_calls"
        return turn
