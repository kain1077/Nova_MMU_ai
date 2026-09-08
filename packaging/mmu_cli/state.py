"""
Remember where MMU was installed, so later commands need no arguments.

`mmu-setup` may be run from a Downloads folder and install into
%LOCALAPPDATA%; without this, every subsequent `mmu status` would have to be
told where to look.
"""

import json
import os
import sys
from pathlib import Path


def _config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "MMU"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MMU"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    return Path(config_home) / "mmu" if config_home else Path.home() / ".config" / "mmu"


def _path():
    return _config_dir() / "cli.json"


def load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(**values):
    data = load()
    data.update({k: str(v) for k, v in values.items() if v is not None})
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def project_dir():
    value = load().get("project_dir")
    return Path(value) if value else None
