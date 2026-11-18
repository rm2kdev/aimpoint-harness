"""The agent loop: soul + skills + MCP tools around one provider, scoped to one profile."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from . import skills as skills_mod
from .mcp_pool import MCPPool
from .profiles import Profile, load_profile
from .providers import Turn, build_provider, parse_args
from .sessions import SessionStore

log = logging.getLogger("aimpoint.agent")
OnTool = Optional[Callable[[dict], Awaitable[None]]]


class ProfileRuntime:
    """Live state of one profile: its MCP connections, rebuilt when its files change."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.sig = profile.signature()
        self.pool = MCPPool(profile.mcp_configs(), profile.env, profile.dir / "logs")
        self.store = SessionStore(profile.sessions_dir)

    async def close(self) -> None:
        await self.pool.close()


class Runtimes:
    def __init__(self):
        self._by_name: dict[str, ProfileRuntime] = {}
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> ProfileRuntime:
        profile = load_profile(name)  # re-reads .env every request: settings apply without a restart
        async with self._lock:
            rt = self._by_name.get(profile.name)
            if rt is None or rt.sig != profile.signature():
                if rt is not None:
                    log.info("profile %s changed on disk; reconnecting MCP servers", profile.name)
                    await rt.close()
                rt = ProfileRuntime(profile)
                self._by_name[profile.name] = rt
            rt.profile = profile
        await rt.pool.ensure()
        return rt

    async def close(self) -> None:
        for rt in self._by_name.values():
            await rt.close()
        self._by_name.clear()


@dataclass
class RunResult:
    content: str
    new_messages: list[dict]
    finish_reason: str
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    tool_calls: list[dict] = field(default_factory=list)  # client-side tool calls handed back


def build_system_prompt(profile: Profile, skills: list, client_system: str) -> str:
    parts = [profile.soul()]
    idx = skills_mod.index_prompt(skills)
    if idx:
        parts.append(idx)
    parts.append(f"Current date/time: {time.strftime('%A, %B %d, %Y %H:%M %Z')}")
    if client_system.strip():
        parts.append("## Instructions from the calling application\n" + client_system.strip())
    return "\n\n".join(p for p in parts if p)


async def run_agent(rt: ProfileRuntime, history: list[dict], *, client_system: str = "",
                    client_tools: list[dict] | None = None, model: str | None = None,
                    on_text=None, on_tool: OnTool = None) -> RunResult:
    profile = rt.profile
    skills = skills_mod.discover(profile.skills_dir)
    system = build_system_prompt(profile, skills, client_system)
    tools: list[dict] = []
    if skills:
        tools.append(skills_mod.SKILL_VIEW_TOOL)
    tools += rt.pool.tool_schemas()
    server_names = {t["function"]["name"] for t in tools}
    client_tools = [t for t in (client_tools or []) if t.get("type") == "function"
                    and t.get("function", {}).get("name") not in server_names]
    client_names = {t["function"]["name"] for t in client_tools}
    tools += client_tools

    provider = build_provider(profile)
    msgs = list(history)
    start = len(msgs)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    texts: list[str] = []
    max_iter = profile.get_int("AIMPOINT_MAX_TOOL_ITERATIONS", 25)

    sep = {"pending": False}

    async def stream_text(delta: str):
        if on_text:
            if sep["pending"]:  # separate text from consecutive agent turns
                sep["pending"] = False
                await on_text("\n\n")
            await on_text(delta)

    turn = Turn()
    for _ in range(max_iter):
        sep["pending"] = bool(texts)
        turn = await provider.complete(system, msgs, tools, stream_text if on_text else None, model=model)
        for k in usage:
            usage[k] += turn.usage.get(k, 0)
        if turn.content.strip():
            texts.append(turn.content.strip())
        if not turn.tool_calls:
            msgs.append(turn.as_message())
            break
        handback = [c for c in turn.tool_calls if c["name"] in client_names]
        if handback:
            # The calling app executes these; return them OpenAI-style and stop here.
            turn.tool_calls = handback
            msgs.append(turn.as_message())
            return RunResult("\n\n".join(texts), msgs[start:], "tool_calls", usage,
                             tool_calls=turn.as_message()["tool_calls"])
        msgs.append(turn.as_message())

        async def execute(call: dict) -> dict:
            if on_tool:
                await on_tool({"tool": call["name"], "toolCallId": call["id"], "status": "running",
                               "label": call["name"]})
            args = parse_args(call["arguments"])
            if call["name"] == "skill_view":
                out = skills_mod.skill_view(skills, str(args.get("name", "")), args.get("file_path"))
            elif rt.pool.owns(call["name"]):
                out = await rt.pool.call(call["name"], args)
            else:
                out = f"Error: unknown tool {call['name']!r}"
            if on_tool:
                await on_tool({"tool": call["name"], "toolCallId": call["id"], "status": "completed"})
            return {"role": "tool", "tool_call_id": call["id"], "content": out}

        msgs.extend(await asyncio.gather(*(execute(c) for c in turn.tool_calls)))
    else:
        note = f"(stopped after {max_iter} tool iterations; raise AIMPOINT_MAX_TOOL_ITERATIONS to allow more)"
        texts.append(note)
        msgs.append({"role": "assistant", "content": note})
        return RunResult("\n\n".join(texts), msgs[start:], "length", usage)

    return RunResult("\n\n".join(texts), msgs[start:], turn.finish_reason or "stop", usage)
