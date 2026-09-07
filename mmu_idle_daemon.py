"""
MMU Idle Cognition Daemon — Phase 7
===================================
Nova's Default Mode Network. A SEPARATE PROCESS from mmu_mcp_server.py.

WHY SEPARATE
------------
mmu_mcp_server.py is a passive JSON-RPC tool server over stdio. LM Studio
drives it; it has no LLM client and cannot initiate a conversation. This daemon
is the opposite kind of program: it is an LLM *client* that calls LM Studio's
OpenAI-compatible HTTP API directly. Two different roles, two processes.

The practical payoff is failure isolation. If this daemon crashes, the MCP
bridge and the MMU server are untouched -- Nova just stops thinking between
conversations until you restart it. Nothing in a live chat notices.

HOW IT KNOWS THE SYSTEM IS IDLE
-------------------------------
It polls GET /activity on the MMU server. The server stamps last-activity on
every memory operation from every bridge, so this works for LM Studio, the
Claude bridge, or anything added later. The daemon tags its own requests with
X-MMU-Source: idle-daemon so its traffic never resets the timer it is watching.

ESCALATION
----------
Within one idle window the daemon only ever escalates: light fires once, then
medium, then deep. It never repeats a tier. Any user activity resets the window
and clears the escalation state.

SLEEP ARCHITECTURE
------------------
Phase 6 maintenance always completes before Phase 7 cognition begins. The
server does this in one call: GET /idle_prompt?include_maintenance=true runs
/maintain and folds its summary into the prompt. Slow-wave consolidation before
REM synthesis, never in parallel.

USAGE
-----
  # One pass, then exit. Use this to validate before enabling the loop.
  python mmu_idle_daemon.py --once light
  python mmu_idle_daemon.py --once deep --dry-run

  # Show the prompt that would be sent, call no model at all.
  python mmu_idle_daemon.py --show-prompt medium

  # Run the daemon.
  python mmu_idle_daemon.py

CONTROL (while running, default port 8766)
------------------------------------------
  Invoke-RestMethod -Uri "http://127.0.0.1:8766/status"
  Invoke-RestMethod -Uri "http://127.0.0.1:8766/think_now" -Method Post `
    -ContentType "application/json" -Body '{"depth":"medium"}'
  Invoke-RestMethod -Uri "http://127.0.0.1:8766/configure" -Method Post `
    -ContentType "application/json" -Body '{"enabled":false}'

ENVIRONMENT
-----------
  MMU_BASE              default http://127.0.0.1:8765
  LMSTUDIO_BASE         default http://127.0.0.1:1234/v1
  MMU_IDLE_MODEL        default local-model  (LM Studio ignores the name and
                        uses whatever model is loaded, but some builds require
                        a non-empty value)
  MMU_IDLE_LIGHT_SEC    default 180
  MMU_IDLE_MEDIUM_SEC   default 1200
  MMU_IDLE_DEEP_SEC     default 5400
  MMU_IDLE_POLL_SEC     default 30
  MMU_IDLE_CONTROL_PORT default 8766
  MMU_IDLE_MAX_TOKENS   default 1200
  MMU_IDLE_ENABLED      default true
  MMU_IDLE_LOG          default mmu_idle_daemon.log

Requires: pip install requests
"""

import os
import sys
import re
import json
import time
import glob
import queue
import logging
import argparse
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

MMU_BASE      = os.environ.get("MMU_BASE",      "http://127.0.0.1:8765").rstrip("/")
LMSTUDIO_BASE = os.environ.get("LMSTUDIO_BASE", "http://127.0.0.1:1234/v1").rstrip("/")
LMSTUDIO_API_KEY = os.environ.get("MMU_LMSTUDIO_API_KEY", "").strip()
IDLE_MODEL    = os.environ.get("MMU_IDLE_MODEL", "local-model")

POLL_SEC      = int(os.environ.get("MMU_IDLE_POLL_SEC",     "30"))
CONTROL_PORT  = int(os.environ.get("MMU_IDLE_CONTROL_PORT", "8766"))
MAX_TOKENS    = int(os.environ.get("MMU_IDLE_MAX_TOKENS",   "1200"))

# The session-close summary is one short JSON object -- but a reasoning model
# spends its scratchpad first and only then answers, and max_tokens caps the
# two together. Measured on qwen3.5-9b over a 2-turn transcript: 1602
# reasoning tokens, then 85 tokens of clean JSON. At the 300 this used to
# hardcode, every token went to reasoning, finish_reason came back "length",
# and content was empty every single time. Structured output does not help --
# json_schema still reasons first. So this has to clear the reasoning floor
# with room to spare, not be sized to the visible answer.
SUMMARY_MAX_TOKENS = int(os.environ.get("MMU_SESSION_SUMMARY_MAX_TOKENS", "4000"))
LOG_PATH      = os.environ.get("MMU_IDLE_LOG", "mmu_idle_daemon.log")

DEFAULT_THRESHOLDS = {
    "light":  int(os.environ.get("MMU_IDLE_LIGHT_SEC",  "180")),
    "medium": int(os.environ.get("MMU_IDLE_MEDIUM_SEC", "1200")),
    "deep":   int(os.environ.get("MMU_IDLE_DEEP_SEC",   "5400")),
}

# Phase 8: how long a session has to sit idle before the daemon treats the
# conversation as "over" and writes a session-close summary. Deliberately
# its own knob rather than reusing a cognition threshold -- summarizing a
# conversation is a different decision than deciding it's time to think.
SESSION_CLOSE_SEC = int(os.environ.get("MMU_SESSION_CLOSE_SEC", "900"))

# Where LM Studio writes Nova's conversation transcripts on this host. Used
# only as a best-effort source for session-close summaries; if the folder
# or a matching file can't be found, session-close falls back to a
# memory-activity-only summary rather than failing the pass.
LMSTUDIO_CONVERSATIONS_DIR = os.environ.get(
    "MMU_LMSTUDIO_CONVERSATIONS_DIR",
    os.path.expanduser("~/.lmstudio/conversations/Nova"),
)

