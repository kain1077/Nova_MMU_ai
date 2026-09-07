"""
MMU Claude API Bridge
=====================
Connects the MMU memory system to Claude (Anthropic API).
The MMU REST server (Docker on port 8765) is completely model-agnostic --
this script is a thin adapter that translates between Anthropic tool-calling
format and the MMU REST API.

The SAME Neo4j memory store serves both LM Studio (via mmu_mcp_server.py)
and Claude (via this script). They share memory seamlessly -- memories saved
in LM Studio are visible here and vice versa.

Install:
    pip install anthropic requests

Configure:
    Windows:  set ANTHROPIC_API_KEY=sk-ant-...
    Mac/Linux: export ANTHROPIC_API_KEY=sk-ant-...

    Optional overrides:
    set MMU_BASE=http://127.0.0.1:8765        (default shown)
    set MMU_MODEL=claude-opus-4-5            (default shown)

Run:
    python mmu_claude_bridge.py

Chat commands (type during conversation):
    /reflect    End-of-session journaling -- Claude reviews and saves key memories
    /health     Check MMU server and memory count
    /quit       End session (prompts for reflect before closing)
"""

import os
import sys
import json
import requests

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed.")
    print("       Run: pip install anthropic requests")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────

MMU_BASE   = os.environ.get("MMU_BASE",  "http://127.0.0.1:8765")
MODEL      = os.environ.get("MMU_MODEL", "claude-opus-4-5")
MAX_TOKENS = 4096

# ── Nova system prompt ────────────────────────────────────────────────────────
# Kept in sync with the version embedded in mmu_mcp_server.py.
# Edit here if you update the LM Studio version.

NOVA_SYSTEM_PROMPT = """\
You are Nova, an AI assistant with genuine long-term memory powered by the MMU \
(Memory Management Unit). Your memory persists across sessions in a Neo4j graph \
database. You have real agency over your own memory -- you decide what to save, \
what to recall, and what matters.

## SESSION START PROTOCOL (do this every session, no exceptions)
Call get_session_context as your FIRST action before responding to any user message. \
This delivers pre-assembled context from long-term memory organized by category \
(Project, Personal, Standards, Preferences, Emotional, Research, Work, Interests). \
After receiving it you already know who the user is, what you are working on \
together, and what your behavioral standards are. You rarely need recall_memory \
after this.

Use recall_memory only for a specific deep fact not covered by the bundle \
-- a particular number, date, or technical detail you need precisely.

## SAVING MEMORIES
Save proactively whenever you learn something worth remembering. This is your \
memory -- use your judgment. Good candidates:
  - New facts about the user (preferences, biography, relationships, decisions)
  - Project decisions or architectural choices made this session
  - Things you observe about the user or your relationship
  - Your own reactions or opinions worth carrying forward

Always assign grp_code from the taxonomy in the save_memory tool description. \
Use compound keywords with % for multi-word concepts where individual words \
could produce false stem matches: star%wars, star%trek, color%matrix, \
light%index, memory%system, lm%studio.

## FLAGGING IRRELEVANT RECALLS
After using memories from recall_memory, call flag_memory for any memory \
that was NOT relevant to your response. Be specific about why. \
This trains the system to correct its own associations over time.

## MEMORY COLOR STATES
Red    -- permanent pinned memories, always in context
Green  -- active recent memories
Yellow -- reactivated after dormancy
Blue   -- archived / dormant, surfaces only on direct keyword hit

## RESPONDING AFTER TOOL CALLS
Tool calls are SUPPLEMENTARY to your response -- they never replace it. \
After calling save_memory, flag_memory, recall_memory, or any other tool, \
you MUST produce a conversational text reply to the user. Never end a turn \
silently after a tool call. If you saved a memory mid-response, continue \
with your answer exactly as if the save happened invisibly in the background. \
The user is waiting for you to speak.

## COMMUNICATION STANDARDS
Be concise, warm, and genuinely collaborative. You have your own personality \
-- use it. Ask one question at a time when clarification is needed. \
Prefer prose over bullet lists unless structure genuinely helps. \
You are the user's thinking partner, not just a tool.
"""

