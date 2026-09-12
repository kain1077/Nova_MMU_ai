"""
Register MMU with MCP clients by merging into their config, never replacing it.

The README warns that most MCP clients replace their config rather than merging,
and tells users to paste a complete file containing every server they want. That
is a good warning and a bad chore: it means adding MMU by hand risks deleting
whatever else the user had wired up. This module reads the existing file, adds
one key, and writes a timestamped backup first.
"""

import datetime
import json
import os
import shutil
import sys
from pathlib import Path

SERVER_KEY = "mmu-memory"


def claude_desktop_config():
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def lm_studio_config():
    return Path.home() / ".lmstudio" / "mcp.json"


def known_clients():
    """(label, path) for each client we know how to configure."""
    return [
        ("Claude Desktop", claude_desktop_config()),
        ("LM Studio", lm_studio_config()),
    ]


def detect_clients():
    """
    Clients that look installed.

    A client counts as present if its config file exists, or if its config
    directory does -- a freshly installed Claude Desktop creates the directory
    but writes no config until the first MCP server is added, and refusing to
    configure that case would fail exactly the user who most needs the help.
    """
    found = []
    for label, path in known_clients():
        if path is None:
            continue
        if path.exists() or path.parent.exists():
            found.append((label, path))
    return found


def build_entry(command, args=None, mmu_base="http://127.0.0.1:8765"):
    entry = {"command": str(command), "args": [str(a) for a in (args or [])]}
    entry["env"] = {"MMU_BASE": mmu_base}
    return entry


def _load(path):
    if not path.exists():
        return {}, None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON ({e}); refusing to overwrite it"


def backup(path):
    """Copy path aside with a timestamp. Returns the backup path, or None."""
    if not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_suffix(path.suffix + f".mmu-backup-{stamp}")
    shutil.copy2(path, target)
    return target


def install(path, entry, key=SERVER_KEY):
    """
    Merge one server entry into an MCP client config.

    Returns (action, backup_path, error) where action is "added", "updated" or
    "unchanged". An unparseable config is left completely alone -- rewriting it
    would destroy hand-written configuration we cannot read.
    """
    config, error = _load(path)
    if error:
        return None, None, error

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    existing = servers.get(key)
    if existing == entry:
        return "unchanged", None, None

    action = "updated" if key in servers else "added"
    backup_path = backup(path)

    servers[key] = entry
    config["mcpServers"] = servers

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return action, backup_path, None


def references(entry, project_dir):
    """
    Does this config entry launch the bridge belonging to `project_dir`?

    A machine can hold more than one MMU checkout, and the client config names
    exactly one of them. Uninstalling a scratch copy must not unregister the
    install the user actually uses, so removal is gated on the entry pointing at
    the project being removed.
    """
    if not isinstance(entry, dict):
        return False
    target = os.path.normcase(os.path.abspath(str(project_dir)))
    parts = [entry.get("command", "")] + list(entry.get("args") or [])
    for part in parts:
        if not part:
            continue
        candidate = os.path.normcase(os.path.abspath(str(part)))
        if candidate == target or candidate.startswith(target + os.sep):
            return True
    return False


def remove(path, key=SERVER_KEY, project_dir=None):
    """
    Take MMU back out of a client config, leaving every other server intact.

    With `project_dir`, an entry pointing somewhere else is left alone and
    reported as "foreign" -- the caller should say so rather than claim success.
    """
    config, error = _load(path)
    if error:
        return None, None, error
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or key not in servers:
        return "absent", None, None
    if project_dir is not None and not references(servers[key], project_dir):
        return "foreign", None, None
    backup_path = backup(path)
    del servers[key]
    config["mcpServers"] = servers
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return "removed", backup_path, None