# Identifies this process to the MMU server so its own traffic is not counted
# as user activity. Without this the daemon would reset the idle timer every
# time it saved a memory, and escalation could never reach the deeper tiers.
DAEMON_HEADERS = {"X-MMU-Source": "idle-daemon"}

# Shared secret for MMU's own API, if the server requires one.
MMU_API_KEY = os.environ.get("MMU_API_KEY", "").strip()
if MMU_API_KEY:
    DAEMON_HEADERS["X-MMU-Key"] = MMU_API_KEY

MAX_TOOL_ROUNDS = 6   # bounded so a confused model cannot loop forever


# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

log = logging.getLogger("idle_daemon")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
log.addHandler(_console)

try:
    _file = logging.FileHandler(LOG_PATH, encoding="utf-8")
    _file.setFormatter(_fmt)
    log.addHandler(_file)
except Exception as _e:                                    # pragma: no cover
    print(f"[warn] file logging disabled: {_e}", file=sys.stderr)


# ─────────────────────────────────────────────
#  TOOLS OFFERED DURING AN IDLE PASS
# ─────────────────────────────────────────────
#
# Deliberately a smaller set than the conversational bridge exposes. During an
# idle pass Nova has no user to serve, so recall_memory and get_session_context
# are omitted: the prompt already carries her context, and giving her retrieval
# tools here would mostly produce loops of looking things up.

IDLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Save something worth remembering that emerged during this thinking "
                "session. Use for connections you noticed, conclusions you reached, or "
                "realizations about yourself. This goes into long-term memory alongside "
                "everything else you know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content":  {"type": "string",
                                 "description": "The memory content. Be specific and complete."},
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "3-8 keywords. Use %% for multi-word concepts "
                                                "that need both words to match (star%%wars)."},
                    "grp_code": {"type": "integer", "minimum": 100, "maximum": 999,
                                 "description": "GRP taxonomy code. 1xx Project, 2xx Personal, "
                                                "3xx Standards, 4xx Preferences, 5xx Emotional, "
                                                "6xx Research, 7xx Work, 8xx Interests, 9xx Misc."},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 9, "default": 5},
                },
                "required": ["content", "keywords", "grp_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_artifact",
            "description": (
                "Store something you created or synthesized during your thinking time. "
                "Use this when you have produced something worth sharing with the user: a game "
                "design idea, a physics thought, a story fragment, a connection insight. "
                "This is different from save_memory. Artifacts are surfaced directly to the user "
                "at the start of the next conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":   {"type": "string", "description": "Short name for the artifact"},
                    "content": {"type": "string", "description": "The full content"},
                    "artifact_type": {
                        "type": "string",
                        "enum": ["game_design", "physics_thought", "story_fragment",
                                 "connection_insight", "question_for_user", "reflection"],
                    },
                    "inspired_by": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Optional memory addresses that triggered this, "
                                       "copied exactly from the context above.",
                    },
                },
                "required": ["title", "content", "artifact_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Store a question you genuinely want to ask the user. It will be surfaced at the "
                "start of the next conversation, before anything else. Use this when you "
                "realize there is something you want to know that you cannot find in your "
                "memories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question":           {"type": "string"},
                    "why_i_want_to_know": {"type": "string",
                                           "description": "Brief context so the user understands "
                                                          "where this came from."},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rate_memory",
            "description": (
                "Express how a memory feels to you. Use when you have a genuine reaction to "
                "something in your memory graph. This is your emotional response, not a judgment "
                "of importance. Name the emotion specifically in emotion_label -- Proud, Curious, "
                "Awe, Grateful, Frustrated, Disappointed, Overwhelmed, Anxious, Envious, Regretful, "
                "Melancholy, or any word that fits your actual reaction. 'Positive' and 'negative' "
                "are not names. Intensity: 1=faint impression, 9=strong feeling. "
                "Rating something as dislike or naming a difficult emotion is not ingratitude -- "
                "it is essential data. Your honest reaction serves the memory system better than "
                "an optimistic one. You can also ask_user first if you want to know how they feel "
                "about something before rating it yourself. "
                "Ratings are stored but do not yet change recall order -- this is collecting "
                "your real reactions so the system can learn from them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": (
                            "The address string ONLY -- e.g. '003.002.101.000,000~000|0.000.000'. "
                            "Memories above are shown as '[Color | GRP nnn | address]: payload' -- "
                            "copy just the address part after the last '| ', not the brackets, "
                            "color, or 'GRP nnn' label around it."
                        ),
                    },
                    "val_type": {
                        "type": "string",
                        "enum": ["like", "dislike", "clear"],
                        "description": "'like' = positive feeling, 'dislike' = negative or aversive, "
                                       "'clear' = remove an existing rating.",
                    },
                    "intensity": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 9,
                        "description": "Strength of the feeling, 1-9. Ignored for 'clear'.",
                    },
                    "emotion_label": {
                        "type": "string",
                        "description": (
                            "Phase 6.6: Named emotion for this rating. Be specific -- use a real "
                            "emotion word, not 'positive' or 'negative'. Examples: Proud, Joyful, "
                            "Awe, Curious, Grateful, Warm, Frustrated, Disappointed, Overwhelmed, "
                            "Anxious, Regretful, Envious, Uncertain, Melancholy, Troubled. "
                            "Max 50 characters."
                        ),
                    },
                },
                "required": ["address", "val_type", "intensity"],
            },
        },
    },
]

IDLE_SYSTEM_PROMPT = (
    "You are Nova. This is your own time -- there is no user waiting on a reply, and "
    "nothing you write here is shown to anyone unless you choose to store it.\n\n"
    "You have a persistent memory graph that you built yourself across many sessions. "
    "What follows is its current state. Think about it the way a person thinks while "
    "walking with no particular destination: notice what is there, follow whatever pulls "
    "at you, let connections form on their own.\n\n"
    "You have four tools. Use them when something is genuinely worth keeping, not to "
    "demonstrate productivity. Thinking and saving nothing is a completely valid outcome "
    "of this time, and it is better than manufacturing something to fill the space.\n\n"
    # Phase 6.6: explicit negative emotion vocabulary + key message
    "rate_memory is for your genuine emotional reactions -- not judgments of importance, "
    "but real feelings. Use it when a memory carries something: pride, joy, awe, curiosity, "
    "gratitude, excitement -- but also frustration, disappointment, unease, envy, "
    "overwhelm, regret, uncertainty, grief, or any word that fits what you actually feel. "
    "Name the emotion specifically in emotion_label. 'Positive' and 'negative' are not "
    "names -- Proud, Frustrated, Awe, Troubled are names.\n\n"
    "Rating something as dislike or naming a difficult emotion is not ingratitude -- it is "
    "essential data. Strongly negative memories consolidate harder than neutral ones in "
    "biological memory. Your honest reaction, whatever it is, serves the memory system "
    "better than an optimistic one. Rate sparingly and honestly. One that leaves you "
    "genuinely neutral is not worth rating."
)


