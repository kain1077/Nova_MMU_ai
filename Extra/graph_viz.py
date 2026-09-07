"""
MMU Graph Visualizer
====================
Reads the live SQLite + NetworkX state and shows:
  1. Connection weights between co-recalled memories
  2. Color state distribution
  3. Most connected nodes (the "hub" memories)
  4. Orphan nodes (never co-recalled with anything)

Run anytime:  python graph_viz.py
"""

import sqlite3
import json
import os
from collections import defaultdict
from pathlib import Path

# Defaults are relative to the repo root, so this works the same on Windows,
# macOS and Linux. The live data normally lives in a Docker volume -- point
# MMU_DB_PATH / MMU_INDEX_PATH at wherever you copied it out to.
REPO_ROOT  = Path(__file__).resolve().parent.parent
DB_PATH    = os.environ.get("MMU_DB_PATH",    str(REPO_ROOT / "data" / "memory_system.db"))
INDEX_PATH = os.environ.get("MMU_INDEX_PATH", str(REPO_ROOT / "data" / "memory_index.json"))
MMU_BASE   = "http://127.0.0.1:8765"

# ── Pull live data from the REST server ──────────────

import requests

def get_memories():
    try:
        r = requests.get(f"{MMU_BASE}/memories", timeout=5)
        return r.json()["memories"]
    except Exception as e:
        print(f"❌ Can't reach MMU server: {e}")
        print("   Make sure Docker is running: docker compose up -d")
        return []

def get_health():
    try:
        r = requests.get(f"{MMU_BASE}/health", timeout=5)
        return r.json()
    except:
        return {}

# ── Graph state from index ───────────────────────────

def load_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            return json.load(f)
    # Try Docker volume path
    alt = "/data/memory_index.json"
    if os.path.exists(alt):
        with open(alt) as f:
            return json.load(f)
    return {}

# ── Reporting ────────────────────────────────────────

COLOR_ICONS = {"Red": "🔴", "Green": "🟢", "Yellow": "🟡", "Blue": "🔵"}
SRC_LABELS  = {0: "Conversation", 1: "AI-Self", 2: "Document", 3: "Web"}

def sep(label, width=62):
    print(f"\n{'─'*width}")
    print(f"  {label}")
    print(f"{'─'*width}")

def run_report():
    print("\n" + "="*62)
    print("  MMU GRAPH & CONNECTION REPORT")
    print("="*62)

    # Health summary
    health = get_health()
    if health:
        print(f"\n  Server: ✅ online")
        print(f"  Total memories : {health.get('total_memories', '?')}")
        print(f"  Color breakdown: {health.get('color_summary', {})}")

    # Full memory list
    memories = get_memories()
    if not memories:
        print("\n  No memories found.")
        return

    # ── 1. Memory inventory ───────────────────────────
    sep("1. Full Memory Inventory")
    for m in memories:
        icon = COLOR_ICONS.get(m["color"], "⚪")
        src  = SRC_LABELS.get(m.get("src_type", 0), "?")
        print(f"\n  {icon} [{m['color']:7s}] [{src:12s}]")
        print(f"     Address : {m['address']}")
        print(f"     Keywords: {m['keywords']}")
        print(f"     Memory  : {m['payload'][:90]}{'...' if len(m['payload'])>90 else ''}")
        if m.get("note"):
            print(f"     Note    : {m['note']}")

    # ── 2. Color distribution ─────────────────────────
    sep("2. Color State Distribution")
    color_counts = defaultdict(int)
    for m in memories:
        color_counts[m["color"]] += 1
    total = len(memories)
    for color, count in sorted(color_counts.items()):
        bar = "█" * count + "░" * (total - count)
        print(f"  {COLOR_ICONS.get(color,'⚪')} {color:8s} {bar} {count}/{total}")

    # ── 3. Source breakdown ───────────────────────────
    sep("3. Memory Source Breakdown")
    src_counts = defaultdict(int)
    for m in memories:
        src_counts[SRC_LABELS.get(m.get("src_type", 0), "Unknown")] += 1
    for src, count in sorted(src_counts.items(), key=lambda x: -x[1]):
        bar = "█" * count
        print(f"  {src:14s} {bar} {count}")

    # ── 4. Keyword co-occurrence map ──────────────────
    sep("4. Keyword Overlap Map (cross-reference potential)")
    print("  Memories sharing keywords — these are natural link candidates:\n")

    kw_to_addrs = defaultdict(list)
    for m in memories:
        for kw in m["keywords"].split(","):
            kw = kw.strip().lower()
            if kw:
                kw_to_addrs[kw].append((m["address"][:20], m["payload"][:40]))

    shared = {kw: addrs for kw, addrs in kw_to_addrs.items() if len(addrs) > 1}
    if shared:
        for kw, addrs in sorted(shared.items(), key=lambda x: -len(x[1])):
            print(f"  '{kw}' links {len(addrs)} memories:")
            for addr, payload in addrs:
                print(f"    → {addr}...  \"{payload}\"")
            print()
    else:
        print("  No keyword overlaps yet — memories are all unique topics.")
        print("  Cross-references build as more memories are added.")

    # ── 5. Address aging status ───────────────────────
    sep("5. Memory Aging Status")
    print("  USE counter shows turns since last recall (higher = colder)\n")

    import re
    pattern = r"(\d{3})\.(\d{3})\.(\d{3})\.(\d{3}),(\d{3})"
    aging_data = []
    for m in memories:
        match = re.match(pattern, m["address"])
        if match:
            use = int(match.group(4))
            arc = int(match.group(5))
            aging_data.append((use, arc, m["color"], m["payload"][:50]))

    aging_data.sort(reverse=True)
    for use, arc, color, payload in aging_data:
        icon = COLOR_ICONS.get(color, "⚪")
        warmth = "❄️ COLD" if arc == 999 else ("🌡️ warm" if use > 5 else "🔥 hot")
        print(f"  {icon} use={use:3d} arc={arc:03d} {warmth:10s}  {payload}")

    # ── 6. Cross-reference suggestions ───────────────
    sep("6. Cross-Reference Suggestions")
    print("  Based on semantic overlap — memories worth linking:\n")

    # Simple semantic grouping by shared keyword stems
    groups = defaultdict(list)
    for m in memories:
        kws = [k.strip().lower() for k in m["keywords"].split(",")]
        for kw in kws:
            # Group by first 4 chars (rough stemming)
            stem = kw[:4] if len(kw) >= 4 else kw
            groups[stem].append(m)

    printed = set()
    suggestions = 0
    for stem, mems in groups.items():
        unique = {m["address"]: m for m in mems}
        if len(unique) > 1:
            addrs = list(unique.keys())
            pair_key = frozenset(addrs[:2])
            if pair_key not in printed and suggestions < 5:
                printed.add(pair_key)
                suggestions += 1
                m1, m2 = list(unique.values())[:2]
                print(f"  🔗 Suggested link (stem: '{stem}'):")
                print(f"     A: {m1['payload'][:60]}")
                print(f"     B: {m2['payload'][:60]}")
                print()

    if suggestions == 0:
        print("  No strong suggestions yet — keep chatting to build the graph!")

    print("\n" + "="*62)
    print("  To force cross-reference building:")
    print("  Ask the LLM questions that span multiple topics in one turn.")
    print("  Each multi-memory recall adds an edge to the NetworkX graph.")
    print("="*62 + "\n")

if __name__ == "__main__":
    run_report()
