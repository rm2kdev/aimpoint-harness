"""End-to-end API test against a running `aimpoint serve` (live providers). Usage: python e2e_api.py BASE"""
import json
import os
import sys
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8650"
HOME = Path(os.environ["AIMPOINT_HOME"])


def env_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(f"no key in {path}")


GKEY = env_key(HOME / ".env")
RKEY = env_key(HOME / "profiles/router/.env")
c = httpx.Client(base_url=BASE, timeout=300)
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or cond  # noqa: E731
H = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
results = []

r = c.get("/health"); results.append(ok(r.status_code == 200 and r.json()["status"] == "ok", "health"))
r = c.get("/v1/models"); results.append(ok(r.status_code == 401, f"no key -> 401 ({r.status_code})"))
r = c.get("/v1/capabilities", headers=H(GKEY))
results.append(ok(r.status_code == 200 and r.json()["profile"] == "default", f"capabilities default: {r.json().get('model')}"))
r = c.get("/p/nope/v1/models", headers=H(GKEY)); results.append(ok(r.status_code == 404, "unknown profile -> 404"))
r = c.get("/p/router/v1/models", headers=H(GKEY)); results.append(ok(r.status_code == 401, "router rejects global key"))
r = c.get("/p/router/v1/models", headers=H(RKEY)); results.append(ok(r.status_code == 200, f"router own key: {r.json()['data'][0]['id']}"))
r = c.get("/p/sarah/v1/skills", headers=H(GKEY)); results.append(ok([s["name"] for s in r.json()["data"]] == ["greeting-protocol"], "sarah skills"))
r = c.get("/p/sarah/v1/toolsets", headers=H(GKEY)); results.append(ok(r.json()["data"][0]["connected"], f"sarah toolsets: {r.json()['data']}"))

# 1) Codex (default profile), server-side session continuity
sid = "e2e-codex-1"
r = c.post("/v1/chat/completions", headers={**H(GKEY), "X-Hermes-Session-Id": sid}, json={
    "model": "anything", "messages": [{"role": "user", "content": "Call the vault_word tool and tell me the word only."}]})
body = r.json()
print("codex turn1:", r.status_code, json.dumps(body)[:400])
results.append(ok(r.status_code == 200 and "pomegranate-42" in body["choices"][0]["message"]["content"], "codex + MCP tool"))
results.append(ok(r.headers.get("x-hermes-session-id") == sid, "session header echoed"))
r = c.post("/v1/chat/completions", headers={**H(GKEY), "X-Hermes-Session-Id": sid}, json={
    "messages": [{"role": "user", "content": "Repeat the exact word you just told me, reversed letter by letter is NOT needed, just the same word again."}]})
results.append(ok(r.status_code == 200 and "pomegranate-42" in r.json()["choices"][0]["message"]["content"],
                  f"codex session memory: {r.json()['choices'][0]['message']['content'][:80]!r}"))
r = c.get(f"/api/sessions/{sid}/messages", headers=H(GKEY))
roles = [m["role"] for m in r.json()["data"]]
results.append(ok(roles.count("user") == 2 and "tool" in roles, f"stored transcript roles {roles}"))

# 2) OpenRouter (router profile), streaming with tool progress events
events, text, saw_done = [], "", False
with c.stream("POST", "/p/router/v1/chat/completions", headers=H(RKEY), json={
        "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "Use the add tool to compute 19+23, then reply with the number."}]}) as resp:
    print("router stream status", resp.status_code, resp.headers.get("x-hermes-session-id"))
    ev = ""
    for line in resp.iter_lines():
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                saw_done = True
            elif ev == "hermes.tool.progress":
                events.append(json.loads(data)); ev = ""
            else:
                d = json.loads(data)
                if "error" in d:
                    print("STREAM ERROR", d)
                for ch in d.get("choices", []):
                    text += ch.get("delta", {}).get("content") or ""
print("router text:", repr(text[:200]), "events:", events)
results.append(ok("42" in text and saw_done and any(e["status"] == "running" for e in events), "openrouter streaming + tool events"))
results.append(ok("Sarah" in text, "router inherited sarah's soul via --clone"))

# 3) Claude Code (sarah), stateless OpenAI-style + client-side tool handback
tools = [{"type": "function", "function": {"name": "get_local_weather", "description": "Weather at the user's location",
                                           "parameters": {"type": "object", "properties": {}}}}]
msgs = [{"role": "system", "content": "Answer in one short sentence."},
        {"role": "user", "content": "What's the weather where I am? Use get_local_weather."}]
r = c.post("/p/sarah/v1/chat/completions", headers=H(GKEY), json={"messages": msgs, "tools": tools})
b = r.json(); print("sarah handback:", json.dumps(b)[:400])
choice = b["choices"][0]
results.append(ok(choice["finish_reason"] == "tool_calls" and choice["message"]["tool_calls"][0]["function"]["name"] == "get_local_weather",
                  "client tool handed back"))
tc = choice["message"]["tool_calls"][0]
msgs += [choice["message"], {"role": "tool", "tool_call_id": tc["id"], "content": "Sunny, 31C, Perth"}]
r = c.post("/p/sarah/v1/chat/completions", headers=H(GKEY), json={"messages": msgs, "tools": tools})
final = r.json()["choices"][0]["message"]["content"]
print("sarah final:", repr(final))
results.append(ok("31" in final, "client tool result used"))
r = c.get("/p/sarah/api/sessions", headers=H(GKEY))
results.append(ok(len(r.json()["data"]) == 1, f"sarah sessions: {[s['id'] for s in r.json()['data']]}"))
r = c.get("/api/sessions", headers=H(GKEY))
results.append(ok([s["id"] for s in r.json()["data"]] == [sid], "default sessions isolated from sarah"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