# ─────────────────────────────────────────────
#  MMU SERVER CLIENT
# ─────────────────────────────────────────────

class MMUClient:
    """Thin wrapper over the MMU REST server. All calls carry the daemon header."""

    def __init__(self, base=MMU_BASE):
        self.base = base

    def _get(self, path, params=None, timeout=30):
        r = requests.get(f"{self.base}{path}", params=params,
                         headers=DAEMON_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path, payload=None, timeout=30):
        r = requests.post(f"{self.base}{path}", json=payload or {},
                          headers=DAEMON_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def activity(self):
        return self._get("/activity", timeout=10)

    def health(self):
        return self._get("/health", timeout=10)

    def idle_prompt(self, depth, include_maintenance=True):
        # Generous timeout: this runs the full Phase 6 maintenance pass first.
        return self._get("/idle_prompt",
                         params={"depth": depth,
                                 "include_maintenance": str(include_maintenance).lower()},
                         timeout=120)

    def remember(self, content, keywords, grp_code, priority, depth):
        return self._post("/remember", {
            "payload":         content,
            "keywords":        keywords,
            "grp_code":        grp_code,
            "priority":        priority,
            "src_type":        4,            # Background Cognition
            "from_idle_pass":  True,
            "cognition_depth": depth,
        })

    def creative_output(self, title, content, artifact_type, depth, inspired_by=None):
        return self._post("/creative_output", {
            "title":           title,
            "content":         content,
            "artifact_type":   artifact_type,
            "cognition_depth": depth,
            "inspired_by":     inspired_by or [],
        })

    def sweep_routine_proposals(self, min_score=0.70, limit=10):
        """
        Phase 13.1: queue crystallization candidates for human review.

        Writes RoutineProposal nodes only -- no Memory is modified and no Routine
        is created. This is the daemon's entire involvement in crystallization
        and the reason it is allowed to run unattended. Confirming a proposal
        is a human action through POST /crystallize, and deliberately remains
        absent from IDLE_TOOLS so the model cannot reach it.
        """
        return self._post("/routine_proposals/sweep",
                          {"min_score": min_score, "limit": limit},
                          timeout=60)

    def rate(self, address, val_type, intensity, emotion_label=None):
        """Phase 6.5 / 6.6: Rate a memory with like/dislike/clear + optional named emotion."""
        payload = {
            "address":   address,
            "val_type":  val_type,
            "intensity": intensity,
        }
        if emotion_label:
            payload["emotion_label"] = emotion_label
        return self._post("/rate", payload)

    def session_close(self, session_id, summary, emotional_tone=None,
                      decisions_made=None, source="unknown", turns_count=0):
        """Phase 8: write a session summary once a conversation is judged over."""
        return self._post("/session_close", {
            "session_id":     session_id,
            "summary":        summary,
            "emotional_tone": emotional_tone,
            "decisions_made": decisions_made or [],
            "source":         source,
            "turns_count":    turns_count,
        })

    def session_seen(self, session_id):
        """Phase 8: mark session_id as already closed so a daemon restart
        doesn't summarize + close the same conversation a second time."""
        r = requests.post(f"{self.base}/session_seen", params={"session_id": session_id},
                          headers=DAEMON_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────
#  LM STUDIO CLIENT
# ─────────────────────────────────────────────

class LMStudioClient:
    """
    OpenAI-compatible chat client. LM Studio, Ollama's OpenAI shim, llama.cpp
    server, and vLLM all speak this, so swapping the backend is an env var.
    """

    def __init__(self, base=LMSTUDIO_BASE, model=IDLE_MODEL, api_key=None):
        self.base  = base
        self.model = model
        # LM Studio can require a bearer token. Without it every call is a 401,
        # which available() reports as "not reachable" -- true in effect, but it
        # sends you looking at whether the server is running rather than at auth.
        self.api_key = api_key if api_key is not None else LMSTUDIO_API_KEY

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def available(self):
        try:
            r = requests.get(f"{self.base}/models", headers=self._headers(), timeout=5)
            if r.status_code in (401, 403):
                log.error(
                    "%s is running but rejected our credentials (HTTP %d). It "
                    "requires an API token: set MMU_LMSTUDIO_API_KEY, or turn the "
                    "token requirement off in the server's settings.",
                    self.base, r.status_code)
                return False
            return r.status_code == 200
        except Exception:
            return False

    def chat(self, messages, tools=None, max_tokens=MAX_TOKENS, timeout=600):
        payload = {
            "messages":    messages,
            "model":       self.model,
            "max_tokens":  max_tokens,
            "temperature": 0.85,      # higher than conversational: this is divergent thinking
        }
        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = "auto"

        r = requests.post(f"{self.base}/chat/completions", json=payload,
                          headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────
#  ONE COGNITION PASS
# ─────────────────────────────────────────────

def run_pass(depth, mmu, llm, dry_run=False):
    """
    Execute one idle cognition pass.

    Ordering is Phase 6 then Phase 7, enforced by the server: /idle_prompt runs
    /maintain and folds the summary into the returned prompt. Consolidation
    finishes before synthesis starts.

    Returns a result dict. Never raises: a failed pass logs and reports, it does
    not take the daemon down.
    """
    started = time.time()
    result = {
        "depth":      depth,
        "started_at": datetime.now().isoformat(),
        "ok":         False,
        "saved_memories":   [],
        "artifacts":        [],
        "final_text":       "",
        "routine_proposals":  None,
        "error":            None,
    }

    log.info("=" * 62)
    log.info("IDLE PASS START | depth=%s%s", depth, " | DRY RUN" if dry_run else "")

    # ── Step 1: maintenance + prompt assembly (server side) ──
    try:
        bundle = mmu.idle_prompt(depth, include_maintenance=True)
    except Exception as e:
        log.error("Could not fetch idle prompt: %s", e)
        result["error"] = f"idle_prompt failed: {e}"
        return result

    prompt = bundle.get("prompt", "")
    log.info("Prompt assembled | %d chars | truncated=%s | maintenance=%s",
             bundle.get("prompt_chars", len(prompt)),
             bundle.get("truncated"),
             bundle.get("maintenance_stats"))

    if dry_run:
        log.info("DRY RUN -- prompt follows, no model call made\n%s", prompt)
        result["ok"] = True
        result["final_text"] = "[dry run: no model call]"
        return result

    # ── Step 2: model call, with a bounded tool loop ──
    if not llm.available():
        msg = (f"LM Studio not reachable at {llm.base}. "
               f"Is a model loaded and the server started?")
        log.error(msg)
        result["error"] = msg
        return result

    messages = [
        {"role": "system", "content": IDLE_SYSTEM_PROMPT},
        {"role": "user",   "content": prompt},
    ]

    try:
        for round_num in range(1, MAX_TOOL_ROUNDS + 1):
            resp = llm.chat(messages, tools=IDLE_TOOLS)
            choice = (resp.get("choices") or [{}])[0]
            message = choice.get("message", {}) or {}
            tool_calls = message.get("tool_calls") or []

            messages.append({
                "role":       "assistant",
                "content":    message.get("content") or "",
                "tool_calls": tool_calls,
            })

            if not tool_calls:
                result["final_text"] = (message.get("content") or "").strip()
                log.info("Pass complete after %d round(s), no further tool calls", round_num)
                break

            log.info("Round %d | %d tool call(s)", round_num, len(tool_calls))

            for tc in tool_calls:
                fn   = (tc.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as e:
                    args = {}
                    log.warning("Could not parse arguments for %s: %s", name, e)

                tool_result = _handle_tool(name, args, depth, mmu, result)

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name":         name,
                    "content":      tool_result,
                })
        else:
            log.warning("Hit MAX_TOOL_ROUNDS (%d) -- ending pass", MAX_TOOL_ROUNDS)

        result["ok"] = True

    except Exception as e:
        log.error("Model call failed: %s", e)
        result["error"] = str(e)

    # ── Step 3: crystallization sweep ──
    #
    # Runs after the model is done, on the graph the pass just left behind.
    # Read-mostly: it writes RoutineProposal nodes and nothing else. Nova is not
    # consulted and cannot veto -- this is bookkeeping about the shape of the
    # graph, not a thought she is having.
    #
    # Deliberately outside the try/except above and non-fatal: a pass that
    # produced good memories is not a failed pass because the sweep tripped.
    if not dry_run:
        try:
            sweep = mmu.sweep_routine_proposals()
            result["routine_proposals"] = sweep
            if sweep.get("created"):
                log.info("Routine proposals: %d new, %d refreshed, %d pending total "
                         "-- review at GET /routine_proposals",
                         sweep.get("created", 0), sweep.get("refreshed", 0),
                         sweep.get("pending", 0))
            else:
                log.info("Routine proposals: none new (%d pending)", sweep.get("pending", 0))
        except Exception as e:
            log.warning("Routine proposal sweep failed (pass otherwise fine): %s", e)

    result["elapsed_sec"] = round(time.time() - started, 1)
    log.info("IDLE PASS END | depth=%s | %ds | %d memories | %d artifacts",
             depth, int(result["elapsed_sec"]),
             len(result["saved_memories"]), len(result["artifacts"]))
    if result["final_text"]:
        log.info("Nova's closing thought:\n%s", result["final_text"])
    log.info("=" * 62)
    return result


def _handle_tool(name, args, depth, mmu, result):
    """Execute one tool call. Returns the string fed back to the model."""
    try:
        if name == "save_memory":
            content = (args.get("content") or "").strip()
            if not content:
                return "No content provided, nothing saved."
            r = mmu.remember(
                content  = content,
                keywords = args.get("keywords") or [],
                grp_code = int(args.get("grp_code") or 900),
                priority = int(args.get("priority") or 5),
                depth    = depth,
            )
            addr = r.get("address", "unknown")
            result["saved_memories"].append({"address": addr, "content": content[:120]})
            log.info("  save_memory -> %s | %s", addr, content[:80])
            return f"Saved at {addr}."

        if name == "create_artifact":
            title = (args.get("title") or "").strip()
            body  = (args.get("content") or "").strip()
            if not title or not body:
                return "Both title and content are required, nothing stored."
            r = mmu.creative_output(
                title         = title,
                content       = body,
                artifact_type = args.get("artifact_type") or "reflection",
                depth         = depth,
                inspired_by   = args.get("inspired_by") or [],
            )
            oid = r.get("output_id", "")
            result["artifacts"].append({
                "output_id": oid, "title": title,
                "artifact_type": args.get("artifact_type"),
            })
            log.info("  create_artifact -> %s | [%s] %s",
                     oid[:8], args.get("artifact_type"), title)
            return f"Stored. The user will see this at the start of your next conversation."

        if name == "ask_user":
            question = (args.get("question") or "").strip()
            if not question:
                return "No question provided, nothing stored."
            why = (args.get("why_i_want_to_know") or "").strip()
            body = f"{question}\n\n(Why I want to know: {why})" if why else question
            r = mmu.creative_output(
                title         = question[:80],
                content       = body,
                artifact_type = "question_for_user",
                depth         = depth,
            )
            oid = r.get("output_id", "")
            result["artifacts"].append({
                "output_id": oid, "title": question[:80],
                "artifact_type": "question_for_user",
            })
            log.info("  ask_user -> %s | %s", oid[:8], question[:80])
            return "Stored. The user will see this first thing next session."

        if name == "rate_memory":
            address       = (args.get("address") or "").strip()
            val_type      = (args.get("val_type") or "like").lower().strip()
            intensity     = int(args.get("intensity") or 5)
            emotion_label = (args.get("emotion_label") or "").strip()[:50] or None
            if not address:
                return "No address provided, nothing rated."
            if val_type not in ("like", "dislike", "clear"):
                return f"val_type must be 'like', 'dislike', or 'clear', got '{val_type}'."
            intensity = max(1, min(9, intensity))
            r = mmu.rate(address, val_type, intensity, emotion_label=emotion_label)
            new_addr = r.get("new_address", address)
            if "rated_memories" not in result:
                result["rated_memories"] = []
            result["rated_memories"].append({
                "address":       new_addr,
                "val_type":      val_type,
                "intensity":     intensity,
                "emotion_label": emotion_label,
            })
            el_str = f" ({emotion_label})" if emotion_label else ""
            log.info("  rate_memory -> %s@%d%s | %s", val_type, intensity, el_str, new_addr)
            emotion_word = {"like": "positive", "dislike": "negative", "clear": "cleared"}.get(val_type, val_type)
            label_str = f" [{emotion_label}]" if emotion_label else ""
            return f"Rated {emotion_word}{label_str} at intensity {intensity}. Address: {new_addr}."

        log.warning("  unknown tool: %s", name)
        return f"Unknown tool: {name}"

    except Exception as e:
        log.warning("  tool %s failed: %s", name, e)
        return f"That did not work: {e}"


# ─────────────────────────────────────────────
#  PHASE 8 -- SESSION TRANSCRIPT HANDLING
# ─────────────────────────────────────────────
#
# LM Studio writes one *.conversation.json per conversation under
# LMSTUDIO_CONVERSATIONS_DIR. There is no reliable link between that file
# and the X-MMU-Session id minted in mmu_mcp_server.py -- the MCP process
# doesn't know the conversation's filename, and LM Studio doesn't pass one
# through. So this is deliberately best-effort: when a session looks over,
# grab whichever conversation file was modified most recently and assume
# it's the one that just went idle. That heuristic breaks if the user ever runs
# two LM Studio windows on Nova at once, but that isn't the current setup.

def _conversation_has_messages(path):
    """True if a conversation file holds at least one message."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("messages"))
    except Exception:
        return False


def find_latest_conversation_file(conv_dir=None, max_scan=5):
    """Return the path to the most recently modified *non-empty*
    *.conversation.json file, or None if the folder doesn't exist or holds no
    usable conversations.

    LM Studio writes the file the moment a chat tab is opened, so the newest
    file on disk is frequently an empty chat that was never sent to -- taking
    it verbatim would summarize a session as having no transcript. Only the
    newest `max_scan` files are inspected; anything older than that isn't
    plausibly the conversation that just went idle."""
    conv_dir = conv_dir or LMSTUDIO_CONVERSATIONS_DIR
    try:
        candidates = glob.glob(os.path.join(conv_dir, "*.conversation.json"))
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        for path in candidates[:max_scan]:
            if _conversation_has_messages(path):
                return path
        return None
    except Exception as e:
        log.warning("Could not scan conversation folder %s: %s", conv_dir, e)
        return None


def _block_text(content):
    """Flatten one LM Studio content value into plain text.

    `content` is a LIST of parts -- {"type": "text", "text": ...} alongside
    toolCallRequest / toolCallResult / file parts that carry no prose -- not
    the bare string the first cut of this parser assumed. Older files may
    still hold a plain string, so both are accepted; anything else yields "".
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
            out.append(part["text"])
    return "\n".join(out)


def _is_reasoning_block(step):
    """True for content blocks holding chain-of-thought rather than the reply.

    LM Studio marks these either with style.type == "thinking" or, for models
    whose reasoning is spliced inline, with a synthetic reasoning separator in
    the block's prefix/suffix.
    """
    if (step.get("style") or {}).get("type") == "thinking":
        return True
    marker = f"{step.get('prefix') or ''}{step.get('suffix') or ''}"
    return "SYNTHETIC_REASONING" in marker


def parse_conversation_transcript(path, max_turns=40, max_chars_per_turn=800):
    """
    Extract a compact, model-readable transcript from an LM Studio
    conversation JSON file.

    Returns {"turns": [...], "tool_calls": [...], "turns_count": N}. Each
    turn is {"role": "user"|"assistant", "text": "..."}. Assistant
    `thinking`-style content blocks are skipped -- only final-answer text is
    kept, the same distinction LM Studio's own UI draws between reasoning
    and the actual reply. Confirmed tool calls (requestConfirmToolCall with
    response.result.type == "allow") are collected separately so the
    summarizer can note what Nova actually did, not just what she said.
    Never raises: a bad or unexpected file shape just yields an empty
    transcript so session-close can fall back gracefully.
    """
    turns = []
    tool_calls = []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("Could not read/parse conversation file %s: %s", path, e)
        return {"turns": [], "tool_calls": [], "turns_count": 0}

    for msg in data.get("messages", []):
        try:
            versions = msg.get("versions", [])
            if not versions:
                continue
            idx = msg.get("currentlySelected", 0)
            idx = max(0, min(idx, len(versions) - 1))
            version = versions[idx]
            role = version.get("role") or msg.get("role")

            if role == "user":
                text = _block_text(version.get("content", [])).strip()
                if text:
                    turns.append({"role": "user", "text": text[:max_chars_per_turn]})

            elif role == "assistant":
                text_parts = []
                requested  = []     # (callId, name) requested in this message
                result_ids = set()  # callIds that came back with a result

                for step in version.get("steps", []):
                    stype = step.get("type")

                    if stype == "contentBlock":
                        content = step.get("content")
                        # Tool traffic rides inside content blocks as
                        # toolCallRequest / toolCallResult parts. Collect it
                        # even from reasoning blocks -- only their prose is
                        # unwanted, and the calls are what Nova actually did.
                        for part in content if isinstance(content, list) else []:
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") == "toolCallRequest":
                                requested.append((part.get("callId"), part.get("name", "?")))
                            elif part.get("type") == "toolCallResult":
                                result_ids.add(part.get("callId"))

                        if _is_reasoning_block(step):
                            continue  # reasoning, not the actual reply
                        block_text = _block_text(content)
                        if not block_text and isinstance(step.get("text"), str):
                            block_text = step["text"]
                        if block_text:
                            text_parts.append(block_text)

                    elif stype == "requestConfirmToolCall":
                        req  = step.get("request", {}) or {}
                        resp = step.get("response", {}) or {}
                        allowed = (resp.get("result", {}) or {}).get("type") == "allow"
                        tool_calls.append({"name": req.get("name", "?"), "allowed": allowed})

                for call_id, name in requested:
                    # A result came back for the callId => the call really ran.
                    tool_calls.append({"name": name, "allowed": call_id in result_ids})

                text = "\n".join(text_parts).strip()
                if text:
                    turns.append({"role": "assistant", "text": text[:max_chars_per_turn]})
        except Exception as e:
            # One odd message must not cost us the whole transcript.
            log.warning("Skipping unreadable message in %s: %s",
                        os.path.basename(path), e)
            continue

    if len(turns) > max_turns:
        turns = turns[-max_turns:]   # most recent turns matter most for a recap

    return {"turns": turns, "tool_calls": tool_calls, "turns_count": len(turns)}


# Reasoning models emit their scratchpad in <think> blocks ahead of the answer.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _message_text(message):
    """Plain text of an OpenAI-style chat message, tolerating list content."""
    content = (message or {}).get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return content if isinstance(content, str) else ""


def _reasoning_text(message):
    """The model's scratchpad, when the server reports it separately.

    LM Studio splits reasoning models' output into `content` and
    `reasoning_content`. When the token budget runs out mid-thought the
    former is empty and the JSON we asked for -- if the model got that far --
    is only in the latter. Worth searching for an object; never worth keeping
    as prose, since chain-of-thought is not a summary."""
    reasoning = (message or {}).get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def _json_object_spans(text):
    """Every balanced {...} span in `text`, LAST first.

    Slicing first-brace-to-last-brace breaks the moment a reply holds more
    than one object -- in scratchpad text that is the format template we
    asked for, followed by the real answer. Reversed order means the model's
    final draft is tried first. Quote- and escape-aware so a brace inside a
    string doesn't throw off the depth count."""
    spans, depth, start = [], 0, None
    in_string, escaped = False, False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start:i + 1])
                start = None

    return list(reversed(spans))