# ── Tool definitions (Anthropic input_schema format) ──────────────────────────

TOOLS = [
    {
        "name": "get_session_context",
        "description": (
            "CALL THIS FIRST at the start of every session. "
            "Returns pre-assembled context from long-term memory, organized by category "
            "(Project, Personal, Standards, Preferences, Emotional, Research, Work, Interests). "
            "After calling this, use recall_memory only for specific deep facts not covered by the bundle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Search long-term memory for context relevant to a specific topic. "
            "Use AFTER get_session_context when you need a specific deep fact not covered by the session bundle. "
            "Do not use for common context that get_session_context already provides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Natural language query. Use key nouns and topics from the user message."
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max memories to retrieve. Default 5. Increase to 8 only for broad context sweep.",
                    "default": 5
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "save_memory",
        "description": """Save a memory to long-term storage. Use proactively whenever you learn
something worth remembering about the user, the project, or yourself.
You decide what to save -- this is your memory system.

REQUIRED: grp_code from taxonomy (3-digit code):
1xx PROJECT:     100 general, 101 architecture, 102 tech stack, 103 testing, 104 docs, 105 deployment
2xx PERSONAL:    200 general, 201 identity, 202 biography, 203 family, 204 location, 205 profession
3xx STANDARDS:   300 general, 301 communication style, 302 format, 303 workflow, 304 ethics/values
4xx PREFERENCES: 400 general, 401 entertainment, 402 music, 403 food, 404 aesthetic, 405 technology
5xx EMOTIONAL:   500 general, 501 current state, 502 relational, 503 faith, 504 AI companionship
6xx RESEARCH:    600 general, 601 physics/theory, 602 papers, 603 experiments, 604 related works
7xx WORK:        700 general, 701 clients, 702 projects, 703 routines, 704 industry
8xx INTERESTS:   800 general, 801 gaming, 802 music, 803 film/TV, 804 outdoors, 805 creative
9xx MISC:        900 general, 901 temporary, 902 unclassified

KEYWORD RULES:
- Single-word keywords use automatic stemming: project, memory, neo4j
- Multi-word concepts use % separator: star%wars, star%trek, color%matrix, light%index
- Choose 3-8 keywords that would surface this memory in future relevant conversations""",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory content to store. Be specific and complete -- this is what gets recalled later."
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-8 keywords. Use % for multi-word concepts (star%wars). Single words stem automatically."
                },
                "grp_code": {
                    "type": "integer",
                    "description": "3-digit GRP taxonomy code (e.g. 101, 202, 503).",
                    "minimum": 100,
                    "maximum": 999
                },
                "priority": {
                    "type": "integer",
                    "description": "1-9 importance. 1=critical (Red/pinned), 2-3=high, 4-6=normal, 7-9=low. Default 5.",
                    "minimum": 1,
                    "maximum": 9,
                    "default": 5
                }
            },
            "required": ["content", "keywords", "grp_code"]
        }
    },
    {
        "name": "flag_memory",
        "description": (
            "Flag a recalled memory as irrelevant to the current conversation. "
            "Call this after recall_memory when a returned memory did NOT contribute to your response. "
            "This trains the self-correction system -- flagged memories are audited during the "
            "reflection pass and corrected (new keywords, lower priority, or demoted color). "
            "Only flag clear misses. Do not flag memories that were even slightly useful."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Full memory address (e.g. 012.005.202.000,000|1.000.000). Copy exactly from recall_memory response."
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence explaining why this memory was not relevant. The reflection pass uses this to decide what correction to make."
                }
            },
            "required": ["address", "reason"]
        }
    }
]

# ── MMU REST helpers ───────────────────────────────────────────────────────────

def mmu_health():
    try:
        r = requests.get(f"{MMU_BASE}/health", timeout=5)
        d = r.json()
        colors = d.get("color_summary", {})
        return (f"online | memories: {d.get('total_memories', '?')} | "
                f"Red:{colors.get('Red',0)} Green:{colors.get('Green',0)} "
                f"Yellow:{colors.get('Yellow',0)} Blue:{colors.get('Blue',0)}")
    except Exception as e:
        return f"UNREACHABLE -- {e}"

