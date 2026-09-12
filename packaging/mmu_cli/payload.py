"""
Locate, or lay down, the files docker compose needs.

The compose file builds the server image from source (`build: .`), so the
installer cannot work from nothing -- it needs docker-compose.yml, the Dockerfile
and the four server modules on disk somewhere. Two situations have to work:

  1. The user cloned the repo and ran the binary inside it. Use what is there.
  2. The user downloaded one file and ran it. Everything is bundled inside the
     binary, so write it out to a real directory and use that.

Case 2 is what makes this a standalone installer rather than a helper script.
"""

import os
import shutil
import sys
from pathlib import Path

# Everything needed for `docker compose up` to build and run. The Dockerfile
# COPYs exactly these four modules, so this list has to track it.
PAYLOAD_FILES = [
    "docker-compose.yml",
    "Dockerfile",
    "requirements.txt",
    "requirements-host.txt",
    ".env.example",
    "mmu_server.py",
    "neo4j_layer.py",
    "light_index_v2.py",
    "ingest.py",
    "mmu_mcp_server.py",
]

MARKER = "docker-compose.yml"


def is_frozen():
    return getattr(sys, "frozen", False)


def bundle_dir():
    """
    Where our own data files live.

    PyInstaller unpacks a onefile bundle into a temp directory named by
    sys._MEIPASS; that directory is deleted when the process exits, so anything
    we need to survive has to be copied out, not referenced in place.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # Running from a source checkout: packaging/mmu_cli/payload.py -> repo root.
    return Path(__file__).resolve().parent.parent.parent


def payload_source():
    """Directory holding the bundled copies, or None when unavailable."""
    if is_frozen():
        candidate = bundle_dir() / "payload"
        return candidate if candidate.is_dir() else None
    root = bundle_dir()
    return root if (root / MARKER).exists() else None


def looks_like_mmu(directory):
    directory = Path(directory)
    if not (directory / MARKER).exists():
        return False
    text = (directory / MARKER).read_text(encoding="utf-8", errors="replace")
    return "mmu-server" in text or "MMU_PORT" in text


def find_existing(start=None):
    """
    Walk up from `start` looking for an MMU checkout.

    Running the binary from inside a clone should use that clone -- the user's
    edits, their .env and their existing volumes all live there, and quietly
    installing a second copy elsewhere would strand them.
    """
    current = Path(start or os.getcwd()).resolve()
    for directory in [current, *current.parents]:
        if looks_like_mmu(directory):
            return directory
    return None


def default_install_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "MMU"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MMU"
    data_home = os.environ.get("XDG_DATA_HOME")
    return Path(data_home) / "mmu" if data_home else Path.home() / ".local" / "share" / "mmu"


def materialize(target, overwrite_sources=True):
    """
    Write the bundled project files into `target`.

    Source modules are refreshed so that re-running a newer installer upgrades an
    existing install. .env is never touched: it holds the user's Neo4j password,
    and losing it means losing access to the graph the password protects.
    """
    source = payload_source()
    if source is None:
        raise RuntimeError(
            "This build has no bundled project files, and no MMU checkout was "
            "found. Run the installer from inside a clone of the repository."
        )

    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    written = []

    for name in PAYLOAD_FILES:
        src = Path(source) / name
        if not src.exists():
            continue
        dst = target / name
        if dst.exists() and not overwrite_sources:
            continue
        shutil.copy2(src, dst)
        written.append(name)

    # /ingest mounts this read-only; compose fails to start if it is missing.
    docs = target / "documents"
    docs.mkdir(exist_ok=True)
    keep = docs / ".gitkeep"
    if not keep.exists():
        keep.write_text("", encoding="utf-8")

    return written
