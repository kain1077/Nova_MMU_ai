"""
Docker detection and Compose invocation.

MMU's server is Neo4j plus a FastAPI container, so Docker stays a genuine
prerequisite -- this module's job is to find it, prove the daemon is actually
running, and give a platform-correct instruction when it isn't. "docker: command
not found" is not a useful thing to hand a user who has never installed it.
"""

import shutil
import subprocess
import sys


class DockerError(RuntimeError):
    """Docker is missing, too old, or not running. The message is user-facing."""


INSTALL_HINT = {
    "win32": (
        "Install Docker Desktop from https://docs.docker.com/desktop/install/windows-install/\n"
        "      then start it and wait for the whale icon to stop animating."
    ),
    "darwin": (
        "Install Docker Desktop from https://docs.docker.com/desktop/install/mac-install/\n"
        "      (or `brew install --cask docker`), then launch it from Applications."
    ),
    "linux": (
        "Install Docker Engine and the Compose plugin:\n"
        "      https://docs.docker.com/engine/install/\n"
        "      then `sudo systemctl start docker` and add yourself to the `docker` group."
    ),
}


def install_hint():
    return INSTALL_HINT.get(sys.platform, INSTALL_HINT["linux"])


def _run(args, **kw):
    """Run a command, capturing both streams as text. Never raises on non-zero."""
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kw,
    )


def find_docker():
    """Absolute path to the docker binary, or None."""
    return shutil.which("docker")


def check(timeout=60):
    """
    Prove Docker is usable. Returns a version string; raises DockerError with a
    message meant to be printed verbatim.

    Three distinct failures get three distinct messages, because the fixes are
    completely different: not installed, installed but the daemon is asleep, and
    installed with only the obsolete compose v1.
    """
    exe = find_docker()
    if not exe:
        raise DockerError(
            "Docker is not installed, or not on PATH.\n      " + install_hint()
        )

    # `docker info` talks to the daemon; `docker --version` does not, and will
    # happily succeed while Docker Desktop is closed.
    try:
        info = _run([exe, "info", "--format", "{{.ServerVersion}}"], timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DockerError(
            "Docker did not respond within %ds. It may still be starting up --\n"
            "      wait for it to finish and run this again." % timeout
        )
    if info.returncode != 0:
        detail = (info.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no detail"
        raise DockerError(
            "Docker is installed but the daemon is not responding.\n"
            f"      Start Docker Desktop (or `sudo systemctl start docker`) and retry.\n"
            f"      Docker said: {tail}"
        )

    server_version = (info.stdout or "").strip()

    compose = _run([exe, "compose", "version", "--short"], timeout=timeout)
    if compose.returncode != 0:
        raise DockerError(
            "Docker Compose v2 is missing. MMU uses `docker compose` (a plugin),\n"
            "      not the older standalone `docker-compose`.\n      " + install_hint()
        )

    return f"Docker {server_version}, Compose {(compose.stdout or '').strip()}"


def compose(project_dir, *args, stream=False, timeout=None):
    """
    Run `docker compose <args>` in project_dir.

    stream=True lets output go straight to the terminal, which is what you want
    for `up`, where the image build is slow and silence looks like a hang.
    """
    exe = find_docker()
    if not exe:
        raise DockerError("Docker is not installed, or not on PATH.")
    cmd = [exe, "compose", *args]
    if stream:
        return subprocess.run(cmd, cwd=str(project_dir), timeout=timeout)
    return _run(cmd, cwd=str(project_dir), timeout=timeout)
