"""Per-profile conversation store (JSON files under <profile>/sessions/)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from .paths import chmod_private, safe_child_name

_locks: dict[str, asyncio.Lock] = {}


def derive_session_id(system: str, first_user: str, profile: str) -> str:
    """Stable id from the conversation's opening (same idea as Hermes' api-<sha16>), namespaced by
    profile, so stateless OpenAI clients that resend the whole history map onto one stored session."""
    seed = f"{profile}\0{system}\n{first_user}"
    return "api-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def lock_for(profile: str, sid: str) -> asyncio.Lock:
    return _locks.setdefault(f"{profile}/{sid}", asyncio.Lock())


class SessionStore:
    def __init__(self, sessions_dir: Path):
        self.dir = sessions_dir

    def _path(self, sid: str) -> Path:
        return self.dir / f"{safe_child_name(sid, 'session id')}.json"

    def load(self, sid: str) -> dict | None:
        try:
            return json.loads(self._path(sid).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def save(self, sid: str, messages: list[dict], title: str | None = None) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        cur = self.load(sid) or {"id": sid, "created_at": int(time.time())}
        cur.update(messages=messages, updated_at=int(time.time()), message_count=len(messages))
        if title and not cur.get("title"):
            cur["title"] = title[:80]
        p = self._path(sid)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        chmod_private(tmp)
        tmp.replace(p)
        return cur

    def delete(self, sid: str) -> bool:
        try:
            self._path(sid).unlink()
            return True
        except FileNotFoundError:
            return False

    def list(self) -> list[dict]:
        out = []
        if not self.dir.is_dir():
            return out
        for f in self.dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                out.append({k: d.get(k) for k in ("id", "title", "created_at", "updated_at", "message_count")})
            except Exception:
                continue
        return sorted(out, key=lambda d: d.get("updated_at") or 0, reverse=True)
