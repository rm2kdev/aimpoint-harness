# Aimpoint Harness

A self-hosted wrapper that brings your LLM subscription (Claude Pro/Max, ChatGPT, OpenRouter, any
OpenAI-compatible server) into Aimpoint as isolated **profiles**. Each profile is a folder with its
own soul, skills and MCP servers, exposed through an OpenAI Chat Completions API that is
URL-compatible with the Hermes Agent API server.

No self-improvement, memory or messaging gateway: just isolation of soul + skills + MCPs per profile.

> **Derivative work.** Aimpoint Harness is inspired by, and partly derived from,
> [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT). The
> profile-per-folder model (`SOUL.md`, `skills/`, `.env`), the MCP server config shape and
> `mcp__<server>__<tool>` naming, the skills index + `skill_view` progressive disclosure, the API
> server routes (`/v1/...`, `/p/<profile>/...`, `X-Hermes-Session-Id`) and the Claude Code / Codex
> OAuth request details all follow how Hermes does it. This is an independent project, not
> affiliated with or endorsed by Nous Research.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/). Works on macOS, Linux and Windows.

```bash
uv tool install .            # puts `aimpoint` on PATH
aimpoint init                # creates ~/.aimpoint, the default profile and an API key
```

Set `AIMPOINT_HOME` to keep everything somewhere else.

## Layout

```
~/.aimpoint/
  .env                      global settings + secrets, inherited by every profile
  auth.json                 subscription logins (OAuth, 0600)
  profiles/<name>/
    .env                    this profile's settings/secrets (override the global .env)
    SOUL.md                 identity / standing instructions
    skills/<skill>/SKILL.md skills for this profile only
    mcps/<server>.json      MCP servers for this profile only
    sessions/<id>.json      conversations, scoped to this profile
    logs/                   stdio MCP server stderr
```

Edit files directly or use the CLI; the API server picks up changes on the next request
(MCP servers reconnect when a `mcps/*.json` or `.env` changes, no restart needed).

## Providers

```bash
aimpoint provider list
aimpoint provider set claude-code               # global default for every profile
aimpoint provider set codex -p research         # override for one profile
aimpoint models -p research                     # what this credential can use
```

| id | backend | credential |
|---|---|---|
| `claude-code` | Claude Pro/Max subscription (Anthropic Messages API, Claude Code OAuth) | `aimpoint login claude-code` (browser PKCE, auto-refresh) or `aimpoint login claude-code --token` with a `claude setup-token` token |
| `codex` | ChatGPT subscription (`chatgpt.com/backend-api/codex`, Responses API) | `aimpoint login codex` (device code, auto-refresh) |
| `openrouter` | OpenRouter | `OPENROUTER_API_KEY` |
| `openai` | OpenAI Platform | `OPENAI_API_KEY` |
| `anthropic` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openai-compatible` | Ollama, LM Studio, vLLM, LiteLLM... | `AIMPOINT_BASE_URL` (+ optional `AIMPOINT_LLM_API_KEY`) |

Aimpoint keeps its own OAuth grants. It never refreshes the Claude Code or Codex CLI's tokens
(refresh tokens are single-use; sharing one logs the other tool out).

Model availability on subscriptions depends on the plan; use `aimpoint models` to see the ids.

## Profiles, souls, skills, MCPs

```bash
aimpoint profile create support                 # --clone default to copy soul/skills/mcps/env
aimpoint profile soul support --edit

aimpoint skill add ./my-skill -p support        # folder containing SKILL.md
aimpoint skill add https://github.com/org/skills.git --subdir pdf -p support
aimpoint skill new triage --description "Use when triaging tickets" -p support
aimpoint skill copy triage -p support --to sales

aimpoint mcp add fs -p support -- npx -y @modelcontextprotocol/server-filesystem /data
aimpoint mcp add linear -p support --url https://mcp.linear.app/mcp --bearer-env LINEAR_TOKEN
aimpoint env set LINEAR_TOKEN -p support        # hidden prompt, stored in the profile .env
aimpoint mcp import claude_desktop_config.json -p support   # {"mcpServers": {...}} format
aimpoint mcp test linear -p support
aimpoint mcp disable fs -p support

