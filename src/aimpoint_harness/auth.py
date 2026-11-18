"""OAuth for subscription providers: Claude Pro/Max (Claude Code OAuth) and ChatGPT (Codex OAuth).

Aimpoint owns its own grants (stored in $AIMPOINT_HOME/auth.json). It never refreshes tokens that
belong to the Claude Code or Codex CLIs: refresh tokens are single-use, so sharing one would log the
other tool out.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from . import paths

# ── Claude (same public client the Claude Code CLI uses) ──
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
CLAUDE_TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token", "https://console.anthropic.com/v1/oauth/token")
CLAUDE_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
CLAUDE_SCOPES = "org:create_api_key user:profile user:inference"

# ── Codex / ChatGPT (same public client the Codex CLI uses) ──
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_ISSUER = "https://auth.openai.com"
CODEX_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"

_REFRESH_MARGIN_S = 300
_lock = asyncio.Lock()


class AuthError(RuntimeError):
    pass


# ── store ──
def load_store() -> dict[str, Any]:
    p = paths.auth_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise AuthError(f"{p} is not valid JSON: {e}")


def save_entry(provider: str, entry: dict[str, Any] | None) -> None:
    p: Path = paths.auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load_store()
    if entry is None:
        data.pop(provider, None)
    else:
        data[provider] = entry
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    paths.chmod_private(tmp)
    tmp.replace(p)


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ── Claude ──
def _claude_token_post(payload: dict, *, as_json: bool) -> dict:
    last: Exception | None = None
    for url in CLAUDE_TOKEN_URLS:
        try:
            if as_json:
                r = httpx.post(url, json=payload, timeout=20, headers={"User-Agent": "axios/1.7.9"})
            else:
                r = httpx.post(url, data=payload, timeout=20, headers={"User-Agent": "axios/1.7.9"})
            if r.status_code == 200:
                return r.json()
            last = AuthError(f"{url} -> HTTP {r.status_code}: {r.text[:300]}")
            if r.status_code in (400, 401) and "invalid_grant" in r.text:
                break
        except httpx.HTTPError as e:
            last = e
    raise AuthError(f"Claude token request failed: {last}")


def claude_login_interactive() -> dict:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    url = CLAUDE_AUTHORIZE_URL + "?" + urlencode({
        "code": "true", "client_id": CLAUDE_CLIENT_ID, "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT_URI, "scope": CLAUDE_SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    print("\nSign in with your Claude Pro/Max account:\n\n  " + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    raw = input("Paste the authorization code shown after approving: ").strip()
    if not raw:
        raise AuthError("No code entered")
    code, _, got_state = raw.partition("#")
    if got_state != state:
        raise AuthError("OAuth state mismatch; start the login again")
    res = _claude_token_post({
        "grant_type": "authorization_code", "client_id": CLAUDE_CLIENT_ID, "code": code, "state": got_state,
        "redirect_uri": CLAUDE_REDIRECT_URI, "code_verifier": verifier}, as_json=True)
    entry = {"access_token": res["access_token"], "refresh_token": res.get("refresh_token", ""),
             "expires_at_ms": int(time.time() * 1000) + int(res.get("expires_in", 3600)) * 1000,
             "source": "aimpoint_pkce"}
    save_entry("claude-code", entry)
    return entry


def _claude_refresh(entry: dict) -> dict:
    res = _claude_token_post({"grant_type": "refresh_token", "refresh_token": entry["refresh_token"],
                              "client_id": CLAUDE_CLIENT_ID}, as_json=False)
    entry = dict(entry, access_token=res["access_token"],
                 refresh_token=res.get("refresh_token") or entry["refresh_token"],
                 expires_at_ms=int(time.time() * 1000) + int(res.get("expires_in", 3600)) * 1000)
    save_entry("claude-code", entry)
    return entry


# ── Codex ──
def codex_login_device() -> dict:
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode", json={"client_id": CODEX_CLIENT_ID})
        if r.status_code != 200:
            raise AuthError(f"Device code request failed: HTTP {r.status_code} {r.text[:200]}")
        dev = r.json()
        interval = max(3, int(dev.get("interval", 5)))
        print(f"\nOpen {CODEX_ISSUER}/codex/device and enter code:  {dev['user_code']}\n")
        try:
            webbrowser.open(f"{CODEX_ISSUER}/codex/device")
        except Exception:
            pass
        deadline = time.monotonic() + 15 * 60
        code_resp = None
        while time.monotonic() < deadline:
            time.sleep(interval)
            p = c.post(f"{CODEX_ISSUER}/api/accounts/deviceauth/token",
                       json={"device_auth_id": dev["device_auth_id"], "user_code": dev["user_code"]})
            if p.status_code == 200:
                code_resp = p.json()
                break
            if p.status_code not in (403, 404):
                raise AuthError(f"Device auth polling failed: HTTP {p.status_code} {p.text[:200]}")
        if not code_resp:
            raise AuthError("Login timed out")
        t = c.post(CODEX_TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code_resp["authorization_code"],
            "redirect_uri": f"{CODEX_ISSUER}/deviceauth/callback", "client_id": CODEX_CLIENT_ID,
            "code_verifier": code_resp["code_verifier"]})
        if t.status_code != 200:
            raise AuthError(f"Token exchange failed: HTTP {t.status_code} {t.text[:200]}")
        tok = t.json()
    entry = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
             "id_token": tok.get("id_token", ""), "last_refresh": int(time.time()), "source": "aimpoint_device"}
    save_entry("codex", entry)
    return entry


def _codex_refresh(entry: dict) -> dict:
    r = httpx.post(CODEX_TOKEN_URL, timeout=20, data={
        "grant_type": "refresh_token", "refresh_token": entry["refresh_token"], "client_id": CODEX_CLIENT_ID})
    if r.status_code != 200:
        raise AuthError(f"Codex token refresh failed (HTTP {r.status_code}); run: aimpoint login codex")
    tok = r.json()
    entry = dict(entry, access_token=tok["access_token"],
                 refresh_token=tok.get("refresh_token") or entry["refresh_token"],
                 id_token=tok.get("id_token") or entry.get("id_token", ""), last_refresh=int(time.time()))
    save_entry("codex", entry)
    return entry


def codex_account_headers(access_token: str) -> dict[str, str]:
    auth = jwt_claims(access_token).get("https://api.openai.com/auth", {}) or {}
    h: dict[str, str] = {}
    if auth.get("chatgpt_account_id"):
        h["ChatGPT-Account-ID"] = auth["chatgpt_account_id"]
    residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
    if residency:
        h["x-openai-internal-codex-residency"] = str(residency)
    return h


# ── resolution used by providers ──
def _expires_at(provider: str, entry: dict) -> float:
    if provider == "claude-code":
        return entry.get("expires_at_ms", 0) / 1000
    return float(jwt_claims(entry.get("access_token", "")).get("exp", 0))


async def get_access_token(provider: str, static_token: str = "", force_refresh: bool = False) -> str:
    """A static token from the env (e.g. `claude setup-token` output) wins; else the stored grant,
    refreshed when it is about to expire."""
    if static_token and not force_refresh:
        return static_token
    async with _lock:
        entry = load_store().get(provider)
        if not entry or not entry.get("access_token"):
            if static_token:
                raise AuthError(f"{provider}: the configured token was rejected and there is no stored login "
                                f"to refresh. Run: aimpoint login {provider}")
            raise AuthError(f"No {provider} credentials. Run: aimpoint login {provider}")
        exp = _expires_at(provider, entry)
        if force_refresh or (exp and exp - time.time() < _REFRESH_MARGIN_S):
            if not entry.get("refresh_token"):
                raise AuthError(f"{provider} token expired. Run: aimpoint login {provider}")
            fn = _claude_refresh if provider == "claude-code" else _codex_refresh
            entry = await asyncio.to_thread(fn, entry)
        return entry["access_token"]


def status() -> dict[str, dict]:
    out = {}
    for prov, entry in load_store().items():
        exp = _expires_at(prov, entry)
        out[prov] = {"source": entry.get("source", "?"), "has_refresh_token": bool(entry.get("refresh_token")),
                     "expires_in_s": int(exp - time.time()) if exp else None}
    return out