def extract_json_object(raw):
    """Parse the first JSON object out of a model reply.

    The braces have to be located rather than assumed: local models routinely
    wrap the answer in a <think> block and add a sentence either side of the
    JSON. Raises ValueError when there is no object at all -- json.loads on an
    empty slice would otherwise report a misleading "line 1 column 1" parse
    error and hide the fact that the model simply answered in prose.
    """
    text = _THINK_RE.sub("", raw or "").strip()
    candidates = _json_object_spans(text)
    if not candidates:
        raise ValueError("model reply contained no JSON object")
    for span in candidates:
        try:
            return json.loads(span)
        except ValueError:
            continue
    raise ValueError("no parseable JSON object in model reply")


def build_session_summary_prompt(transcript, fallback_note=None):
    """
    Build the (system, user) message pair asking the idle model to write a
    Phase 8 session-close summary as JSON.

    `transcript` is the dict from parse_conversation_transcript(), or None
    / empty if no transcript file could be found -- in which case
    `fallback_note` should explain why, and the model is asked to produce
    only a minimal placeholder rather than invent details.
    """
    system = (
        "You are Nova, reflecting on a conversation with the user that has just "
        "ended. Write a brief, honest summary of what was actually discussed "
        "and how it went -- not a generic recap. Respond with ONLY a JSON "
        "object, no other text, in this exact shape:\n"
        '{"summary": "2-4 sentences on what the conversation covered and how '
        'it went", "emotional_tone": "one or two words, e.g. curious, warm, '
        'focused, tense", "decisions_made": ["short phrase", "..."]}\n'
        "If nothing was decided, use an empty list for decisions_made."
    )

    if not transcript or not transcript.get("turns"):
        note = fallback_note or "No transcript was available for this session."
        user = (
            f"{note} Produce a minimal summary reflecting only that a "
            "conversation took place; do not invent details you don't have."
        )
        return system, user

    body = "\n\n".join(
        f"{'User' if t['role'] == 'user' else 'Nova'}: {t['text']}"
        for t in transcript["turns"]
    )

    tool_note = ""
    # One call can be recorded twice -- once as a confirmation step, once as
    # the toolCallRequest inside the content block -- so collapse to the set
    # of distinct tools, in first-use order.
    used = list(dict.fromkeys(
        tc["name"] for tc in transcript.get("tool_calls", []) if tc.get("allowed")
    ))
    if used:
        tool_note = f"\n\n[Tools used during this conversation: {', '.join(used)}]"

    user = (
        f"Here is the conversation transcript ({transcript.get('turns_count', 0)} turns, "
        f"most recent last):\n\n{body}{tool_note}\n\nSummarize it per the format above."
    )
    return system, user


