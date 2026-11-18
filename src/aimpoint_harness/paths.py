"""Filesystem layout.

    $AIMPOINT_HOME/              (default ~/.aimpoint)
      .env                       global settings + secrets, inherited by every profile
      auth.json                  OAuth credentials for subscription providers (0600)
      profiles/<name>/
        .env                     profile settings + secrets (override the global .env)
        SOUL.md                  the profile's identity / agent instructions
        skills/<skill>/SKILL.md  skills available to this profile only
        mcps/<server>.json       MCP servers available to this profile only
        sessions/<id>.json       conversations, scoped to this profile
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_PROFILE = "default"


def home() -> Path:
    raw = os.environ.get("AIMPOINT_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".aimpoint"


def global_env_path() -> Path:
    return home() / ".env"


def auth_path() -> Path:
    return home() / "auth.json"


def profiles_root() -> Path:
    return home() / "profiles"


def normalize_profile_name(name: str) -> str:
    return (name or "").strip().lower()


def validate_profile_name(name: str) -> str:
    n = normalize_profile_name(name)
    if not PROFILE_NAME_RE.match(n):
        raise ValueError(
            f"Invalid profile name {name!r}: use lowercase letters, digits, '-' or '_' (max 64 chars)")
    return n


def profile_dir(name: str) -> Path:
    return profiles_root() / validate_profile_name(name)


def safe_child_name(name: str, what: str = "name") -> str:
    """Names used as file/dir names inside a profile (skills, MCP servers, sessions)."""
    n = (name or "").strip()
    if not n or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", n) or ".." in n:
        raise ValueError(f"Invalid {what} {name!r}: letters, digits, '.', '-', '_' only")
    return n


def chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows / exotic filesystems
