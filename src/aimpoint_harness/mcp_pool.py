"""MCP client pool: one set of live MCP server connections per profile.

Each profile's `mcps/<server>.json` is one server:

    {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
     "env": {"TOKEN": "${MY_TOKEN}"}}                       # stdio transport
    {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${MCP_X_API_KEY}"}}
                                                            # streamable HTTP ("transport": "sse" for SSE)
Optional keys: enabled (bool), timeout (s, per tool call), connect_timeout (s),
tools: {"include": [...]} or {"exclude": [...]}, cwd.

`${VAR}` is resolved from the profile's .env, then the global .env, then the process env.
Stdio servers only inherit a minimal safe environment plus their own `env`.
Tools are exposed to the model as mcp__<server>__<tool>, like Hermes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from .envfile import interpolate

log = logging.getLogger("aimpoint.mcp")
_RETRY_AFTER_S = 30


def tool_wire_name(server: str, tool: str) -> str:
    clean = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", s)  # noqa: E731
    name = f"mcp__{clean(server)}__{clean(tool)}"
    if len(name) > 64:  # OpenAI/Anthropic tool-name limit
        name = name[:55] + "_" + hashlib.sha1(name.encode()).hexdigest()[:8]
    return name


def _result_to_text(result) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        t = getattr(block, "type", "")
        if t == "text":
            parts.append(block.text)
        elif t == "resource":
            res = block.resource
            parts.append(getattr(res, "text", None) or f"[resource {getattr(res, 'uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[resource link {getattr(block, 'uri', '')}]")
        elif t in ("image", "audio"):
            parts.append(f"[{t} content: {getattr(block, 'mimeType', '')}, not shown]")
        else:
            parts.append(str(block))
    if not parts and getattr(result, "structuredContent", None) is not None:
        parts.append(json.dumps(result.structuredContent))
    text = "\n".join(parts) if parts else "(no output)"
    return ("Error: " + text) if getattr(result, "isError", False) else text


class MCPServer:
    def __init__(self, name: str, cfg: dict, env: dict[str, str], log_dir: Path):
        self.name, self.raw_cfg, self.log_dir = name, cfg, log_dir
        self.cfg: dict[str, Any] = interpolate({k: v for k, v in cfg.items() if k != "tools"}, env)
        self.filters = cfg.get("tools") or {}
        self.session = None
        self.tools: list[Any] = []
        self.error: str | None = None
        self.failed_at = 0.0
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.session is not None and self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=float(self.cfg.get("connect_timeout", 60)))
        except asyncio.TimeoutError:
            self.error = "connect timed out"
            await self.stop()
        if self.error:
            self.failed_at = time.monotonic()
            log.warning("MCP server %s unavailable: %s", self.name, self.error)

    async def _transport(self, stack: AsyncExitStack):
        from mcp.client.sse import sse_client
        from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        cfg = self.cfg
        if cfg.get("url"):
            headers = {k: str(v) for k, v in (cfg.get("headers") or {}).items()}
            transport = cfg.get("transport") or ("sse" if cfg["url"].rstrip("/").endswith("/sse") else "http")
            if transport == "sse":
                read, write = await stack.enter_async_context(sse_client(cfg["url"], headers=headers))
            else:
                read, write, _ = await stack.enter_async_context(streamablehttp_client(cfg["url"], headers=headers))
            return read, write
        if not cfg.get("command"):
            raise ValueError("config needs either 'command' (stdio) or 'url' (HTTP)")
        env = {**get_default_environment(), **{k: str(v) for k, v in (cfg.get("env") or {}).items()}}
        self.log_dir.mkdir(parents=True, exist_ok=True)
        errlog = stack.enter_context(open(self.log_dir / f"mcp-{self.name}.log", "a", encoding="utf-8"))
        params = StdioServerParameters(command=cfg["command"], args=[str(a) for a in cfg.get("args") or []],
                                       env=env, cwd=cfg.get("cwd") or None)
        return await stack.enter_async_context(stdio_client(params, errlog=errlog))

    async def _run(self) -> None:
        from mcp import ClientSession
        try:
            async with AsyncExitStack() as stack:
                read, write = await self._transport(stack)
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools, cursor = [], None
                while True:
                    page = await session.list_tools(cursor) if cursor else await session.list_tools()
                    tools.extend(page.tools)
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                inc, exc = set(self.filters.get("include") or []), set(self.filters.get("exclude") or [])
                self.tools = [t for t in tools if (not inc or t.name in inc) and t.name not in exc]
                self.session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as e:  # noqa: BLE001 - report every failure, incl. ExceptionGroup
            self.error = self.error or _describe(e)
        finally:
            self.session = None
            self._ready.set()

    async def call(self, tool: str, args: dict) -> str:
        if not self.alive:
            raise RuntimeError(f"MCP server {self.name!r} is not connected ({self.error or 'stopped'})")
        timeout = timedelta(seconds=float(self.cfg.get("timeout", 120)))
        res = await self.session.call_tool(tool, args, read_timeout_seconds=timeout)
        return _result_to_text(res)

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe(x) for x in e.exceptions)
    return f"{type(e).__name__}: {e}"


class MCPPool:
    """All MCP servers of one profile."""

    def __init__(self, configs: dict[str, dict], env: dict[str, str], log_dir: Path):
        self.configs, self.env, self.log_dir = configs, env, log_dir
        self.servers: dict[str, MCPServer] = {}
        self._lock = asyncio.Lock()
        self._map: dict[str, tuple[str, str]] = {}

    async def ensure(self) -> None:
        async with self._lock:
            pending = []
            for name, cfg in self.configs.items():
                if cfg.get("enabled", True) is False or "_error" in cfg:
                    continue
                cur = self.servers.get(name)
                if cur and (cur.alive or (cur.error and time.monotonic() - cur.failed_at < _RETRY_AFTER_S)):
                    continue
                srv = MCPServer(name, cfg, self.env, self.log_dir)
                self.servers[name] = srv
                pending.append(srv.start())
            if pending:
                await asyncio.gather(*pending)

    def tool_schemas(self) -> list[dict]:
        out, self._map = [], {}
        for sname, srv in self.servers.items():
            if not srv.alive:
                continue
            for t in srv.tools:
                wire = tool_wire_name(sname, t.name)
                self._map[wire] = (sname, t.name)
                schema = t.inputSchema or {"type": "object", "properties": {}}
                if schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                out.append({"type": "function", "function": {
                    "name": wire, "description": (t.description or f"{t.name} ({sname} MCP)")[:4000],
                    "parameters": schema}})
        return out

    def owns(self, wire_name: str) -> bool:
        return wire_name in self._map

    async def call(self, wire_name: str, args: dict) -> str:
        sname, tname = self._map[wire_name]
        try:
            return await self.servers[sname].call(tname, args)
        except Exception as e:
            return f"Error calling {tname} on MCP server {sname}: {_describe(e)}"

    def status(self) -> dict[str, dict]:
        out = {}
        for name, cfg in self.configs.items():
            srv = self.servers.get(name)
            out[name] = {"enabled": cfg.get("enabled", True) is not False and "_error" not in cfg,
                         "connected": bool(srv and srv.alive),
                         "tools": [t.name for t in srv.tools] if srv and srv.alive else [],
                         "error": cfg.get("_error") or (srv.error if srv else None)}
        return out

    async def close(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.servers.values()), return_exceptions=True)
        self.servers.clear()
