"""Minimal, dependency-free .env reader/writer that preserves comments and ordering."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import chmod_private

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        return inner.encode().decode("unicode_escape") if v[0] == '"' else inner
    # strip trailing inline comment for unquoted values
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return v


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def _quote(v: str) -> str:
    if v == "" or re.search(r"[\s#\"'$\\]", v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def set_env(path: Path, key: str, value: str | None) -> None:
    """Set (or with value=None remove) one key, keeping every other line intact."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError(f"Invalid env var name {key!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    new: list[str] = []
    done = False
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) == key:
            if value is not None and not done:
                new.append(f"{key}={_quote(value)}")
                done = True
            continue
        new.append(line)
    if value is not None and not done:
        new.append(f"{key}={_quote(value)}")
    path.write_text("\n".join(new) + ("\n" if new else ""), encoding="utf-8")
    chmod_private(path)


def interpolate(value, env: dict[str, str]):
    """Expand ${VAR} in strings (recursively in lists/dicts) from env, then os.environ."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: env.get(m.group(1), os.environ.get(m.group(1), "")), value)
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    return value


def mask(value: str) -> str:
    if not value:
        return ""
    return ("*" * 4 + value[-4:]) if len(value) > 8 else "****"
