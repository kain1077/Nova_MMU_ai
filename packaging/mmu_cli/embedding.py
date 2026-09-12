"""
Find the user's embeddings endpoint and measure what it actually returns.

This exists because MMU_EMBEDDING_DIM must match the model's real output width
exactly -- it is baked into the Neo4j vector index at creation time, and getting
it wrong produces a graph that accepts saves and silently never embeds them. The
README asks the user to curl the endpoint and count numbers in the response by
hand. Counting them here removes the single most error-prone step in the install.

Stdlib urllib only; this runs before anything is installed.
"""

import json
import urllib.error
import urllib.request

# Ordered by how likely each is to be what a first-time user is running.
CANDIDATES = [
    ("LM Studio", "http://127.0.0.1:1234/v1"),
    ("Ollama", "http://127.0.0.1:11434/v1"),
    ("llama.cpp", "http://127.0.0.1:8080/v1"),
    ("vLLM", "http://127.0.0.1:8000/v1"),
]

# The server runs inside Docker, where 127.0.0.1 is the container itself. Every
# base written into .env has to be rewritten to the host gateway alias, which
# docker-compose.yml wires up explicitly via extra_hosts.
DOCKER_HOST_ALIAS = "host.docker.internal"
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1")


def to_container_base(base):
    """Rewrite a host-visible base URL into one the container can reach."""
    for host in _LOCAL_HOSTS:
        for pattern in (f"//{host}:", f"//{host}/"):
            if pattern in base:
                return base.replace(host, DOCKER_HOST_ALIAS, 1)
    return base


def _get_json(url, timeout=3, payload=None, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_models(base, timeout=3, api_key=None):
    """Model ids served by an OpenAI-compatible endpoint, or [] if unreachable."""
    try:
        body = _get_json(f"{base.rstrip('/')}/models", timeout=timeout, api_key=api_key)
    except Exception:
        return []
    out = []
    for entry in body.get("data", []) or []:
        mid = entry.get("id")
        if mid:
            out.append(mid)
    return out


def measure(base, model, timeout=20, api_key=None):
    """
    Embed one short string and return the vector's real length.

    Returns (dim, None) on success, (None, reason) on failure. A chat model asked
    to embed returns an error rather than a vector, which is exactly how we tell
    embedding models apart from the rest of a mixed `/models` listing.
    """
    url = f"{base.rstrip('/')}/embeddings"
    try:
        body = _get_json(url, timeout=timeout,
                         payload={"input": "test", "model": model},
                         api_key=api_key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "endpoint requires an API key (401/403)"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)

    try:
        vector = body["data"][0]["embedding"]
    except Exception:
        return None, "response contained no embedding"
    if not isinstance(vector, list) or not vector:
        return None, "embedding was empty"
    return len(vector), None


def _rank(models):
    """Embedding models first -- names almost always say so."""
    embed = [m for m in models if "embed" in m.lower()]
    rest = [m for m in models if "embed" not in m.lower()]
    return embed + rest


def probe(api_key=None, on_step=None):
    """
    Walk the candidate endpoints and return the first working embedding model.

    Returns a dict {label, base, container_base, model, dim} or None. `on_step`
    is called with a human-readable line per attempt so the caller can show
    progress -- probing four endpoints with a timeout each is slow enough that
    silence reads as a hang.
    """
    def say(msg):
        if on_step:
            on_step(msg)

    for label, base in CANDIDATES:
        models = list_models(base, api_key=api_key)
        if not models:
            say(f"{label} ({base}) -- no response")
            continue
        say(f"{label} ({base}) -- {len(models)} model(s) offered")

        # Only try a handful; a full Ollama library can be dozens of chat models
        # and each failed attempt costs a round trip.
        for model in _rank(models)[:6]:
            dim, reason = measure(base, model, api_key=api_key)
            if dim:
                say(f"{label}: '{model}' returns {dim}-dimensional vectors")
                return {
                    "label": label,
                    "base": base,
                    "container_base": to_container_base(base),
                    "model": model,
                    "dim": dim,
                }
            say(f"{label}: '{model}' is not an embedding model ({reason})")
    return None
