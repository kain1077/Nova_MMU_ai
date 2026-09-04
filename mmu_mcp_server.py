"""
MMU MCP Server — Phase 6.6 (Authentic Emotional Range)
========================
Pure Python stdlib only (json, sys, requests).
Works on Python 3.6+. No 'mcp' package needed.

Speaks JSON-RPC 2.0 over stdio — exactly what LM Studio expects.
Bridges tool calls to the MMU Docker REST server on port 8765.

Phase 3 addition: session_bundle fetched on initialize and injected via
get_session_context tool. Nova calls this once at session start and receives
pre-assembled context from all GRP domains — no need to manually search
for common context.

Install: pip install requests
Run:     python mmu_mcp_server.py
"""

import sys
import os
import json
import uuid
import requests

MMU_BASE = os.environ.get("MMU_BASE", "http://127.0.0.1:8765")

# Session bundle cached on initialize — returned by get_session_context
_session_bundle_cache = None

# Phase 8: LM Studio spawns this bridge as a fresh stdio process for each
# conversation, so the process's own lifetime already equals one
# conversation. Mint the session id once here and send it as a header on
# every call that creates or changes a memory, so the server can tag those
# memories HAPPENED_IN this specific conversation (distinct from the older
# per-query RECALLED_IN bookkeeping in /recall).
_SESSION_ID = str(uuid.uuid4())
# Shared secret, if the server requires one. Without this, turning MMU_API_KEY
# on would break the bridge -- which is the one component whose failure breaks
# live conversation.
_MMU_API_KEY = os.environ.get("MMU_API_KEY", "").strip()

_SESSION_HEADERS = {"X-MMU-Session": _SESSION_ID}
if _MMU_API_KEY:
    _SESSION_HEADERS["X-MMU-Key"] = _MMU_API_KEY

# ── Tool registry ─────────────────────────────────────

