"""`aimpoint` command-line management interface."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from . import __version__, auth, paths
from . import skills as skills_mod
from .envfile import mask, read_env, set_env
from .profiles import (PROVIDERS, create_profile, delete_profile, ensure_home, list_profiles, load_profile,
                       set_setting)

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")


def _p(args) -> str:
    return getattr(args, "profile", None) or paths.DEFAULT_PROFILE


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ── init / status / key ──
def cmd_init(args):
    h = ensure_home()
    g = read_env(paths.global_env_path())
    if not g.get("API_SERVER_KEY"):
        set_env(paths.global_env_path(), "API_SERVER_KEY", secrets.token_hex(32))
        print("Generated API_SERVER_KEY (show it with: aimpoint key show)")
    print(f"Aimpoint home: {h}\nProfiles: {', '.join(list_profiles())}")
    print("Next: aimpoint provider set <provider>   (aimpoint provider list)")


def cmd_status(args):
    ensure_home()
    g = read_env(paths.global_env_path())
    print(f"Aimpoint Harness {__version__}\nHome: {paths.home()}")
    print(f"API: http://{g.get('API_SERVER_HOST', '127.0.0.1')}:{g.get('API_SERVER_PORT', '8642')}"
          f"  key: {'set' if g.get('API_SERVER_KEY') else 'MISSING (aimpoint key generate)'}")
    print("\nProfiles:")
    for name in list_profiles():
        p = load_profile(name)
        n_sk = len(skills_mod.discover(p.skills_dir))
        n_mcp = len(p.mcp_configs())
        cred = "oauth" if p.provider_spec.get("oauth") else ("key set" if p.api_key else "NO KEY")
        if p.provider_spec.get("oauth"):
            cred = "token" if p.api_key else ("login ok" if auth.load_store().get(p.provider) else "NOT LOGGED IN")
        print(f"  {name:<16} {p.provider:<18} {p.model:<28} {cred:<14} skills={n_sk} mcps={n_mcp}"
              f"{'  own-key' if p.server_key else ''}")
    st = auth.status()
    if st:
        print("\nSubscription logins:")
        for prov, s in st.items():
            print(f"  {prov:<12} source={s['source']} refresh={'yes' if s['has_refresh_token'] else 'no'} "
                  f"expires_in={s['expires_in_s']}s")


def cmd_key(args):
    ensure_home()
    target = load_profile(args.profile).env_path if args.profile else paths.global_env_path()
    if args.action == "generate":
        key = secrets.token_hex(32)
        set_env(target, "API_SERVER_KEY", key)
        print(f"API_SERVER_KEY written to {target}")
        if args.show:
            print(key)
    else:
        key = read_env(target).get("API_SERVER_KEY")
        print(key or "(not set)")


# ── profiles ──
def cmd_profile(args):
    ensure_home()
    a = args.action
    if a == "list":
        for name in list_profiles():
            p = load_profile(name)
            print(f"{name:<20} {p.provider:<18} {p.model}")
    elif a == "create":
        p = create_profile(args.name, clone_from=args.clone)
        print(f"Created profile {p.name} at {p.dir}")
        print(f"API base URL: /p/{p.name}/v1  (edit its soul: {p.soul_path})")
    elif a == "delete":
        if not args.yes and input(f"Delete profile {args.name} and all its sessions? [y/N] ").lower() != "y":
            return
        delete_profile(args.name)
        print(f"Deleted {args.name}")
    elif a == "show":
        p = load_profile(args.name)
        _print_json({"name": p.name, "dir": str(p.dir), "provider": p.provider, "model": p.model,
                     "base_url": p.base_url, "own_api_key": bool(p.server_key),
                     "skills": [s.name for s in skills_mod.discover(p.skills_dir)],
                     "mcps": {k: ("disabled" if v.get("enabled") is False else "enabled")
                              for k, v in p.mcp_configs().items()},
                     "soul_chars": len(p.soul())})
    elif a == "path":
        print(load_profile(args.name).dir)
    elif a == "soul":
        p = load_profile(args.name)
        if args.set_file:
            p.soul_path.write_text(Path(args.set_file).read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Updated {p.soul_path}")
        elif args.edit:
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
            os.system(f'{editor} "{p.soul_path}"')
        else:
            print(p.soul() or "(empty)")


# ── providers / auth ──
def cmd_provider(args):
    ensure_home()
    if args.action == "list":
        for pid, spec in PROVIDERS.items():
            print(f"{pid:<18} {spec['label']}\n{'':<18} default model: {spec['model'] or '(set --model)'}"
                  f"   credential: {spec['key_env']}{' or aimpoint login ' + pid if spec.get('oauth') else ''}")
        return
    pid = args.provider
    if pid not in PROVIDERS:
        _die(f"unknown provider {pid!r}; choose from {', '.join(PROVIDERS)}")
    spec = PROVIDERS[pid]
    where = set_setting("AIMPOINT_PROVIDER", pid, args.profile)
    if args.model:
        set_setting("AIMPOINT_MODEL", args.model, args.profile)
    elif not args.keep_model and read_env(where).get("AIMPOINT_MODEL"):
        # a model id from the previous provider would not work here; fall back to this provider's default
        set_setting("AIMPOINT_MODEL", None, args.profile)
    if args.base_url is not None:
        set_setting("AIMPOINT_BASE_URL", args.base_url or None, args.profile)
    print(f"{'Profile ' + args.profile if args.profile else 'Global default'}: provider={pid} "
          f"model={args.model or spec['model'] or '(unset)'}  ({where})")
    prof = load_profile(_p(args))
    if spec.get("oauth"):
        if not prof.api_key and not auth.load_store().get(pid):
            print(f"Not signed in yet. Run: aimpoint login {pid}")
    elif not prof.api_key and not args.no_prompt and sys.stdin.isatty():
        key = getpass.getpass(f"{spec['key_env']} (hidden, Enter to skip): ").strip()
        if key:
            set_setting(spec["key_env"], key, args.profile)
            print(f"Saved {spec['key_env']} ({mask(key)})")


def cmd_login(args):
    ensure_home()
    pid = args.provider
    try:
        if pid == "claude-code":
            if args.token:
                tok = getpass.getpass("Paste the token from `claude setup-token` (hidden): ").strip()
                if not tok:
                    _die("no token entered")
                set_setting("CLAUDE_CODE_OAUTH_TOKEN", tok, args.profile)
                print(f"Saved CLAUDE_CODE_OAUTH_TOKEN ({mask(tok)})")
                return
            auth.claude_login_interactive()
        elif pid == "codex":
            auth.codex_login_device()
        else:
            _die("login supports: claude-code, codex (API-key providers use: aimpoint provider set <id>)")
    except auth.AuthError as e:
        _die(str(e))
    print(f"Signed in to {pid}. Stored in {paths.auth_path()}")


def cmd_logout(args):
    auth.save_entry(args.provider, None)
    print(f"Removed stored {args.provider} login")


def cmd_auth(args):
    _print_json(auth.status())


# ── env ──
def cmd_env(args):
    ensure_home()
    target = load_profile(args.profile).env_path if args.profile else paths.global_env_path()
    if args.action == "list":
        for k, v in read_env(target).items():
            print(f"{k}={mask(v) if any(h in k.upper() for h in SECRET_HINTS) else v}")
    elif args.action == "set":
        value = args.value
        if value is None:
            value = getpass.getpass(f"{args.key} (hidden): ")
        set_env(target, args.key, value)
        print(f"Set {args.key} in {target}")
    elif args.action == "unset":
        set_env(target, args.key, None)
        print(f"Removed {args.key} from {target}")


# ── skills ──
def cmd_skill(args):
    ensure_home()
    p = load_profile(_p(args))
    a = args.action
    if a == "list":
        for s in skills_mod.discover(p.skills_dir):
            print(f"{s.name:<28} {s.description}")
    elif a == "add":
        dest = skills_mod.install(args.source, p.skills_dir, name=args.name, subdir=args.subdir, force=args.force)
        print(f"Installed skill {dest.name} into profile {p.name}")
    elif a == "remove":
        d = p.skills_dir / paths.safe_child_name(args.name, "skill name")
        if not d.is_dir():
            match = next((s for s in skills_mod.discover(p.skills_dir) if s.name == args.name), None)
            if not match:
                _die(f"no skill {args.name!r} in profile {p.name}")
            d = match.dir
        shutil.rmtree(d)
        print(f"Removed skill {args.name} from {p.name}")
    elif a == "new":
        d = skills_mod.scaffold(p.skills_dir, args.name, args.description)
        print(f"Created {d / 'SKILL.md'}")
    elif a == "copy":
        src = next((s for s in skills_mod.discover(p.skills_dir) if s.name == args.name), None)
        if not src:
            _die(f"no skill {args.name!r} in profile {p.name}")
        dest = skills_mod.install(str(src.dir), load_profile(args.to).skills_dir, force=args.force)
        print(f"Copied {args.name} -> {args.to}:{dest.name}")


# ── MCP ──
def _kv(items: list[str] | None, what: str) -> dict[str, str]:
    out = {}
    for it in items or []:
        if "=" not in it:
            _die(f"{what} must be KEY=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k] = v
    return out


def _mcp_path(p, name: str) -> Path:
    return p.mcps_dir / f"{paths.safe_child_name(name, 'MCP server name')}.json"


def _write_mcp(p, name: str, cfg: dict, force: bool) -> Path:
    path = _mcp_path(p, name)
    if path.exists() and not force:
        _die(f"MCP server {name!r} already exists in profile {p.name} (use --force)")
    p.mcps_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return path


async def _mcp_probe(p, name: str) -> dict:
    from .mcp_pool import MCPPool
    cfg = dict(p.mcp_configs()[name], enabled=True)
    pool = MCPPool({name: cfg}, p.env, p.dir / "logs")
    try:
        await pool.ensure()
        return pool.status()[name]
    finally:
        await pool.close()


def cmd_mcp(args):
    ensure_home()
    p = load_profile(_p(args))
    a = args.action
    if a == "list":
        for name, cfg in p.mcp_configs().items():
            kind = cfg.get("url") or " ".join([cfg.get("command", "")] + [str(x) for x in cfg.get("args", [])])
            state = "invalid" if "_error" in cfg else ("disabled" if cfg.get("enabled") is False else "enabled")
            print(f"{name:<20} {state:<9} {kind}")
    elif a == "add":
        cmd = list(getattr(args, "command_argv", None) or [])
        if bool(args.url) == bool(cmd):
            _die("give either --url URL or a command after `--` (e.g. aimpoint mcp add fs -- npx -y pkg /dir)")
        cfg: dict = {"url": args.url} if args.url else {"command": cmd[0], "args": cmd[1:]}
        if args.transport:
            cfg["transport"] = args.transport
        if args.header:
            cfg["headers"] = _kv(args.header, "--header")
        if args.bearer_env:
            cfg.setdefault("headers", {})["Authorization"] = "Bearer ${" + args.bearer_env + "}"
        if args.env:
            cfg["env"] = _kv(args.env, "--env")
        if args.timeout:
            cfg["timeout"] = args.timeout
        if args.include:
            cfg["tools"] = {"include": args.include}
        path = _write_mcp(p, args.name, cfg, args.force)
        print(f"Saved {path}")
        if args.bearer_env and not p.get(args.bearer_env):
            print(f"Set the token with: aimpoint env set {args.bearer_env} --profile {p.name}")
        if not args.no_test:
            st = asyncio.run(_mcp_probe(load_profile(p.name), args.name))
            print(f"connected: {len(st['tools'])} tools" if st["connected"] else f"NOT connected: {st['error']}")
    elif a == "import":
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or data.get("mcp_servers") or data.get("servers") or data
        for name, cfg in servers.items():
            cfg = {k: v for k, v in cfg.items() if k in ("command", "args", "env", "url", "headers", "transport",
                                                         "timeout", "connect_timeout", "tools", "enabled", "cwd")}
            if cfg.get("transport") in ("streamable-http", "streamable_http", "http"):
                cfg.pop("transport")
            print(f"Saved {_write_mcp(p, name, cfg, args.force)}")
    elif a in ("remove", "enable", "disable", "show", "test"):
        path = _mcp_path(p, args.name)
        if not path.exists():
            _die(f"no MCP server {args.name!r} in profile {p.name}")
        if a == "remove":
            path.unlink()
            print(f"Removed {args.name} from {p.name}")
        elif a == "show":
            print(path.read_text(encoding="utf-8"))
        elif a == "test":
            st = asyncio.run(_mcp_probe(p, args.name))
            if st["connected"]:
                print(f"OK: {len(st['tools'])} tools")
                for t in st["tools"]:
                    print(f"  mcp__{args.name}__{t}")
            else:
                _die(f"could not connect: {st['error']}")
        else:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            cfg["enabled"] = a == "enable"
            path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            print(f"{a}d {args.name} in {p.name}")
    elif a == "copy":
        src = _mcp_path(p, args.name)
        if not src.exists():
            _die(f"no MCP server {args.name!r} in profile {p.name}")
        dst_p = load_profile(args.to)
        print(f"Saved {_write_mcp(dst_p, args.name, json.loads(src.read_text()), args.force)}")


def cmd_models(args):
    from .providers import list_models
    ensure_home()
    p = load_profile(_p(args))
    try:
        ids = asyncio.run(list_models(p))
    except Exception as e:  # noqa: BLE001
        _die(f"{p.provider}: {e}")
    print(f"# {p.provider} models available to profile {p.name} (current: {p.model})")
    for i in ids:
        print(("* " if i == p.model else "  ") + i)


# ── serve / chat ──
def cmd_serve(args):
    import uvicorn
    from .server import build_app
    ensure_home()
    g = read_env(paths.global_env_path())
    host = args.host or g.get("API_SERVER_HOST") or "127.0.0.1"
    port = int(args.port or g.get("API_SERVER_PORT") or 8642)
    if not g.get("API_SERVER_KEY") and not os.environ.get("API_SERVER_KEY"):
        print("warning: no global API_SERVER_KEY; only profiles with their own key will accept requests "
              "(aimpoint key generate)", file=sys.stderr)
    print(f"Aimpoint Harness API on http://{host}:{port}  (default profile: /v1, others: /p/<profile>/v1)")
    uvicorn.run(build_app(), host=host, port=port, log_level=args.log_level)


def cmd_chat(args):
    from .agent import Runtimes, run_agent
    from .sessions import SessionStore
    ensure_home()

    async def main():
        rts = Runtimes()
        try:
            rt = await rts.get(_p(args))
            store: SessionStore = rt.store
            sid = args.session
            history = (store.load(sid) or {}).get("messages", []) if sid else []
            for name, st in rt.pool.status().items():
                if st["enabled"] and not st["connected"]:
                    print(f"[mcp {name} unavailable: {st['error']}]", file=sys.stderr)

            async def out(d):
                print(d, end="", flush=True)

            async def tool(ev):
                if ev["status"] == "running":
                    print(f"\n[tool {ev['tool']}]", file=sys.stderr, flush=True)

            async def one(text: str):
                nonlocal history
                history = history + [{"role": "user", "content": text}]
                res = await run_agent(rt, history, on_text=out, on_tool=tool)
                print()
                history = history + res.new_messages
                if sid:
                    store.save(sid, history, title=text)

            if args.message:
                await one(" ".join(args.message))
                return
            print(f"Chatting with profile {rt.profile.name} ({rt.profile.provider}/{rt.profile.model}). Ctrl-D to exit.")
            while True:
                try:
                    text = input("\n> ").strip()
                except EOFError:
                    break
                if text:
                    await one(text)
        finally:
            await rts.close()

    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        _die(str(e))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aimpoint", description="Aimpoint Harness: profiles, skills, MCPs and an "
                                 "OpenAI-compatible API over your LLM subscription.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def prof(p, help_="Profile (default: global settings / 'default')"):
        p.add_argument("-p", "--profile", help=help_)

    sub.add_parser("init", help="Create ~/.aimpoint, the default profile and an API key").set_defaults(fn=cmd_init)
    sub.add_parser("status", help="Overview of profiles, providers and logins").set_defaults(fn=cmd_status)

    k = sub.add_parser("key", help="API key for the HTTP API")
    k.add_argument("action", choices=["generate", "show"])
    k.add_argument("--show", action="store_true", help="print the generated key")
    prof(k, "Give this profile its own key (it will accept only that key)")
    k.set_defaults(fn=cmd_key)

    pr = sub.add_parser("profile", help="Manage profiles")
    ps = pr.add_subparsers(dest="action", required=True)
    ps.add_parser("list")
    c = ps.add_parser("create"); c.add_argument("name"); c.add_argument("--clone", help="copy soul/skills/mcps/env from")
    d = ps.add_parser("delete"); d.add_argument("name"); d.add_argument("-y", "--yes", action="store_true")
    for n in ("show", "path"):
        ps.add_parser(n).add_argument("name")
    s = ps.add_parser("soul", help="print / edit SOUL.md"); s.add_argument("name")
    s.add_argument("--edit", action="store_true"); s.add_argument("--set-file")
    pr.set_defaults(fn=cmd_profile)

    pv = sub.add_parser("provider", help="Choose the LLM backend")
    pvs = pv.add_subparsers(dest="action", required=True)
    pvs.add_parser("list")
    st = pvs.add_parser("set"); st.add_argument("provider")
    st.add_argument("--model"); st.add_argument("--base-url")
    st.add_argument("--keep-model", action="store_true", help="keep the currently configured model id")
    st.add_argument("--no-prompt", action="store_true", help="don't ask for an API key")
    prof(st, "Set for one profile only (default: global, inherited by all profiles)")
    pv.set_defaults(fn=cmd_provider)

    lg = sub.add_parser("login", help="Sign in with a subscription (claude-code, codex)")
    lg.add_argument("provider", choices=["claude-code", "codex"])
    lg.add_argument("--token", action="store_true", help="claude-code: paste a `claude setup-token` token instead")
    prof(lg, "--token only: store the token for one profile")
    lg.set_defaults(fn=cmd_login)
    lo = sub.add_parser("logout"); lo.add_argument("provider", choices=["claude-code", "codex"]); lo.set_defaults(fn=cmd_logout)
    sub.add_parser("auth", help="Show stored subscription logins").set_defaults(fn=cmd_auth)

    ev = sub.add_parser("env", help="Global or per-profile settings/secrets (.env)")
    evs = ev.add_subparsers(dest="action", required=True)
    prof(evs.add_parser("list"))
    es = evs.add_parser("set"); es.add_argument("key"); es.add_argument("value", nargs="?"); prof(es)
    eu = evs.add_parser("unset"); eu.add_argument("key"); prof(eu)
    ev.set_defaults(fn=cmd_env)

    sk = sub.add_parser("skill", help="Manage a profile's skills")
    sks = sk.add_subparsers(dest="action", required=True)
    prof(sks.add_parser("list"))
    sa = sks.add_parser("add", help="from a folder or git URL"); sa.add_argument("source")
    sa.add_argument("--name"); sa.add_argument("--subdir"); sa.add_argument("--force", action="store_true"); prof(sa)
    sr = sks.add_parser("remove"); sr.add_argument("name"); prof(sr)
    sn = sks.add_parser("new"); sn.add_argument("name"); sn.add_argument("--description", default="TODO: when to use this skill"); prof(sn)
    sc = sks.add_parser("copy"); sc.add_argument("name"); sc.add_argument("--to", required=True)
    sc.add_argument("--force", action="store_true"); prof(sc)
    sk.set_defaults(fn=cmd_skill)

    mc = sub.add_parser("mcp", help="Manage a profile's MCP servers")
    mcs = mc.add_subparsers(dest="action", required=True)
    prof(mcs.add_parser("list"))
    ma = mcs.add_parser("add", help="aimpoint mcp add NAME --url URL | aimpoint mcp add NAME -- CMD ARGS...")
    ma.add_argument("name"); ma.add_argument("--url"); ma.add_argument("--transport", choices=["http", "sse"])
    ma.add_argument("--header", action="append", help="KEY=VALUE (use ${VAR} for secrets)")
    ma.add_argument("--bearer-env", help="send Authorization: Bearer ${VAR}")
    ma.add_argument("--env", action="append", help="KEY=VALUE for stdio servers (use ${VAR} for secrets)")
    ma.add_argument("--timeout", type=int); ma.add_argument("--include", action="append", help="only expose these tools")
    ma.add_argument("--force", action="store_true"); ma.add_argument("--no-test", action="store_true")
    prof(ma)
    mi = mcs.add_parser("import", help="import an mcpServers JSON (Claude Desktop / Cursor format)")
    mi.add_argument("file"); mi.add_argument("--force", action="store_true"); prof(mi)
    for n in ("remove", "enable", "disable", "show", "test"):
        x = mcs.add_parser(n); x.add_argument("name"); prof(x)
    mcp_copy = mcs.add_parser("copy"); mcp_copy.add_argument("name"); mcp_copy.add_argument("--to", required=True)
    mcp_copy.add_argument("--force", action="store_true"); prof(mcp_copy)
    mc.set_defaults(fn=cmd_mcp)

    md = sub.add_parser("models", help="List models the profile's provider credential can use")
    prof(md); md.set_defaults(fn=cmd_models)

    sv = sub.add_parser("serve", help="Run the HTTP API")
    sv.add_argument("--host"); sv.add_argument("--port", type=int)
    sv.add_argument("--log-level", default="info")
    sv.set_defaults(fn=cmd_serve)

    ch = sub.add_parser("chat", help="Chat with a profile in the terminal (one-shot if a message is given)")
    ch.add_argument("message", nargs="*"); ch.add_argument("--session", help="persist under this session id")
    prof(ch)
    ch.set_defaults(fn=cmd_chat)
    return ap


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    trailing: list[str] = []
    if "--" in argv:  # `aimpoint mcp add NAME [opts] -- command args...`
        i = argv.index("--")
        argv, trailing = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    args.command_argv = trailing
    try:
        args.fn(args)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        _die(str(e))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