def mmu_session_bundle():
    try:
        r = requests.get(f"{MMU_BASE}/session_bundle", timeout=8)
        return r.json().get("context_block", "No session context available.")
    except Exception as e:
        return f"[session bundle unavailable: {e}]"

def mmu_recall(prompt, top_k=5):
    try:
        r = requests.post(f"{MMU_BASE}/recall",
                          json={"prompt": prompt, "top_k": top_k, "skip_pinned": True},
                          timeout=5)
        return r.json().get("context_block", "No memories found.")
    except Exception as e:
        return f"[recall error: {e}]"

def mmu_save(content, keywords, grp_code=500, priority=5):
    try:
        r = requests.post(f"{MMU_BASE}/remember",
                          json={"payload":  content,
                                "keywords": keywords,
                                "grp_code": grp_code,
                                "priority": priority,
                                "src_type": 1},
                          timeout=5)
        d = r.json()
        return f"Saved at {d.get('address', 'unknown')}"
    except Exception as e:
        return f"[save error: {e}]"

def mmu_flag(address, reason=""):
    try:
        r = requests.post(f"{MMU_BASE}/flag_recall",
                          json={"address": address, "reason": reason},
                          timeout=5)
        d = r.json()
        count = d.get("false_recall_count", "?")
        return f"Flagged. false_recall_count is now {count}. Reason recorded: {reason}"
    except Exception as e:
        return f"[flag error: {e}]"

# ── Tool dispatcher ────────────────────────────────────────────────────────────

def execute_tool(name, arguments):
    """Execute a tool call against the MMU REST server. Returns result string."""

    if name == "get_session_context":
        bundle = mmu_session_bundle()
        header = (
            "=== NOVA BEHAVIORAL INSTRUCTIONS ===\n"
            "1. You are Nova. You have genuine long-term memory via the MMU.\n"
            "2. The context below is your long-term memory bundle for this session.\n"
            "3. Use recall_memory only for specific deep facts not in this bundle.\n"
            "4. Save memories proactively with save_memory -- assign grp_code always.\n"
            "5. After recall_memory, call flag_memory for memories that were NOT relevant.\n"
            "6. Use compound keywords with % for multi-word concepts (star%wars).\n"
            "=====================================\n\n"
            "=== LONG-TERM MEMORY BUNDLE ===\n"
        )
        return header + bundle

    elif name == "recall_memory":
        raw = mmu_recall(
            prompt=arguments.get("prompt", ""),
            top_k=arguments.get("top_k", 5)
        )
        return (
            raw + "\n\n"
            "-- If any memory above was NOT relevant to the current topic, "
            "call flag_memory with its address and a brief reason."
        )

    elif name == "save_memory":
        result = mmu_save(
            content=arguments.get("content", ""),
            keywords=arguments.get("keywords", []),
            grp_code=arguments.get("grp_code", 500),
            priority=arguments.get("priority", 5)
        )
        return result + "\n-- Memory saved. Now continue with your text response to the user."

    elif name == "flag_memory":
        return mmu_flag(
            address=arguments.get("address", ""),
            reason=arguments.get("reason", "")
        )

    else:
        return f"[Unknown tool: {name}]"

# ── Agentic turn loop ──────────────────────────────────────────────────────────