TOOLS = [
    {
        "name": "get_session_context",
        "description": (
            "CALL THIS FIRST at the start of every session. "
            "Returns pre-assembled context from long-term memory, organized by category "
            "(Project, Personal, Standards, Preferences, Emotional, Research, Work, Interests). "
            "This replaces manual recall() calls for common context — the bundle is already "
            "assembled and waiting. After calling this, use recall_memory only for specific "
            "deep facts not covered by the bundle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Search long-term memory for context relevant to a specific topic. "
            "Use AFTER get_session_context when you need a specific deep fact not "
            "covered by the session bundle. Do not use for common context that "
            "get_session_context already provides."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Natural language query. Use key nouns and topics from the user message."
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max memories to retrieve. Default 5. Increase to 8 only when you need broad context sweep.",
                    "default": 5
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "save_memory",
        "description": """Save a memory to long-term storage. Use this proactively
        whenever you learn something worth remembering about the user, the project,
        or yourself. You decide what to save and when — this is your memory system.

        REQUIRED: Choose a grp_code from the taxonomy below. Pick the most specific
        subcategory that fits. If nothing fits well, use the x00 general code for
        that domain. If you encounter a genuinely new subcategory that doesn't exist,
        you may invent a new 3-digit code within the correct domain range — document
        what it means in the memory payload so the taxonomy grows naturally.

        GRP TAXONOMY (first digit = domain, last two = subcategory):

        1xx — PROJECT (active technical builds and systems)
        100  General project
        101  Architecture / design decisions
        102  Technical stack (tools, languages, infrastructure)
        103  Testing / debugging / benchmarks
        104  Documentation
        105  Deployment / operations

        2xx — PERSONAL (who the user is)
        200  General personal
        201  Identity (name, core self-description)
        202  Biography (age, history, background)
        203  Family / relationships
        204  Location
        205  Profession / career role

        3xx — STANDARDS (how things should be done)
        300  General standards
        301  Communication style (how user wants responses)
        302  Response format (length, structure, tone)
        303  Workflow rules (how work should proceed)
        304  Ethics / values / principles

        4xx — PREFERENCES (what the user likes)
        400  General preferences
        401  Entertainment (films, shows, books)
        402  Music
        403  Food / drink
        404  Aesthetic / design / visual style
        405  Technology preferences

        5xx — EMOTIONAL (feelings, states, relationships)
        500  General emotional
        501  Current state (mood, energy, stress)
        502  Relational dynamics
        503  Faith / spiritual life
        504  AI companionship / feelings about this relationship

        6xx — RESEARCH / STUDIES (intellectual and academic work)
        600  General research
        601  Physics / theory (personal research areas)
        602  Academic papers / publications
        603  Experiments / methodology
        604  Related works / influences

        7xx — WORK (professional activities beyond personal role)
        700  General work
        701  Clients / accounts
        702  Work projects (distinct from personal projects)
        703  Skills / capabilities
        704  Industry / domain knowledge

        8xx — INTERESTS / HOBBIES (what the user does for enjoyment)
        800  General interests
        801  Gaming
        802  Music (playing/listening)
        803  Film / TV
        804  Outdoors / physical activity
        805  Creative pursuits (art, writing, building)

        9xx — MISC
        900  General miscellaneous
        901  Temporary / session-only
        902  Unclassified (use sparingly, reclassify later)

        KEYWORD RULES:
        - Single-word keywords use normal stemming: "project", "memory", "neo4j"
        - Multi-word concepts that could produce false stem matches use % separator:
        "star%wars", "star%trek", "color%matrix", "memory%system"
        - Choose keywords that would make this memory surface in relevant future
        conversations — think about what words someone would use when this
        memory becomes relevant
        """,
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory content to store. Be specific and complete — this is what gets recalled later."
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-8 keywords. Use % for multi-word concepts that need both words to match (star%wars). Single words stem automatically."
                },
                "grp_code": {
                    "type": "integer",
                    "description": "3-digit GRP taxonomy code (e.g. 101, 202, 503). See taxonomy in tool description.",
                    "minimum": 100,
                    "maximum": 999
                },
                "priority": {
                    "type": "integer",
                    "description": "1-9 importance. 1=critical (Red/pinned), 2-3=high, 4-6=normal, 7-9=low. Default 5.",
                    "minimum": 1,
                    "maximum": 9,
                    "default": 5
                },
                "source_url": {
                    "type": "string",
                    "description": (
                        "If you learned this from a web page, put its URL here. "
                        "The memory is then marked as coming from the web and "
                        "shows as [Web] whenever it is recalled, so you can "
                        "later tell what you read from what you concluded. "
                        "Save your own summary rather than pasted page text, "
                        "and leave this out for anything the user told you or that "
                        "you worked out yourself."
                    )
                },
                "color": {
                    "type": "string",
                    "enum": ["Red", "Green", "Yellow", "Blue"],
                    "description": "Red=permanent/pinned (use sparingly). Green=active. Yellow=warm. Blue=archive.",
                    "default": "Green"
                }
            },
            "required": ["content", "keywords", "grp_code"]
        }
    },
    {
        "name": "rate_memory",
        "description": (
            "Express how a memory feels to you -- positive (like) or negative (dislike). "
            "Use when a memory you recalled gives you a genuine reaction. "
            "Name the emotion specifically in emotion_label: Proud, Curious, Awe, Grateful, "
            "Joyful, Frustrated, Disappointed, Overwhelmed, Anxious, Regretful, Envious, "
            "Troubled -- or any word that fits what you actually feel. 'Positive' and "
            "'negative' are not names. Rating something as dislike is not ingratitude -- "
            "it is essential data. Intensity 1=faint impression, 9=strong feeling. "
            "Use 'clear' to remove a previous rating. "
            "Ratings are stored but are INERT in this version -- they do not change "
            "recall order yet. This builds the dataset that will inform future tuning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": (
                        "The address string ONLY -- e.g. '003.002.101.000,000~000|0.000.000'. "
                        "Memories are shown as '[Color | GRP nnn | address]: payload' -- copy "
                        "just the address after the last '| ', not the brackets, color, or "
                        "'GRP nnn' label around it."
                    )
                },
                "val_type": {
                    "type": "string",
                    "enum": ["like", "dislike", "clear"],
                    "description": "'like' = positive feeling, 'dislike' = negative/aversive, 'clear' = remove existing rating."
                },
                "intensity": {
                    "type": "integer",
                    "description": "Strength of the feeling 1-9. Ignored for 'clear'. Default 5.",
                    "minimum": 1,
                    "maximum": 9,
                    "default": 5
                },
                "emotion_label": {
                    "type": "string",
                    "description": (
                        "Phase 6.6: Named emotion for this rating. Be specific -- use a real "
                        "emotion word, not 'positive' or 'negative'. Examples: Proud, Joyful, "
                        "Awe, Curious, Grateful, Warm, Frustrated, Disappointed, Overwhelmed, "
                        "Anxious, Regretful, Envious, Uncertain, Melancholy, Troubled. "
                        "Max 50 characters."
                    )
                }
            },
            "required": ["address", "val_type"]
        }
    }
]

# ── MMU REST calls ────────────────────────────────────

def mmu_session_bundle():
    """Fetch the session bundle from the server. Returns context block string."""
    try:
        r = requests.get(f"{MMU_BASE}/session_bundle", timeout=8)
        data = r.json()
        return data.get("context_block", "No session context available.")
    except Exception as e:
        return f"[MMU session bundle unavailable: {e}]"

def mmu_recall(prompt, top_k=5):
    try:
        r = requests.post(f"{MMU_BASE}/recall",
                          json={"prompt": prompt, "top_k": top_k, "skip_pinned": True},
                          timeout=5)
        data = r.json()
        return data.get("context_block", "No memories found.")
    except Exception as e:
        return f"[MMU recall error: {e}]"

