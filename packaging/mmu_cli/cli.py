"""
The MMU installer and launcher.

One binary, several verbs. `setup` is the wizard a first-time user runs; the rest
are the day-to-day operations that otherwise require remembering docker compose
incantations and which directory to run them in.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, dockerctl, embedding, envfile, mcpconfig, payload, server, state, ui


# ── helpers ──────────────────────────────────────────

def resolve_project_dir(args, required=True):
    """
    Where the MMU project lives, in order of confidence.

    An explicit --dir wins; then a checkout we are standing inside; then whatever
    a previous setup recorded. Guessing wrong here means running compose against
    the wrong volumes, so it never falls through to a default silently.
    """
    if getattr(args, "dir", None):
        return Path(args.dir).expanduser().resolve()
    found = payload.find_existing()
    if found:
        return found
    remembered = state.project_dir()
    if remembered and payload.looks_like_mmu(remembered):
        return remembered
    if required:
        ui.fail("No MMU installation found.")
        ui.info("Run `mmu-setup setup` first, or pass --dir /path/to/mmu.")
        raise SystemExit(2)
    return None


def env_values(project_dir):
    if project_dir is None:
        return {}
    env_path = Path(project_dir) / ".env"
    if not env_path.exists():
        return {}
    return envfile.parse(env_path.read_text(encoding="utf-8", errors="replace"))


def server_base(project_dir):
    port = env_values(project_dir).get("MMU_PORT", "8765")
    return f"http://127.0.0.1:{port}"


def bridge_command(project_dir):
    """
    How an MCP client should launch the bridge.

    A frozen mmu-mcp binary sitting beside this executable is the good case: the
    client then needs no Python at all. Falling back to the interpreter keeps
    source checkouts working, and prefers python3 over bare "python", which on
    most macOS and Linux systems is either absent or Python 2.
    """
    exe_name = "mmu-mcp.exe" if sys.platform == "win32" else "mmu-mcp"
    beside_us = Path(sys.executable).parent if payload.is_frozen() else Path.cwd()
    for candidate in (beside_us / exe_name, Path(project_dir) / exe_name):
        if candidate.exists():
            return str(candidate), []

    on_path = shutil.which("mmu-mcp")
    if on_path:
        return on_path, []

    script = Path(project_dir) / "mmu_mcp_server.py"
    if payload.is_frozen():
        interpreter = shutil.which("python3") or shutil.which("python") or "python3"
    else:
        interpreter = sys.executable
    return interpreter, [str(script)]


# ── setup ────────────────────────────────────────────

def cmd_setup(args):
    ui.header(f"MMU installer {__version__}")

    # 1. Docker -------------------------------------------------------------
    ui.step("Checking Docker")
    try:
        ui.ok(dockerctl.check())
    except dockerctl.DockerError as e:
        ui.fail(str(e))
        return 1

    # 2. Where it goes ------------------------------------------------------
    if args.dir:
        project_dir = Path(args.dir).expanduser().resolve()
    else:
        project_dir = payload.find_existing()

    if project_dir and payload.looks_like_mmu(project_dir):
        ui.ok(f"Using the MMU project already at {project_dir}")
    else:
        project_dir = project_dir or payload.default_install_dir()
        if args.interactive:
            project_dir = Path(
                ui.ask("Install MMU to", default=str(project_dir))
            ).expanduser().resolve()
        ui.step(f"Installing to {project_dir}")

    try:
        written = payload.materialize(project_dir)
    except RuntimeError as e:
        ui.fail(str(e))
        return 1
    if written:
        ui.ok(f"Wrote {len(written)} project file(s)")

    # 3. Configuration ------------------------------------------------------
    env_path = project_dir / ".env"
    template_path = project_dir / ".env.example"
    if not template_path.exists():
        ui.fail(f"{template_path} is missing; cannot generate configuration.")
        return 1

    template = template_path.read_text(encoding="utf-8")
    current = env_values(project_dir)
    updates = {}

    ui.header("Database password")
    password = args.password or current.get("NEO4J_PASS", "")
    if password in ("", "change-me", "pick-something"):
        password = envfile.generate_password()
        ui.ok("Generated a password for Neo4j")
        ui.info(f"Stored in {env_path} as NEO4J_PASS. Nothing else needs to know it.")
    else:
        ui.ok("Keeping the existing NEO4J_PASS")
    updates["NEO4J_PASS"] = password

    # 4. Embeddings ---------------------------------------------------------
    ui.header("Embedding endpoint")
    if args.embedding_dim and args.embedding_model:
        base = args.embedding_base or "http://127.0.0.1:1234/v1"
        updates["MMU_EMBEDDING_BASE"] = embedding.to_container_base(base)
        updates["MMU_EMBEDDING_MODEL"] = args.embedding_model
        updates["MMU_EMBEDDING_DIM"] = str(args.embedding_dim)
        ui.ok(f"Using your settings: {args.embedding_model}, dim {args.embedding_dim}")
    else:
        ui.step("Looking for a local embeddings server")
        found = embedding.probe(on_step=ui.info)
        if found:
            ui.ok(f"{found['label']}: {found['model']} returns {found['dim']} dimensions")
            ui.info(f"The container will reach it at {found['container_base']}")
            updates["MMU_EMBEDDING_BASE"] = found["container_base"]
            updates["MMU_EMBEDDING_MODEL"] = found["model"]
            updates["MMU_EMBEDDING_DIM"] = str(found["dim"])
        else:
            ui.warn("No embeddings endpoint answered on the usual ports.")
            ui.info("MMU will still start, on keyword recall alone. Semantic recall")
            ui.info("stays off until an endpoint is reachable: start LM Studio or")
            ui.info("Ollama, load an embedding model, then run `mmu-setup doctor`.")
            if args.interactive and ui.confirm("Enter the endpoint manually?", default=False):
                base = ui.ask("Base URL", default="http://127.0.0.1:1234/v1")
                model = ui.ask("Model name")
                if base and model:
                    dim, reason = embedding.measure(base, model)
                    if dim:
                        ui.ok(f"{model} returns {dim}-dimensional vectors")
                        updates["MMU_EMBEDDING_BASE"] = embedding.to_container_base(base)
                        updates["MMU_EMBEDDING_MODEL"] = model
                        updates["MMU_EMBEDDING_DIM"] = str(dim)
                    else:
                        ui.warn(f"Could not embed with that model: {reason}")

    # Keep anything the user already set that we are not deliberately changing.
    for key, value in current.items():
        updates.setdefault(key, value)

    env_path.write_text(envfile.render(template, updates), encoding="utf-8")
    ui.ok(f"Wrote {env_path}")

    problems = envfile.needs_attention(updates)
    for problem in problems:
        ui.warn(problem)
    if problems:
        return 1

    state.save(project_dir=project_dir)

    # 5. Start it -----------------------------------------------------------
    ui.header("Starting MMU")
    ui.info("The first run builds the server image and downloads Neo4j.")
    ui.info("Expect a few minutes, and a lot of output.")
    result = dockerctl.compose(project_dir, "up", "-d", "--build", stream=True)
    if result.returncode != 0:
        ui.fail("docker compose up failed. The output above says why.")
        return 1
    ui.ok("Containers started")

    base = server_base(project_dir)
    api_key = updates.get("MMU_API_KEY") or None
    ui.step(f"Waiting for {base}/health")
    body, error = server.wait_for_health(
        base,
        api_key=api_key,
        on_wait=lambda left, err: ui.info(f"still starting, {left}s to go ({err})"),
    )
    if body is None:
        ui.fail(f"The server did not come up: {error}")
        ui.info("Inspect it with: mmu-setup logs mmu-server")
        return 1
    ui.ok(f"MMU is up -- {body.get('total_memories', 0)} memories in the graph")

    lines, mismatch = server.self_check(project_dir)
    if mismatch:
        ui.fail("Embedding dimension mismatch -- semantic recall will not work.")
        for line in lines:
            ui.info(line.strip())
        ui.info("Correct MMU_EMBEDDING_DIM in .env, drop the memory_embedding")
        ui.info("index, restart, then POST /backfill_embeddings.")
        return 1

    # 6. Wire up the clients ------------------------------------------------
    if not args.no_connect:
        connect_clients(project_dir, base, interactive=args.interactive)

    ui.header("Done")
    ui.info(f"Project   {project_dir}")
    ui.info(f"Server    {base}")
    ui.info("Next      restart your chat client so it picks up the MCP server")
    return 0


def connect_clients(project_dir, base, interactive=True):
    ui.header("Connecting your chat client")
    command, cmd_args = bridge_command(project_dir)
    entry = mcpconfig.build_entry(command, cmd_args, mmu_base=base)

    clients = mcpconfig.detect_clients()
    if not clients:
        ui.warn("No MCP client found (looked for Claude Desktop and LM Studio).")
        ui.info("Add this to your client's MCP config by hand:")
        for line in json.dumps({"mcpServers": {mcpconfig.SERVER_KEY: entry}}, indent=2).splitlines():
            ui.info(line)
        return

    for label, path in clients:
        if interactive and not ui.confirm(f"Configure {label}?", default=True):
            continue
        action, backup_path, error = mcpconfig.install(path, entry)
        if error:
            ui.fail(error)
            continue
        if action == "unchanged":
            ui.ok(f"{label}: already configured")
        else:
            ui.ok(f"{label}: {action} in {path}")
        if backup_path:
            ui.info(f"previous config saved as {backup_path.name}")


# ── lifecycle ────────────────────────────────────────

def cmd_start(args):
    project_dir = resolve_project_dir(args)
    ui.step(f"Starting MMU in {project_dir}")
    if dockerctl.compose(project_dir, "up", "-d", stream=True).returncode != 0:
        ui.fail("docker compose up failed.")
        return 1
    base = server_base(project_dir)
    body, error = server.wait_for_health(
        base, timeout=120, api_key=env_values(project_dir).get("MMU_API_KEY") or None
    )
    if body is None:
        ui.fail(f"Containers started, but {base}/health never answered: {error}")
        return 1
    ui.ok(f"MMU is up at {base} -- {body.get('total_memories', 0)} memories")
    return 0


def cmd_stop(args):
    project_dir = resolve_project_dir(args)
    ui.step(f"Stopping MMU in {project_dir}")
    if dockerctl.compose(project_dir, "down", stream=True).returncode != 0:
        ui.fail("docker compose down failed.")
        return 1
    ui.ok("Stopped. Your memories live on the Docker volumes and are untouched.")
    return 0


def cmd_restart(args):
    code = cmd_stop(args)
    if code:
        return code
    return cmd_start(args)


def cmd_logs(args):
    project_dir = resolve_project_dir(args)
    compose_args = ["logs", "--tail", str(args.tail)]
    if args.follow:
        compose_args.append("--follow")
    if args.service:
        compose_args.append(args.service)
    try:
        dockerctl.compose(project_dir, *compose_args, stream=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args):
    project_dir = resolve_project_dir(args)
    ui.header("MMU status")
    ui.info(f"Project {project_dir}")

    try:
        ui.ok(dockerctl.check())
    except dockerctl.DockerError as e:
        ui.fail(str(e))
        return 1

    print((dockerctl.compose(project_dir, "ps").stdout or "").rstrip())

    values = env_values(project_dir)
    base = server_base(project_dir)
    body, error = server.health(base, api_key=values.get("MMU_API_KEY") or None)
    if body is None:
        ui.fail(f"{base}/health -- {error}")
        return 1
    ui.ok(f"{base} -- {body.get('total_memories', 0)} memories, "
          f"read path {body.get('read_path')}")
    colours = body.get("color_summary") or {}
    if colours:
        ui.info(", ".join(f"{k}: {v}" for k, v in sorted(colours.items())))
    return 0


def cmd_doctor(args):
    """
    Check every prerequisite and report all of them.

    Deliberately does not stop at the first failure: someone filing a bug should
    be able to paste one output that answers every obvious question at once.
    """
    ui.header("MMU doctor")
    failures = 0

    ui.step("Docker")
    try:
        ui.ok(dockerctl.check())
    except dockerctl.DockerError as e:
        ui.fail(str(e))
        failures += 1

    project_dir = resolve_project_dir(args, required=False)
    ui.step("Project files")
    if project_dir is None:
        ui.fail("No MMU installation found. Run `mmu-setup setup`.")
        failures += 1
    else:
        ui.ok(str(project_dir))
        missing = [n for n in payload.PAYLOAD_FILES if not (project_dir / n).exists()]
        if missing:
            ui.warn("missing: " + ", ".join(missing))

    values = env_values(project_dir)
    ui.step("Configuration")
    if not values:
        ui.fail("No .env found. Run `mmu-setup setup`.")
        failures += 1
    else:
        problems = envfile.needs_attention(values)
        for problem in problems:
            ui.fail(problem)
        failures += len(problems)
        if not problems:
            ui.ok(".env looks sane")

    ui.step("Embedding endpoint")
    configured_base = values.get("MMU_EMBEDDING_BASE", "")
    model = values.get("MMU_EMBEDDING_MODEL", "")
    if not configured_base or not model:
        ui.warn("not configured")
    else:
        # We probe from the host, where host.docker.internal usually does not
        # resolve -- the container is the one that needs that name.
        host_base = configured_base.replace(embedding.DOCKER_HOST_ALIAS, "127.0.0.1")
        dim, reason = embedding.measure(
            host_base, model, api_key=values.get("MMU_EMBEDDING_API_KEY") or None
        )
        if dim is None:
            ui.warn(f"{host_base} unreachable, or not an embedding model ({reason})")
            ui.info("Semantic recall is off until this answers. Keyword recall still works.")
        else:
            configured = values.get("MMU_EMBEDDING_DIM")
            if configured and str(dim) != str(configured):
                ui.fail(f"DIMENSION MISMATCH: the model returns {dim}, .env says {configured}")
                ui.info("Semantic recall cannot work until these agree.")
                failures += 1
            else:
                ui.ok(f"{model} at {host_base} returns {dim} dimensions")

    ui.step("Server")
    if project_dir:
        base = server_base(project_dir)
        body, error = server.health(base, api_key=values.get("MMU_API_KEY") or None)
        if body is None:
            ui.fail(f"{base}/health -- {error}")
            failures += 1
        else:
            ui.ok(f"{base} -- {body.get('total_memories', 0)} memories")

    ui.step("MCP clients")
    for label, path in mcpconfig.known_clients():
        if path is None or not path.exists():
            ui.info(f"{label}: no config at {path}")
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception as e:
            ui.warn(f"{label}: config is not valid JSON ({e})")
            continue
        if mcpconfig.SERVER_KEY in (config.get("mcpServers") or {}):
            ui.ok(f"{label}: MMU is registered")
        else:
            ui.warn(f"{label}: MMU is not registered -- run `mmu-setup connect`")

    print()
    if failures:
        ui.fail(f"{failures} problem(s) found.")
        return 1
    ui.ok("Everything checks out.")
    return 0


def cmd_connect(args):
    project_dir = resolve_project_dir(args)
    connect_clients(project_dir, server_base(project_dir), interactive=args.interactive)
    return 0


def disconnect_clients(project_dir=None):
    """
    Unregister MMU from every known client.

    `project_dir` scopes the removal to entries that actually launch that
    project's bridge. A machine can hold several MMU checkouts, and the client
    config names exactly one; tearing down a scratch copy must not silently
    unregister the install the user works in every day.
    """
    for label, path in mcpconfig.known_clients():
        if path is None or not path.exists():
            continue
        action, backup_path, error = mcpconfig.remove(path, project_dir=project_dir)
        if error:
            ui.fail(error)
        elif action == "removed":
            ui.ok(f"{label}: removed (backup {backup_path.name})")
        elif action == "foreign":
            ui.info(f"{label}: left alone -- it points at a different MMU install")
        else:
            ui.info(f"{label}: was not configured")
    return 0


def cmd_disconnect(args):
    # A bare `disconnect` is an explicit request to unregister MMU wherever it is
    # registered, so it is deliberately not scoped to one project.
    return disconnect_clients(project_dir=None)


def cmd_uninstall(args):
    project_dir = resolve_project_dir(args)
    ui.header("Uninstall MMU")
    ui.info(f"Project {project_dir}")

    # Deleting the volumes deletes the graph, and it exists nowhere else. So it
    # is opt-in, and confirmed separately from merely stopping the containers.
    purge = args.purge
    if args.interactive and not purge:
        purge = ui.confirm("Also delete all stored memories (the Docker volumes)?",
                           default=False)
        if purge and not ui.confirm("This permanently destroys your graph. Sure?",
                                    default=False):
            purge = False

    compose_args = ["down", "-v"] if purge else ["down"]
    dockerctl.compose(project_dir, *compose_args, stream=True)
    ui.ok("Containers removed" + (" and volumes deleted" if purge else ""))

    disconnect_clients(project_dir=project_dir)
    ui.info(f"Project files remain at {project_dir}; delete that folder to finish.")
    return 0


# ── entry point ──────────────────────────────────────

def build_parser():
    # Flags that make sense for every verb, defined once and attached both to the
    # top level and to each subcommand. SUPPRESS is what makes that safe: without
    # it the subparser writes its own default over a value given before the verb,
    # so `mmu-setup --dir X doctor` would silently lose the --dir.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS,
                        help="path to the MMU project directory")
    common.add_argument("--non-interactive", dest="interactive", action="store_false",
                        default=argparse.SUPPRESS,
                        help="never prompt; accept every default")

    parser = argparse.ArgumentParser(
        prog="mmu-setup",
        parents=[common],
        description="Install, run and diagnose MMU (Modular Memory Unit).",
    )
    parser.add_argument("--version", action="version", version=f"mmu-setup {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", parents=[common],
                             help="install and start MMU (the wizard)")
    p_setup.add_argument("--password", help="NEO4J_PASS to use instead of a generated one")
    p_setup.add_argument("--embedding-base", help="e.g. http://127.0.0.1:1234/v1")
    p_setup.add_argument("--embedding-model", help="embedding model name")
    p_setup.add_argument("--embedding-dim", type=int, help="vector width the model returns")
    p_setup.add_argument("--no-connect", action="store_true",
                         help="do not touch any MCP client config")
    p_setup.set_defaults(func=cmd_setup)

    simple = [
        ("start", "start the containers", cmd_start),
        ("stop", "stop the containers", cmd_stop),
        ("restart", "stop, then start", cmd_restart),
        ("status", "show container and server state", cmd_status),
        ("doctor", "check every prerequisite", cmd_doctor),
        ("connect", "register MMU with your MCP clients", cmd_connect),
        ("disconnect", "unregister MMU from your MCP clients", cmd_disconnect),
    ]
    for name, help_text, func in simple:
        sub.add_parser(name, parents=[common], help=help_text).set_defaults(func=func)

    p_logs = sub.add_parser("logs", parents=[common], help="show container logs")
    p_logs.add_argument("service", nargs="?", help="mmu-server or neo4j")
    p_logs.add_argument("--tail", default=200)
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.set_defaults(func=cmd_logs)

    p_uninstall = sub.add_parser("uninstall", parents=[common],
                                 help="remove containers and client config")
    p_uninstall.add_argument("--purge", action="store_true",
                             help="also delete the volumes, destroying all memories")
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


def sub_parsers(parser):
    """Every registered subcommand name, asked of argparse rather than hardcoded."""
    for action in parser._subparsers._group_actions if parser._subparsers else []:
        if hasattr(action, "choices") and action.choices:
            return list(action.choices)
    return []


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # No verb means someone double-clicked the binary, which is the commonest way
    # this will ever be launched. Do the obvious thing -- but leave --help and
    # --version alone, or they end up parsed as arguments to `setup`, which does
    # not define them, and the user gets a usage error instead of an answer.
    verbs = set(sub_parsers(parser))
    asks_for_help = any(a in ("-h", "--help", "--version") for a in argv)
    if not asks_for_help and not any(a in verbs for a in argv):
        argv = ["setup", *argv]

    args = parser.parse_args(argv)
    # SUPPRESS means these exist only when the user actually passed them.
    if not hasattr(args, "dir"):
        args.dir = None
    if not hasattr(args, "interactive"):
        args.interactive = ui.interactive()

    try:
        code = args.func(args)
    except KeyboardInterrupt:
        print()
        ui.warn("Interrupted.")
        code = 130
    except dockerctl.DockerError as e:
        ui.fail(str(e))
        code = 1

    # A double-clicked console window closes the instant the process exits,
    # taking the error message with it.
    if payload.is_frozen() and sys.platform == "win32" and ui.interactive() \
            and not os.environ.get("MMU_NO_PAUSE"):
        try:
            input("\n  Press Enter to close. ")
        except Exception:
            pass
    return code
