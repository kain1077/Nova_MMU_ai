"""
Talk to a running MMU server: wait for it, and read its self-check.

The README's install ends with "grep the logs for the self-check and confirm
Configured dim equals Actual dim". That is the right check and the wrong way to
ask for it -- so the installer performs it and reports the verdict.
"""

import json
import time
import urllib.error
import urllib.request

from . import dockerctl


def health(base="http://127.0.0.1:8765", timeout=3, api_key=None):
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(f"{base.rstrip('/')}/health", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def wait_for_health(base="http://127.0.0.1:8765", timeout=180, interval=3,
                    api_key=None, on_wait=None):
    """
    Poll /health until it answers.

    Generous by default: the very first `up` builds a Python image and starts
    Neo4j, which routinely takes over a minute on a cold machine. Giving up too
    early would report a broken install that was merely still starting.
    """
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        body, error = health(base, api_key=api_key)
        if body is not None:
            return body, None
        attempt += 1
        if on_wait:
            on_wait(int(deadline - time.time()), error)
        time.sleep(interval)
    return None, f"no response from {base} within {timeout}s"


def self_check(project_dir, tail=200):
    """
    Pull the startup self-check out of the container log.

    Returns (lines, mismatch) where mismatch is True when the model's real vector
    width disagrees with MMU_EMBEDDING_DIM -- the failure that leaves semantic
    recall permanently dead while everything appears to work.
    """
    result = dockerctl.compose(project_dir, "logs", "--tail", str(tail), "mmu-server")
    text = result.stdout or ""
    lines = []
    capturing = False
    for line in text.splitlines():
        if "MMU self-check" in line:
            capturing = True
            lines = []
        if capturing:
            lines.append(line)
            if line.strip().endswith("=" * 10) or "Startup complete" in line:
                capturing = False
    mismatch = "DIMENSION MISMATCH" in text
    return lines, mismatch
