"""Offline tests: no network, no credentials. A scripted fake provider drives the real agent loop,
real MCP (stdio fixture), real skills, real sessions and the real HTTP app."""
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

FIXTURE = str(Path(__file__).parent / "fixtures" / "fixture_mcp.py")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMPOINT_HOME", str(tmp_path / "home"))
    for k in ("API_SERVER_KEY", "AIMPOINT_PROVIDER", "AIMPOINT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    from aimpoint_harness.cli import main
    main(["init"])
    main(["profile", "create", "ops"])
    return tmp_path / "home"


class FakeProvider:
    """Turn 1: call the MCP `add` tool (and a client tool if offered). Turn 2: answer with the tool result."""
    calls: list = []

    def __init__(self, profile):
        self.profile = profile

    async def complete(self, system, messages, tools, on_text, model=None):
        from aimpoint_harness.providers import Turn
        FakeProvider.calls.append({"system": system, "messages": list(messages), "tools": [t["function"]["name"] for t in tools]})
        names = {t["function"]["name"] for t in tools}
        last = messages[-1]
        if last["role"] == "user" and "weather" in str(last["content"]) and "get_weather" in names:
            return Turn(tool_calls=[{"id": "c1", "name": "get_weather", "arguments": "{}"}], finish_reason="tool_calls")
        if last["role"] == "user" and "add" in str(last["content"]):
            return Turn(content="Working.", tool_calls=[{"id": "t1", "name": "mcp__fixture__add", "arguments": '{"a": 2, "b": 40}'}])
        text = f"result={last['content']}" if last["role"] == "tool" else f"echo:{last['content']}"
        if on_text:
            for ch in (text[:3], text[3:]):
                await on_text(ch)
        return Turn(content=text, usage={"prompt_tokens": 10, "completion_tokens": 5})


@pytest.fixture()
def client(home, monkeypatch):
    import aimpoint_harness.agent as agent
    from aimpoint_harness.cli import main
    monkeypatch.setattr(agent, "build_provider", lambda p: FakeProvider(p))
    FakeProvider.calls = []
    main(["mcp", "add", "fixture", "-p", "ops", "--no-test", "--", sys.executable, FIXTURE])
    from aimpoint_harness.server import build_app
    with TestClient(build_app()) as c:
        yield c


def key(home: Path, profile: str | None = None) -> str:
    from aimpoint_harness.envfile import read_env
    path = home / "profiles" / profile / ".env" if profile else home / ".env"
    return read_env(path)["API_SERVER_KEY"]


def test_layout_and_inheritance(home):
    from aimpoint_harness.cli import main
    from aimpoint_harness.profiles import load_profile
    p = home / "profiles" / "ops"
    assert all((p / x).exists() for x in (".env", "SOUL.md", "skills", "mcps", "sessions"))
    main(["provider", "set", "claude-code", "--no-prompt"])                   # global
    main(["provider", "set", "codex", "--model", "gpt-x", "-p", "ops", "--no-prompt"])  # override
    assert load_profile("default").provider == "claude-code"
    assert load_profile("default").model == "claude-sonnet-5-5"
    assert (load_profile("ops").provider, load_profile("ops").model) == ("codex", "gpt-x")


def test_envfile_roundtrip(tmp_path):
    from aimpoint_harness.envfile import interpolate, read_env, set_env
    f = tmp_path / ".env"
    f.write_text("# comment\nA=1\n")
    set_env(f, "B", 'has space "and quotes" $x')
    set_env(f, "A", "2")
    assert read_env(f) == {"A": "2", "B": 'has space "and quotes" $x'}
    assert "# comment" in f.read_text()
    set_env(f, "A", None)
    assert "A" not in read_env(f)
    assert interpolate({"h": ["Bearer ${T}"]}, {"T": "xyz"}) == {"h": ["Bearer xyz"]}


def test_skills_view_and_traversal(home):
    from aimpoint_harness import skills
    d = home / "profiles" / "ops" / "skills"
    skills.scaffold(d, "deploy", "Use when deploying.")
    (d / "deploy" / "references").mkdir()
    (d / "deploy" / "references" / "steps.md").write_text("step one")
    found = skills.discover(d)
    assert [s.name for s in found] == ["deploy"]
    assert "references/steps.md" in skills.skill_view(found, "deploy")
    assert skills.skill_view(found, "deploy", "references/steps.md") == "step one"
    assert skills.skill_view(found, "deploy", "../../.env").startswith("Error")
    assert "<available_skills>" in skills.index_prompt(found)


def test_message_conversions():
    from aimpoint_harness.providers.anthropic import to_anthropic
    from aimpoint_harness.providers.codex import to_responses_input
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "x", "arguments": '{"k":1}'}},
                {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "b", "content": "2"},
            {"role": "user", "content": "next"}]
    a = to_anthropic(msgs)
    assert [m["role"] for m in a] == ["user", "assistant", "user"]  # tool results + next user merged
    assert [b["type"] for b in a[2]["content"]] == ["tool_result", "tool_result", "text"]
    assert a[1]["content"][0] == {"type": "tool_use", "id": "a", "name": "x", "input": {"k": 1}}
    r = to_responses_input(msgs)
    assert [i["type"] for i in r] == ["message", "function_call", "function_call", "function_call_output",
                                      "function_call_output", "message"]