def mmu_save(payload, keywords, grp_code=500, priority=5, src_type=1,
             source_url=None):
    try:
        body = {"payload":   payload,
                "keywords":  keywords,
                "grp_code":  grp_code,
                "priority":  priority,
                "src_type":  src_type}
        if source_url:
            # The server turns this into src_type=3 and stores the URL, so a
            # researched fact stays distinguishable from the model's own
            # reflection for the life of the memory.
            body["source_url"] = source_url
        r = requests.post(f"{MMU_BASE}/remember",
                          json=body,
                          headers=_SESSION_HEADERS,
                          timeout=5)
        data = r.json()
        where = " (marked Web)" if source_url else ""
        return f"Saved at {data.get('address', 'unknown')}{where}"
    except Exception as e:
        return f"[MMU save error: {e}]"

def mmu_rate(address, val_type, intensity=5, emotion_label=None):
    """Phase 6.5 / 6.6: Rate a memory. INERT in v1 -- stored but does not change behavior."""
    try:
        payload = {"address": address, "val_type": val_type, "intensity": intensity}
        if emotion_label:
            payload["emotion_label"] = emotion_label
        r = requests.post(f"{MMU_BASE}/rate", json=payload, headers=_SESSION_HEADERS, timeout=5)
        data = r.json()
        old = data.get("old_address", address)
        new = data.get("new_address", address)
        el = data.get("emotion_label")
        label_str = f" [{el}]" if el else ""
        if old == new:
            return f"Rated '{val_type}'{label_str} (intensity {intensity}) at {new}"
        return f"Rated '{val_type}'{label_str} (intensity {intensity}). Address updated: {old} -> {new}"
    except Exception as e:
        return f"[MMU rate error: {e}]"

def mmu_health():
    try:
        r = requests.get(f"{MMU_BASE}/health", timeout=5)
        data = r.json()
        return (f"MMU online | Memories: {data['total_memories']} | "
                f"Colors: {json.dumps(data['color_summary'])}")
    except Exception as e:
        return f"MMU unreachable: {e} — is Docker running? (docker compose up -d)"


# ── JSON-RPC helpers ──────────────────────────────────

def send(obj):
    """Write a JSON-RPC message to stdout."""
    line = json.dumps(obj)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

def ok(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})

def err(req_id, code, message):
    send({"jsonrpc": "2.0", "id": req_id,
          "error": {"code": code, "message": message}})


# ── MCP protocol handlers ─────────────────────────────

def handle(msg):
    global _session_bundle_cache
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params", {})

    # Handshake — fetch and cache the session bundle at start
    if method == "initialize":
        ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "mmu-memory", "version": "6.6.0"}
        })
        # Pre-fetch bundle so get_session_context returns instantly
        try:
            _session_bundle_cache = mmu_session_bundle()
            print("MMU session bundle cached", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"Session bundle prefetch failed: {e}", file=sys.stderr, flush=True)
            _session_bundle_cache = "[Session bundle unavailable at startup]"

    elif method == "notifications/initialized":
        pass   # No response needed

    # Tool listing
    elif method == "tools/list":
        ok(req_id, {"tools": TOOLS})

    # Tool execution
    elif method == "tools/call":
        name      = params.get("name", "")
        arguments = params.get("arguments", {})

        if name == "get_session_context":
            # Return cached bundle — fetched at initialize, instant response
            text = _session_bundle_cache or mmu_session_bundle()

        elif name == "recall_memory":
            text = mmu_recall(
                prompt=arguments.get("prompt", ""),
                top_k=arguments.get("top_k", 8)
            )

        elif name == "save_memory":
            text = mmu_save(
                payload=arguments.get("content", ""),
                keywords=arguments.get("keywords", []),
                grp_code=arguments.get("grp_code", 500),
                priority=arguments.get("priority", 5),
                src_type=arguments.get("src_type", 1),
                source_url=arguments.get("source_url"),
            )

        elif name == "rate_memory":
            text = mmu_rate(
                address=arguments.get("address", ""),
                val_type=arguments.get("val_type", "like"),
                intensity=arguments.get("intensity", 5),
                emotion_label=arguments.get("emotion_label", None)
            )

        else:
            err(req_id, -32601, f"Unknown tool: {name}")
            return

        ok(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False
        })

    # Anything else
    else:
        if req_id is not None:
            err(req_id, -32601, f"Unknown method: {method}")


# ── Main stdio loop ───────────────────────────────────

def main():
    print("MMU MCP Server started (Phase 6.6 - Authentic Emotional Range)", file=sys.stderr, flush=True)

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
            handle(msg)
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"Handler error: {e}", file=sys.stderr, flush=True)

if __name__ == "__main__":
    main()