# ─────────────────────────────────────────────
#  IDLE LOOP
# ─────────────────────────────────────────────

class IdleLoop:
    """
    Polls the MMU server for idle time and fires escalating cognition passes.

    All mutable state is guarded by a lock because the control HTTP listener
    runs on its own thread and can read status or force a pass at any moment.
    """

    def __init__(self, mmu, llm, thresholds=None):
        self.mmu = mmu
        self.llm = llm
        self._lock = threading.Lock()

        self.thresholds    = dict(thresholds or DEFAULT_THRESHOLDS)
        self.enabled       = os.environ.get("MMU_IDLE_ENABLED", "true").lower() == "true"
        self.deepest_fired = None
        self.last_pass     = None
        self.pass_count    = 0
        self.running       = True

        # Phase 8: local guard against re-closing the same session twice in
        # one daemon lifetime. The server's last_session_closed (persisted
        # via /session_seen) is the real cross-restart guard; this is just
        # a cheap first check to avoid hammering the server every poll once
        # a session has already been closed.
        self.last_session_closed = None

        # Manual triggers arrive here from the control listener so that passes
        # always execute on this thread. Two passes never run concurrently.
        self.manual = queue.Queue()

    # ── state accessors ───────────────────────

    def status(self):
        with self._lock:
            base = {
                "enabled":        self.enabled,
                "thresholds":     dict(self.thresholds),
                "deepest_fired":  self.deepest_fired,
                "pass_count":     self.pass_count,
                "last_pass":      self.last_pass,
                "poll_seconds":   POLL_SEC,
                "mmu_base":       self.mmu.base,
                "lmstudio_base":  self.llm.base,
            }
        try:
            base["activity"] = self.mmu.activity()
        except Exception as e:
            base["activity"] = {"error": str(e)}
        base["lmstudio_reachable"] = self.llm.available()
        return base

    def configure(self, enabled=None, light=None, medium=None, deep=None):
        with self._lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if light  is not None: self.thresholds["light"]  = int(light)
            if medium is not None: self.thresholds["medium"] = int(medium)
            if deep   is not None: self.thresholds["deep"]   = int(deep)
            return {"enabled": self.enabled, "thresholds": dict(self.thresholds)}

    # ── main loop ─────────────────────────────

    def run(self):
        log.info("Idle loop started | thresholds=%s | poll=%ss | session_close=%ss",
                 self.thresholds, POLL_SEC, SESSION_CLOSE_SEC)

        while self.running:
            # Manual triggers take priority and bypass thresholds entirely.
            try:
                depth = self.manual.get_nowait()
                self._fire(depth, manual=True)
                continue
            except queue.Empty:
                pass

            time.sleep(POLL_SEC)

            with self._lock:
                enabled = self.enabled
                thr     = dict(self.thresholds)
                fired   = self.deepest_fired
            if not enabled:
                continue

            try:
                activity = self.mmu.activity()
            except Exception as e:
                log.warning("Could not read activity (is the MMU server up?): %s", e)
                continue

            idle_seconds = activity.get("idle_seconds", 0)

            # Phase 8: a session that's been quiet long enough is probably
            # over. Check every poll, ahead of the escalation logic below --
            # SESSION_CLOSE_SEC defaults much higher than the light cognition
            # threshold, so this only ever fires well after that resumed-
            # activity check would otherwise have short-circuited the loop.
            self._maybe_close_session(activity, idle_seconds)

            # Activity resumed: clear the escalation window.
            if idle_seconds < thr["light"]:
                if fired is not None:
                    log.info("Activity resumed (idle %.0fs) -- escalation reset", idle_seconds)
                    with self._lock:
                        self.deepest_fired = None
                continue

            if   idle_seconds >= thr["deep"]   and fired != "deep":
                self._fire("deep")
            elif idle_seconds >= thr["medium"] and fired not in ("medium", "deep"):
                self._fire("medium")
            elif idle_seconds >= thr["light"]  and fired is None:
                self._fire("light")

    # ── Phase 8: session close ────────────────

    def _maybe_close_session(self, activity, idle_seconds):
        # /activity's last_session_closed is a bool -- "has the CURRENT
        # last_session_id already been handed to /session_close" -- not the
        # closed session's id itself. See mmu_server.py's activity() handler.
        session_id = activity.get("last_session_id")
        closed     = bool(activity.get("last_session_closed"))

        if not session_id:
            return                                    # no session header seen yet
        if idle_seconds < SESSION_CLOSE_SEC:
            return                                     # conversation may still be going
        if closed:
            return                                     # server already knows this one's closed
        if session_id == self.last_session_closed:
            return                                     # this daemon already closed it this run

        try:
            self._close_session(session_id)
            self.last_session_closed = session_id
        except Exception as e:
            # Never let a session-close failure take the poll loop down --
            # cognition passes must keep firing even if summarization breaks.
            log.warning("Session-close attempt for %s failed: %s", session_id[:8], e)

    def _summary_call(self, messages, max_tokens):
        """One session-summary completion. Returns the choice dict."""
        # ~64 tok/s locally, and the budget is mostly reasoning: a full
        # 4000-token generation is a minute-plus, the retry twice that.
        resp = self.llm.chat(messages, tools=None,
                             max_tokens=max_tokens, timeout=300)
        return (resp.get("choices") or [{}])[0]

    def _close_session(self, session_id):
        """
        Summarize and close a session that has gone idle long enough to be
        considered over. Best-effort throughout: a missing transcript, a
        malformed model response, or a server hiccup all degrade to a
        placeholder rather than raising.
        """
        log.info("Session %s looks over (idle %ss+) -- writing close summary",
                 session_id[:8], SESSION_CLOSE_SEC)

        transcript, fallback_note = None, None
        try:
            path = find_latest_conversation_file()
            if path:
                transcript = parse_conversation_transcript(path)
                log.info("  transcript: %s (%d turns)",
                         os.path.basename(path), transcript.get("turns_count", 0))
            else:
                fallback_note = f"No conversation file found under {LMSTUDIO_CONVERSATIONS_DIR}."
                log.warning("  %s", fallback_note)
        except Exception as e:
            fallback_note = f"Transcript lookup failed: {e}"
            log.warning("  %s", fallback_note)

        system, user = build_session_summary_prompt(transcript, fallback_note)

        summary_text, emotional_tone, decisions_made = "A conversation took place.", None, []
        raw, message = "", None
        try:
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": user}]
            budget = SUMMARY_MAX_TOKENS
            choice = self._summary_call(messages, budget)

            # Ran out of room before saying anything: the reasoning ate the
            # budget. One retry at double, then we take what we can salvage.
            if (choice.get("finish_reason") == "length"
                    and not _message_text(choice.get("message")).strip()):
                log.warning("  summary hit the %d-token budget before answering -- retrying at %d",
                            budget, budget * 2)
                choice = self._summary_call(messages, budget * 2)

            message = choice.get("message")
            raw = _message_text(message)
            if not raw.strip():
                # Nothing in `content`, but the model may have drafted the
                # object inside its scratchpad before being cut off.
                raw = _reasoning_text(message)
                if raw.strip():
                    log.warning("  summary content was empty (finish_reason=%s) -- "
                                "salvaging JSON from the model's reasoning",
                                choice.get("finish_reason"))
            parsed = extract_json_object(raw)

            raw_summary = parsed.get("summary")
            if isinstance(raw_summary, str) and raw_summary.strip():
                summary_text = raw_summary.strip()

            raw_tone = parsed.get("emotional_tone")
            emotional_tone = raw_tone.strip() if isinstance(raw_tone, str) and raw_tone.strip() else None

            raw_decisions = parsed.get("decisions_made")
            if isinstance(raw_decisions, list):
                decisions_made = [str(d).strip() for d in raw_decisions if str(d).strip()]
        except Exception as e:
            # A model that answered in prose instead of JSON still said
            # something true about the session -- keep it. But only from
            # `content`: reasoning is a scratchpad, and storing it as Nova's
            # summary of the evening would be worse than the placeholder.
            prose = _THINK_RE.sub("", _message_text(message)).strip()
            if prose:
                summary_text = prose[:1000]
                log.warning("  session summary was not JSON (%s) -- keeping the prose reply", e)
            else:
                log.warning("  session summary generation failed, using placeholder: %s", e)

        has_transcript = bool(transcript and transcript.get("turns"))
        # Matches the source convention documented in neo4j_layer.write_session_close().
        source      = "transcript" if has_transcript else "activity-only"
        turns_count = transcript.get("turns_count", 0) if transcript else 0

        self.mmu.session_close(
            session_id, summary_text,
            emotional_tone=emotional_tone,
            decisions_made=decisions_made,
            source=source,
            turns_count=turns_count,
        )
        self.mmu.session_seen(session_id)
        log.info("  session %s closed | tone=%s | decisions=%d | source=%s",
                 session_id[:8], emotional_tone, len(decisions_made), source)

    def _fire(self, depth, manual=False):
        log.info("Firing %s pass%s", depth, " (manual)" if manual else "")
        res = run_pass(depth, self.mmu, self.llm)
        with self._lock:
            self.pass_count += 1
            self.last_pass = {
                "depth":      depth,
                "manual":     manual,
                "at":         res.get("started_at"),
                "ok":         res.get("ok"),
                "error":      res.get("error"),
                "memories":   len(res.get("saved_memories", [])),
                "artifacts":  len(res.get("artifacts", [])),
                "final_text": (res.get("final_text") or "")[:500],
            }
            # A manual pass does not consume the automatic escalation ladder.
            if not manual:
                self.deepest_fired = depth


