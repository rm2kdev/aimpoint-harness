"""Profiles: one folder on disk = one isolated agent (soul, skills, MCPs, env, sessions)."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .envfile import read_env, set_env

# provider id -> defaults. `key_env` names the env var holding the API key (api-key providers).
PROVIDERS: dict[str, dict] = {
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "model": "anthropic/claude-sonnet-5.5",
                   "label": "OpenRouter (API key)"},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1",
               "key_env": "OPENAI_API_KEY", "model": "gpt-5", "label": "OpenAI Platform (API key)"},
    "openai-compatible": {"kind": "openai", "base_url": "http://127.0.0.1:11434/v1",
                          "key_env": "AIMPOINT_LLM_API_KEY", "model": "",
                          "label": "Any OpenAI-compatible server (Ollama, LM Studio, vLLM, LiteLLM...)"},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com",
                  "key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5",
                  "label": "Anthropic API (API key)"},
    "claude-code": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "oauth": True,
                    "key_env": "CLAUDE_CODE_OAUTH_TOKEN", "model": "claude-sonnet-5-5",
                    "label": "Claude Pro/Max subscription (Claude Code OAuth)"},
    "codex": {"kind": "codex", "base_url": "https://chatgpt.com/backend-api/codex", "oauth": True,
              "key_env": "CODEX_ACCESS_TOKEN", "model": "gpt-6-luna",
              "label": "ChatGPT Plus/Pro subscription (Codex OAuth)"},
}

DEFAULT_SOUL = """# Soul

You are a helpful assistant running inside Aimpoint Harness.
Describe this profile's identity, tone and standing instructions here.
"""


@dataclass
class Profile:
    name: str
    dir: Path
    env: dict[str, str] = field(default_factory=dict)

    # ── paths ──
    @property
    def env_path(self) -> Path: return self.dir / ".env"
    @property
    def soul_path(self) -> Path: return self.dir / "SOUL.md"
    @property
    def skills_dir(self) -> Path: return self.dir / "skills"
    @property
    def mcps_dir(self) -> Path: return self.dir / "mcps"
    @property
    def sessions_dir(self) -> Path: return self.dir / "sessions"

    # ── settings (profile .env overrides global .env overrides process env) ──
    def get(self, key: str, default: str = "") -> str:
        v = self.env.get(key)
        return v if v not in (None, "") else default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        v = self.get(key, "").lower()
        return default if not v else v in ("1", "true", "yes", "on")

    @property
    def provider(self) -> str:
        return self.get("AIMPOINT_PROVIDER", "openrouter")

    @property
    def provider_spec(self) -> dict:
        spec = PROVIDERS.get(self.provider)
        if spec is None:
            raise ValueError(f"Profile {self.name!r}: unknown AIMPOINT_PROVIDER {self.provider!r} "
                             f"(choose one of: {', '.join(PROVIDERS)})")
        return spec

    @property
    def model(self) -> str:
        return self.get("AIMPOINT_MODEL", self.provider_spec["model"])

    @property
    def base_url(self) -> str:
        return self.get("AIMPOINT_BASE_URL", self.provider_spec["base_url"]).rstrip("/")

    @property
    def api_key(self) -> str:
        return self.get(self.provider_spec["key_env"], "")

    @property
    def server_key(self) -> str:
        """Bearer key for the HTTP API. A profile key, when set, is the ONLY key that profile accepts."""
        return self.env_profile_only.get("API_SERVER_KEY", "")

    @property
    def env_profile_only(self) -> dict[str, str]:
        return read_env(self.env_path)

    def soul(self) -> str:
        try:
            return self.soul_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def mcp_configs(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not self.mcps_dir.is_dir():
            return out
        for f in sorted(self.mcps_dir.glob("*.json")):
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:  # surface, don't crash the profile
                cfg = {"_error": f"invalid JSON: {e}"}
            out[f.stem] = cfg
        return out

    def signature(self) -> tuple:
        """Changes whenever env / MCP files change, so the server can hot-reload MCP connections."""
        files = [paths.global_env_path(), self.env_path]
        if self.mcps_dir.is_dir():
            files += sorted(self.mcps_dir.glob("*.json"))
        sig = []
        for f in files:
            try:
                st = f.stat()
                sig.append((str(f), st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                sig.append((str(f), 0, 0))
        return tuple(sig)


def merged_env(profile_env_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(read_env(paths.global_env_path()))
    env.update(read_env(profile_env_path))
    return env


def load_profile(name: str) -> Profile:
    n = paths.validate_profile_name(name)
    d = paths.profiles_root() / n
    if not d.is_dir():
        raise FileNotFoundError(f"Profile {n!r} does not exist (create it with: aimpoint profile create {n})")
    return Profile(name=n, dir=d, env=merged_env(d / ".env"))


def profile_exists(name: str) -> bool:
    try:
        return (paths.profiles_root() / paths.validate_profile_name(name)).is_dir()
    except ValueError:
        return False


def list_profiles() -> list[str]:
    root = paths.profiles_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and paths.PROFILE_NAME_RE.match(p.name))


def create_profile(name: str, clone_from: str | None = None, soul: str | None = None) -> Profile:
    n = paths.validate_profile_name(name)
    d = paths.profiles_root() / n
    if d.exists():
        raise FileExistsError(f"Profile {n!r} already exists at {d}")
    if clone_from:
        src = load_profile(clone_from).dir
        shutil.copytree(src, d, ignore=shutil.ignore_patterns("sessions"))
    d.mkdir(parents=True, exist_ok=True)
    for sub in ("skills", "mcps", "sessions"):
        (d / sub).mkdir(exist_ok=True)
    if not (d / "SOUL.md").exists() or soul is not None:
        (d / "SOUL.md").write_text(soul if soul is not None else DEFAULT_SOUL, encoding="utf-8")
    if not (d / ".env").exists():
        (d / ".env").write_text(
            "# Profile settings. Anything unset here falls back to $AIMPOINT_HOME/.env\n"
            "# AIMPOINT_PROVIDER=claude-code\n# AIMPOINT_MODEL=claude-sonnet-5-5\n"
            "# API_SERVER_KEY=  (optional: a key only this profile accepts)\n", encoding="utf-8")
        paths.chmod_private(d / ".env")
    return load_profile(n)


def delete_profile(name: str) -> None:
    n = paths.validate_profile_name(name)
    if n == paths.DEFAULT_PROFILE:
        raise ValueError("The default profile cannot be deleted")
    shutil.rmtree(load_profile(n).dir)


def ensure_home() -> Path:
    h = paths.home()
    (h / "profiles").mkdir(parents=True, exist_ok=True)
    g = paths.global_env_path()
    if not g.exists():
        g.write_text("# Aimpoint Harness global settings (inherited by every profile)\n", encoding="utf-8")
        paths.chmod_private(g)
    if not profile_exists(paths.DEFAULT_PROFILE):
        create_profile(paths.DEFAULT_PROFILE)
    return h


def set_setting(key: str, value: str | None, profile: str | None = None) -> Path:
    target = (load_profile(profile).env_path if profile else paths.global_env_path())
    set_env(target, key, value)
    return target
