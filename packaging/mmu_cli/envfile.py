"""
Read and write the project's .env, preserving the template's comments.

.env.example is 8KB of explanation about what each setting does and why. Users
read it while troubleshooting, so generating a bare key=value file would throw
away the most useful documentation in the repo. Instead we rewrite values in
place inside the template and keep every comment.
"""

import re
import secrets

# Deliberately alphanumeric. docker compose performs ${VAR} substitution using
# values from this file, so a '$' inside a password gets re-interpolated and the
# container receives something other than what is written here -- a failure that
# looks like "wrong password" with a correct-looking .env. Quotes and backslashes
# cause the same class of confusion in shells that source the file.
_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_password(length=24):
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def parse(text):
    """Key -> value for every assignment in an env file. Comments ignored."""
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def render(template_text, values):
    """
    Return template_text with each key in `values` set to its new value.

    A key already present in the template -- commented out or not -- is replaced
    where it stands, so it keeps the paragraph that explains it. Anything genuinely
    new is appended under its own heading.
    """
    lines = template_text.splitlines()
    remaining = dict(values)

    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Z_][A-Z0-9_]*)=", line)
        if not match:
            continue
        indent, key = match.group(1), match.group(2)
        if key in remaining:
            lines[i] = f"{indent}{key}={remaining.pop(key)}"

    if remaining:
        lines.append("")
        lines.append("# ── Added by the MMU installer ────────────────────────")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")

    return "\n".join(lines) + "\n"


def needs_attention(values):
    """
    Settings that are present but still placeholders.

    The template ships NEO4J_PASS=change-me so that a copied-but-unedited .env
    fails loudly at compose time rather than starting a database with a guessable
    password. Catch it here instead, where we can say something useful.
    """
    problems = []
    password = values.get("NEO4J_PASS", "").strip()
    if not password or password in ("change-me", "pick-something"):
        problems.append("NEO4J_PASS is still the placeholder value")
    elif len(password) < 8:
        problems.append("NEO4J_PASS is shorter than Neo4j's 8-character minimum")
    if "$" in password:
        problems.append(
            "NEO4J_PASS contains '$', which docker compose will try to expand"
        )
    return problems