aimpoint chat -p support "hello"                # quick terminal test, no server needed
aimpoint status
```

`mcps/<name>.json`:

```json
{"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "${MY_TOKEN}"}}
{"url": "https://host/mcp", "headers": {"Authorization": "Bearer ${MCP_X_KEY}"}, "timeout": 120}
```

Optional keys: `enabled`, `transport` (`http` default, `sse`), `timeout`, `connect_timeout`,
`tools: {"include": [...]}` / `{"exclude": [...]}`, `cwd`. `${VAR}` resolves from the profile
`.env`, then the global `.env`, then the process environment. Stdio servers get only a minimal safe
environment plus their own `env`. Tools appear to the model as `mcp__<server>__<tool>` (as in Hermes).

Skills use progressive disclosure: the system prompt lists name + description, and the model loads
a skill with the built-in `skill_view` tool (`references/`, `templates/` etc. load on demand).

System prompt order: `SOUL.md`, skills index, current time, then any `system` messages the caller sent.

## API

```bash
aimpoint serve                   # http://127.0.0.1:8642 (API_SERVER_HOST / API_SERVER_PORT in the global .env)
aimpoint key show                # the bearer key
```

Same paths as the Hermes API server. Un-prefixed routes use the default profile; every route is
mirrored under `/p/<profile>/`:

| Method | Path |
|---|---|
| GET | `/health`, `/v1/health` (no auth) |
| GET | `/v1/models`, `/v1/capabilities`, `/v1/skills`, `/v1/toolsets` |
| POST | `/v1/chat/completions` (streaming and non-streaming) |
| GET / DELETE | `/api/sessions`, `/api/sessions/{id}`, `/api/sessions/{id}/messages` |
| GET | `/api/profiles` (Aimpoint extension, global key) |

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8642/p/support/v1", api_key="<API_SERVER_KEY>")
client.chat.completions.create(model="support", messages=[{"role": "user", "content": "hi"}])
```

- **Auth**: `Authorization: Bearer <key>`. The global `API_SERVER_KEY` works for every profile,
  unless the profile has its own (`aimpoint key generate -p support`), in which case only that key works.
- **Conversations**: send `X-Hermes-Session-Id: <id>` (or `X-Aimpoint-Session-Id`) and only the new
  message; history (including tool calls) is kept server-side in that profile. Without the header the
  request is plain OpenAI (the body's `messages` is the history) and the transcript is still stored
  under an id derived from its opening, returned in the `X-Hermes-Session-Id` response header.
- **Tools**: MCP tools and skills run on the harness. Function `tools` sent by the caller are offered
  to the model too; when the model calls one, the response ends with `finish_reason: "tool_calls"`
  for the caller to execute and send back (standard OpenAI flow).
- **Streaming** emits `chat.completion.chunk` frames, `event: hermes.tool.progress` frames for tool
  start/finish, and `data: [DONE]`. `stream_options.include_usage` is honoured.
- The request's `model` field is ignored (the profile decides) unless the profile sets
  `AIMPOINT_ALLOW_MODEL_OVERRIDE=true`.

## Settings (`.env`, global or per profile)

| Key | Default |
|---|---|
| `AIMPOINT_PROVIDER`, `AIMPOINT_MODEL`, `AIMPOINT_BASE_URL` | `openrouter`, provider default |
| `AIMPOINT_MAX_TOKENS` | 16000 (Anthropic), provider default otherwise |
| `AIMPOINT_TEMPERATURE`, `AIMPOINT_REASONING_EFFORT` | unset |
| `AIMPOINT_MAX_TOOL_ITERATIONS` | 25 |
| `AIMPOINT_READ_TIMEOUT` | 600 s |
| `AIMPOINT_ALLOW_CLIENT_TOOLS` | true |
| `AIMPOINT_ALLOW_MODEL_OVERRIDE` | false |
| `AIMPOINT_DEFAULT_PROFILE` (global) | `default` |
| `API_SERVER_KEY`, `API_SERVER_HOST`, `API_SERVER_PORT` (global) | generated, `127.0.0.1`, `8642` |

## Security

The API runs MCP tools on this machine, so treat the key like a password. It binds to loopback by
default; prefer a tunnel (Tailscale, Cloudflare Tunnel) to binding `0.0.0.0` on an open network.

## Tests

```bash
uv run pytest -q                                  # offline: fake provider, real MCP/skills/sessions/HTTP
AIMPOINT_HOME=... uv run python tests/e2e_api.py  # live: against a running `aimpoint serve`
```

## License

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Includes portions derived from Hermes Agent, Copyright (c) 2025 Nous Research (MIT).