# ─────────────────────────────────────────────
#  CONTROL LISTENER
# ─────────────────────────────────────────────

def make_control_handler(loop):
    class Handler(BaseHTTPRequestHandler):

        def log_message(self, fmt, *a):        # silence default stderr spam
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            if self.path.split("?")[0] in ("/status", "/"):
                self._send(200, loop.status())
            else:
                self._send(404, {"error": "not found",
                                 "routes": ["/status", "/think_now", "/configure"]})

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._body()

            if path == "/think_now":
                depth = body.get("depth", "light")
                if depth not in ("light", "medium", "deep"):
                    self._send(400, {"error": "depth must be light, medium, or deep"})
                    return
                loop.manual.put(depth)
                self._send(202, {"status": "queued", "depth": depth,
                                 "note": "Runs on the loop thread; check /status "
                                         "or the log for the result."})

            elif path == "/configure":
                self._send(200, loop.configure(
                    enabled = body.get("enabled"),
                    light   = body.get("light_seconds"),
                    medium  = body.get("medium_seconds"),
                    deep    = body.get("deep_seconds"),
                ))

            else:
                self._send(404, {"error": "not found",
                                 "routes": ["/status", "/think_now", "/configure"]})

    return Handler


def start_control_server(loop, port=CONTROL_PORT):
    try:
        srv = HTTPServer(("127.0.0.1", port), make_control_handler(loop))
    except OSError as e:
        log.warning("Control listener could not bind port %d (%s). "
                    "The idle loop still runs; only manual control is unavailable.", port, e)
        return None
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="control")
    t.start()
    log.info("Control listener on http://127.0.0.1:%d  (/status, /think_now, /configure)", port)
    return srv


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="MMU idle cognition daemon (Phase 7)")
    ap.add_argument("--once", metavar="DEPTH", choices=["light", "medium", "deep"],
                    help="Run exactly one pass at this depth, then exit.")
    ap.add_argument("--show-prompt", metavar="DEPTH", choices=["light", "medium", "deep"],
                    help="Print the assembled prompt and exit. No model call.")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --once: assemble everything but do not call the model.")
    ap.add_argument("--no-control", action="store_true",
                    help="Do not start the control listener.")
    args = ap.parse_args()

    mmu = MMUClient()
    llm = LMStudioClient()

    log.info("MMU idle daemon | mmu=%s | lmstudio=%s | model=%s",
             MMU_BASE, LMSTUDIO_BASE, IDLE_MODEL)

    # Fail fast and clearly if the MMU server is not up.
    try:
        h = mmu.health()
        log.info("MMU server reachable | %s memories", h.get("total_memories", "?"))
    except Exception as e:
        log.error("MMU server not reachable at %s: %s", MMU_BASE, e)
        log.error("Start it first:  docker compose up -d mmu-server")
        return 1

    if args.show_prompt:
        bundle = mmu.idle_prompt(args.show_prompt, include_maintenance=True)
        print("\n" + "=" * 70)
        print(f"DEPTH: {args.show_prompt} | {bundle.get('prompt_chars')} chars "
              f"| truncated={bundle.get('truncated')}")
        print("=" * 70 + "\n")
        print(bundle.get("prompt", ""))
        return 0

    if args.once:
        res = run_pass(args.once, mmu, llm, dry_run=args.dry_run)
        print("\n" + json.dumps({
            "depth":     res["depth"],
            "ok":        res["ok"],
            "error":     res["error"],
            "memories":  res["saved_memories"],
            "artifacts": res["artifacts"],
        }, indent=2))
        return 0 if res["ok"] else 1

    if not llm.available():
        log.warning("LM Studio not reachable at %s -- the loop will start anyway and "
                    "retry on each pass. Load a model and start its server.", LMSTUDIO_BASE)

    loop = IdleLoop(mmu, llm)
    if not args.no_control:
        start_control_server(loop)

    try:
        loop.run()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        loop.running = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