def test_auth_and_routing(client, home):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models").status_code == 401
    g = {"Authorization": f"Bearer {key(home)}"}
    assert client.get("/v1/models", headers=g).status_code == 200
    assert client.get("/p/missing/v1/models", headers=g).status_code == 404
    from aimpoint_harness.cli import main
    main(["key", "generate", "-p", "ops"])
    assert client.get("/p/ops/v1/models", headers=g).status_code == 401
    assert client.get("/p/ops/v1/models", headers={"Authorization": f"Bearer {key(home, 'ops')}"}).status_code == 200


def test_chat_with_mcp_tool_soul_and_session(client, home):
    (home / "profiles" / "ops" / "SOUL.md").write_text("I am OPS-SOUL.")
    g = {"Authorization": f"Bearer {key(home)}", "X-Hermes-Session-Id": "s1"}
    r = client.post("/p/ops/v1/chat/completions", headers=g,
                    json={"messages": [{"role": "system", "content": "client-rule"}, {"role": "user", "content": "please add"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Working.\n\nresult=42"
    assert r.headers["x-hermes-session-id"] == "s1"
    first = FakeProvider.calls[0]
    assert "I am OPS-SOUL." in first["system"] and "client-rule" in first["system"]
    assert "mcp__fixture__add" in first["tools"]
    # continuation: server-side history, only the new message is sent
    r = client.post("/p/ops/v1/chat/completions", headers=g, json={"messages": [{"role": "user", "content": "again"}]})
    assert r.json()["choices"][0]["message"]["content"] == "echo:again"
    assert len(FakeProvider.calls[-1]["messages"]) == 5  # user, assistant(tool), tool, assistant, user
    msgs = client.get("/p/ops/api/sessions/s1/messages", headers=g).json()["data"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    # profiles are isolated
    assert client.get("/api/sessions", headers=g).json()["data"] == []
    assert not (home / "profiles" / "default" / "sessions" / "s1.json").exists()


def test_streaming_and_tool_events(client, home):
    g = {"Authorization": f"Bearer {key(home)}"}
    with client.stream("POST", "/p/ops/v1/chat/completions", headers=g,
                       json={"stream": True, "messages": [{"role": "user", "content": "please add"}]}) as r:
        raw = r.read().decode()
    assert "event: hermes.tool.progress" in raw and raw.rstrip().endswith("data: [DONE]")
    text = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
                   for line in raw.splitlines() if line.startswith("data: {") and '"choices"' in line)
    assert text.endswith("result=42")


def test_client_tool_handback(client, home):
    g = {"Authorization": f"Bearer {key(home)}"}
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    msgs = [{"role": "user", "content": "weather?"}]
    r = client.post("/v1/chat/completions", headers=g, json={"messages": msgs, "tools": tools}).json()
    assert r["choices"][0]["finish_reason"] == "tool_calls"
    call = r["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    msgs += [r["choices"][0]["message"], {"role": "tool", "tool_call_id": call["id"], "content": "sunny"}]
    r = client.post("/v1/chat/completions", headers=g, json={"messages": msgs, "tools": tools}).json()
    assert r["choices"][0]["message"]["content"] == "result=sunny"
