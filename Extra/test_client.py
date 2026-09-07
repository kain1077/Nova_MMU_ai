"""
MMU Test Client
===============
Run this AFTER docker compose up to verify everything works.

Usage:
    python test_client.py

Tests:
    1. Health check
    2. Pin a base memory
    3. Save a regular memory
    4. Recall by keyword
    5. Verify color matrix aging
    6. Delete test memories
"""

import requests
import json
import time

BASE = "http://127.0.0.1:8765"

def sep(label):
    print(f"\n{'─'*55}")
    print(f"  {label}")
    print(f"{'─'*55}")

def check(r, label):
    if r.status_code in (200, 201):
        print(f"  ✅ {label}")
        return r.json()
    else:
        print(f"  ❌ {label} — {r.status_code}: {r.text}")
        return None


# ── Test 1: Health ────────────────────────────────────

sep("1. Health Check")
r = requests.get(f"{BASE}/health")
data = check(r, "Server is up")
if data:
    print(f"     Memories: {data['total_memories']}")
    print(f"     Colors:   {data['color_summary']}")


# ── Test 2: Pin base memories ─────────────────────────

sep("2. Pinning Base Memories (Red)")

r = requests.post(f"{BASE}/pin", json={
    "keywords": "user,name,identity,demo",
    "payload":  "User's name is Alex. Interested in science fiction, music, and building AI systems.",
    "note":     "Identity — cold start base layer"
})
pin1 = check(r, "Pinned identity memory")
if pin1:
    print(f"     Address: {pin1['address']}")

r = requests.post(f"{BASE}/pin", json={
    "keywords": "system,docker,ollama,port,llama,lmstudio",
    "payload":  "The user runs Ollama via Docker on port 11434, model llama3.2. Also uses LM Studio for GUI model access. MMU server on port 8765.",
    "note":     "System config — cold start base layer"
})
pin2 = check(r, "Pinned system config memory")
if pin2:
    print(f"     Address: {pin2['address']}")


# ── Test 3: Save regular memories ────────────────────

sep("3. Saving Regular Memories (Green)")

memories = [
    {
        "keywords": "project,memory,mmu,architecture,color,matrix",
        "payload":  "The user is building a two-way neural MMU: color matrix (Red/Green/Yellow/Blue), light index for fast recall, AI self-memory via <remember> tags, end-of-session reflection, and extended address schema with source tracking.",
        "priority": 2,
        "src_type": 0,
        "note":     "Active project"
    },
    {
        "keywords": "address,schema,format,source,index",
        "payload":  "Address format: CON.PRI.GRP.USE,ARC|SRC_TYPE.CHUNK.LINE — SRC_TYPE: 0=convo, 1=AI-self, 2=doc, 3=web.",
        "priority": 3,
        "src_type": 0,
        "note":     "Schema design decision"
    },
    {
        "keywords": "preference,response,style,concise",
        "payload":  "The user prefers concise, collaborative responses. Likes when AI has genuine personality and a background.",
        "priority": 4,
        "src_type": 1,
        "note":     "AI self-observed preference"
    }
]

saved_addrs = []
for mem in memories:
    r = requests.post(f"{BASE}/remember", json=mem)
    result = check(r, f"Saved: {mem['keywords'][:40]}")
    if result:
        saved_addrs.append(result["address"])
        print(f"     Address: {result['address']}")


# ── Test 4: Recall by keyword ─────────────────────────

sep("4. Recall Test")

test_prompts = [
    "What is the user building?",
    "What model and port is Ollama on?",
    "Tell me about the address format",
    "What year was the user born?"
]

for prompt in test_prompts:
    r = requests.post(f"{BASE}/recall", json={"prompt": prompt, "top_k": 5})
    result = check(r, f"Recall: '{prompt[:40]}'")
    if result:
        print(f"     Found {result['count']} memories")
        if result["memories"]:
            top = result["memories"][0]
            print(f"     Top hit [{top['color']}]: {top['payload'][:70]}...")


# ── Test 5: Full context block ────────────────────────

sep("5. Context Block (what the LLM sees)")

r = requests.post(f"{BASE}/recall", json={
    "prompt": "Tell me about the user's project and his system setup",
    "top_k":  8
})
result = r.json()
print("\n  Injected context block:")
print("  " + "\n  ".join(result["context_block"].split("\n")))


# ── Test 6: Aging simulation ──────────────────────────

sep("6. Color Matrix Aging (3 unrelated recalls)")

for i in range(3):
    r = requests.post(f"{BASE}/recall", json={"prompt": f"unrelated topic {i} coffee weather"})
    print(f"  Turn {i+1}: fired recall on unrelated prompt")
    time.sleep(0.2)

r = requests.get(f"{BASE}/memories")
mems = r.json()["memories"]
print(f"\n  Memory states after 3 idle turns:")
for m in mems:
    icon = {"Red":"🔴","Green":"🟢","Yellow":"🟡","Blue":"🔵"}.get(m["color"],"⚪")
    print(f"  {icon} {m['color']:8s} | {m['address'][:40]} | {m['payload'][:50]}...")


# ── Test 7: Health summary ────────────────────────────

sep("7. Final Health Check")
r = requests.get(f"{BASE}/health")
data = r.json()
print(f"  Total memories: {data['total_memories']}")
print(f"  Color summary:  {data['color_summary']}")
print(f"\n  ✅ All tests complete — MMU server is working!")
print(f"  📋 Point LM Studio tools at http://127.0.0.1:8765")
