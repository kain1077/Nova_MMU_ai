#!/usr/bin/env python3
"""
Build the MMU binaries for the platform you are standing on.

    python packaging/build.py            # both binaries
    python packaging/build.py --only mcp # just the bridge

PyInstaller cannot cross-compile: a Windows .exe has to be built on Windows, a
macOS binary on macOS. That is why release.yml fans out across three runners
rather than building everything in one job. This script is what each of those
runners calls, and what you call locally to reproduce a release build.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
REPO_ROOT = PACKAGING.parent
DIST = REPO_ROOT / "dist"
BUILD = REPO_ROOT / "build"

TARGETS = {
    "setup": PACKAGING / "mmu-setup.spec",
    "mcp": PACKAGING / "mmu-mcp.spec",
}


def platform_tag():
    """The suffix that goes on a released binary's filename."""
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(
        platform.system(), platform.system().lower()
    )
    machine = platform.machine().lower()
    arch = {
        "amd64": "x64", "x86_64": "x64",
        "arm64": "arm64", "aarch64": "arm64",
    }.get(machine, machine)
    return f"{system}-{arch}"


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        print("PyInstaller is not installed. Install it with:")
        print("    pip install -r packaging/requirements-build.txt")
        return False


def build(target, clean=True):
    spec = TARGETS[target]
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--distpath", str(DIST), "--workpath", str(BUILD)]
    if clean:
        cmd.append("--clean")
    cmd.append(str(spec))

    print(f"\n=== building {target} ===")
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        return None

    produced = DIST / ("mmu-setup" if target == "setup" else "mmu-mcp")
    if platform.system() == "Windows":
        produced = produced.with_suffix(".exe")
    if not produced.exists():
        print(f"expected {produced}, which PyInstaller did not produce")
        return None
    return produced


def rename_for_release(path):
    """
    Give the artifact a name that says what it runs on.

    Users download these from a release page where three files sit side by side,
    so "mmu-setup.exe" alone is ambiguous about architecture on Apple silicon.
    """
    suffix = ".exe" if path.suffix == ".exe" else ""
    stem = path.stem
    target = path.with_name(f"{stem}-{platform_tag()}{suffix}")
    if target.exists():
        target.unlink()
    shutil.move(str(path), str(target))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=sorted(TARGETS), action="append",
                        help="build just this target (repeatable)")
    parser.add_argument("--no-rename", action="store_true",
                        help="leave artifacts as mmu-setup / mmu-mcp")
    args = parser.parse_args()

    if not ensure_pyinstaller():
        return 1

    targets = args.only or sorted(TARGETS)
    produced = []
    for target in targets:
        path = build(target)
        if path is None:
            print(f"\nbuild failed: {target}")
            return 1
        if not args.no_rename:
            path = rename_for_release(path)
        # The bridge is launched by a chat client, and on Unix a file without
        # the execute bit simply will not start.
        if os.name != "nt":
            path.chmod(0o755)
        produced.append(path)

    print("\n=== built ===")
    for path in produced:
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  {path}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