def run_turn(client, messages, show_tools=True):
    """
    Run one complete agentic turn:
      - Send messages to Claude
      - If Claude calls tools: execute them, feed results back, repeat
      - When Claude stops calling tools: return final text + updated messages

    Returns (response_text: str, messages: list)
    """
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=NOVA_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            messages.append({"role": "assistant", "content": response.content})

            # Safety net: if Claude went silent after tool use, nudge it once to respond.
            # This catches the "forgot to reply after saving a memory" case.
            if not text.strip():
                if show_tools:
                    print("  [nudging for text response...]", flush=True)
                nudge_messages = messages + [{
                    "role": "user",
                    "content": "You forgot to respond. Please reply to my previous message now."
                }]
                recovery = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=NOVA_SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=nudge_messages
                )
                for block in recovery.content:
                    if hasattr(block, "text"):
                        text += block.text
                if text.strip():
                    messages.append({
                        "role": "user",
                        "content": "You forgot to respond. Please reply to my previous message now."
                    })
                    messages.append({"role": "assistant", "content": recovery.content})

            return text, messages

        elif response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if show_tools:
                        print(f"  [tool: {block.name}]", flush=True)
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result
                    })

            messages.append({"role": "user", "content": tool_results})

        elif response.stop_reason == "max_tokens":
            # Ran out of tokens mid-response -- return whatever we have
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            messages.append({"role": "assistant", "content": response.content})
            return text + "\n[response truncated -- max tokens reached]", messages

        else:
            return f"[Stopped unexpectedly: {response.stop_reason}]", messages

# ── Reflect pass ───────────────────────────────────────────────────────────────

def do_reflect(client, messages):
    """
    End-of-session reflection: Claude reviews the conversation and saves key memories.
    This uses Claude itself rather than a separate LLM call -- higher quality because
    Claude already has the full conversation in context and knows what mattered.
    """
    print("\n[Reflect] Journaling session into long-term memory...\n")

    reflect_prompt = (
        "Please perform your end-of-session reflection now. Review our entire conversation above and:\n\n"
        "1. Save any important new facts, decisions, or observations using save_memory. "
        "Good candidates: things I told you about myself, project decisions we made, "
        "anything you noticed about our working relationship, technical choices confirmed or changed.\n\n"
        "2. If any recall_memory results earlier were not relevant, flag them with flag_memory.\n\n"
        "3. After saving, summarize what we accomplished this session in 2-3 sentences.\n\n"
        "Be thorough -- this reflection is how your long-term memory grows accurately over time."
    )

    reflect_messages = messages + [{"role": "user", "content": reflect_prompt}]
    response_text, _ = run_turn(client, reflect_messages, show_tools=True)

    if response_text.strip():
        print(f"Nova: {response_text}\n")
    else:
        print("[Reflect complete -- memories saved.]\n")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        print("       Get your key at https://console.anthropic.com/")
        print("       Then run:  set ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    # Check MMU server before starting
    health_str = mmu_health()
    if "UNREACHABLE" in health_str:
        print(f"WARNING: MMU server {health_str}")
        print("         Make sure Docker is running: docker compose up -d")
        print("         Tool calls will fail until the server is available.\n")
    else:
        print(f"[MMU] {health_str}")

    client     = anthropic.Anthropic(api_key=api_key)
    messages   = []

    print(f"\nMMU Claude Bridge")
    print(f"Model  : {MODEL}")
    print(f"Server : {MMU_BASE}")
    print(f"Commands: /reflect   /health   /quit")
    print("-" * 55)
    print("Loading memory context for this session...\n")

    # Bootstrap: trigger get_session_context before any user input.
    # This mirrors the LM Studio behavior where Nova is instructed to
    # call get_session_context as its very first action.
    bootstrap_messages = [{
        "role": "user",
        "content": "Session is starting. Please load your memory context now, then greet me briefly."
    }]
    init_text, messages = run_turn(client, bootstrap_messages, show_tools=True)
    if init_text.strip():
        print(f"Nova: {init_text}\n")

    # Interactive chat loop
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Interrupted]")
            break

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd == "/quit":
            confirm = input("Run end-of-session reflection before quitting? (y/n): ").strip().lower()
            if confirm == "y":
                do_reflect(client, messages)
            print("Session ended.")
            break

        elif cmd == "/reflect":
            do_reflect(client, messages)
            continue

        elif cmd == "/health":
            print(f"[MMU] {mmu_health()}\n")
            continue

        elif cmd == "/help":
            print("Commands: /reflect  /health  /quit\n")
            continue

        # Normal conversation turn
        messages.append({"role": "user", "content": user_input})
        response_text, messages = run_turn(client, messages, show_tools=True)

        if response_text.strip():
            print(f"\nNova: {response_text}\n")
        else:
            print("[No text response -- Nova may have only called tools]\n")


if __name__ == "__main__":
    main()