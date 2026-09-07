#!/usr/bin/env python3
"""
mmu_review.py -- the human side of crystallization.

Phase 12 made confirming a routine a human decision and Phase 13.1 gave that
decision a queue. This is the terminal for it. Nothing here is reachable by a
model: it is a local script you run, and every write asks first.

    python mmu_review.py                 list pending proposals
    python mmu_review.py --all           include rejected and crystallized
    python mmu_review.py 2               inspect proposal 2 in full
    python mmu_review.py 2 --crystallize walk through confirming it
    python mmu_review.py 2 --reject      decline it, permanently
    python mmu_review.py --sweep         look for new candidates now

Confirming is deliberately a conversation, not a flag. Crystallizing compresses
memories into a Routine and DEMOTES them to Blue, which changes how memory is
structured rather than adding to it -- so the script shows exactly what will be
demoted and makes you type the word before it writes anything.

Proposals are addressed by their position in the listing. The underlying
addresses are resolved server-side at write time, because MMU addresses encode
the use counter and are rewritten whenever a memory is recalled.
"""

import argparse
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request

# Memory payloads contain em-dashes and other non-cp1252 characters, and the
# Windows console default mangles them into replacement marks. Previews are the
# whole point of a review screen, so make the encoding explicit.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

BASE = os.environ.get("MMU_BASE", "http://127.0.0.1:8765")
KEY = os.environ.get("MMU_API_KEY", "").strip()

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    # Old consoles render the escapes literally, which is worse than plain text.
    BOLD = DIM = RESET = ""


def call(method, path, payload=None):
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if KEY:
        req.add_header("X-MMU-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"detail": body}
    except urllib.error.URLError as e:
        print(f"Cannot reach MMU at {BASE}: {e.reason}")
        print("Is the server running?  docker compose up -d")
        sys.exit(1)


def wrap(text, indent="      "):
    return "\n".join(
        textwrap.fill(line, width=96, initial_indent=indent,
                      subsequent_indent=indent + "  ")
        for line in (text or "").splitlines() or [""]
    )


def fetch(status="pending"):
    q = f"?status={status}&limit=50" if status else "?status=&limit=50"
    st, d = call("GET", f"/routine_proposals{q}")
    if st != 200:
        print(f"Error {st}: {d.get('detail')}")
        sys.exit(1)
    return d.get("proposals", [])


def show(p, index, full=False):
    sem = p.get("semantic_coherence")
    sem_s = f"{sem:.2f}" if isinstance(sem, (int, float)) else "n/a"
    head = (f"{BOLD}[{index}]{RESET} score {p['routine_score']:.2f}  "
            f"{DIM}(co-recall {p.get('avg_weight')} | domain {p.get('grp_coherence')} | "
            f"meaning {sem_s}){RESET}")
    if p.get("status") != "pending":
        head += f"  <{p['status']}>"
    print(head)

    mix = ", ".join(f"{v}x {k}" for k, v in sorted((p.get("src_mix") or {}).items()))
    print(f"      {DIM}{mix}  |  GRPs {p.get('grps')}{RESET}")
    if p.get("members_missing"):
        print(f"      !! {p['members_missing']} source memory/memories no longer exist")

    for addr, prev, color in zip(p.get("members", []), p.get("previews", []),
                                 p.get("colors", []) or [""] * 9):
        print(f"      {DIM}{color:<6} {addr}{RESET}")
        print(wrap(prev, "        "))
    if full:
        print(f"      {DIM}proposal_id: {p['proposal_id']}{RESET}")
        if p.get("review_note"):
            print(f"      {DIM}note: {p['review_note']}{RESET}")
    print()


def cmd_list(status):
    props = fetch(status)
    if not props:
        print("Nothing queued." if status else "No proposals at all.")
        print(f"{DIM}Try:  python mmu_review.py --sweep{RESET}")
        return
    label = status or "all"
    print(f"\n{BOLD}{len(props)} {label} proposal(s){RESET}\n")
    for i, p in enumerate(props, 1):
        show(p, i)
    print(f"{DIM}Inspect:  python mmu_review.py <n>{RESET}")
    print(f"{DIM}Confirm:  python mmu_review.py <n> --crystallize{RESET}")


def pick(n, status="pending"):
    props = fetch(status)
    if not 1 <= n <= len(props):
        print(f"No proposal {n}. There are {len(props)} {status or 'total'}.")
        sys.exit(1)
    return props[n - 1]


def cmd_crystallize(n):
    p = pick(n)
    print()
    show(p, n, full=True)

    if p.get("members_missing"):
        print("Refusing: some source memories no longer exist, so this proposal "
              "cannot be crystallized as queued.")
        sys.exit(1)

    print(f"{BOLD}This will:{RESET}")
    print(f"  - create one Routine from these {len(p['members'])} memories")
    print(f"  - DEMOTE all {len(p['members'])} of them to Blue")
    print(f"  {DIM}Blue memories stay as the routine's root system and are never "
          f"deleted, but the recall gate treats them as inactive.{RESET}")
    print()
    print("Two things only you can write. The trigger is when this routine should "
          "fire; the procedure is what to actually do.")
    print()

    try:
        trigger = input("  trigger  > ").strip()
        if not trigger:
            print("Empty trigger. Nothing written.")
            return
        procedure = input("  procedure> ").strip()
        if not procedure:
            print("Empty procedure. Nothing written.")
            return
        raw = input("  confidence 0-1 [0.7] > ").strip()
        confidence = float(raw) if raw else 0.7

        print()
        print(f"{BOLD}Confirm{RESET} — type CRYSTALLIZE to write this, anything else to abort.")
        if input("  > ").strip() != "CRYSTALLIZE":
            print("Aborted. Nothing written.")
            return
    except (KeyboardInterrupt, EOFError):
        print("\nAborted. Nothing written.")
        return

    st, d = call("POST", f"/routine_proposals/{p['proposal_id']}/crystallize", {
        "member_addresses": [],          # resolved server-side; see the endpoint
        "trigger": trigger,
        "procedure": procedure,
        "confidence": confidence,
        "confirmed": True,
    })
    if st != 200:
        print(f"\nFailed ({st}): {d.get('detail')}")
        sys.exit(1)
    print(f"\n{BOLD}Crystallized.{RESET}  routine_id {d['routine_id']}")
    print(f"{DIM}{len(d['members'])} memories demoted to Blue. "
          f"See them with: curl {BASE}/routines{RESET}")


def cmd_reject(n, note):
    p = pick(n)
    print()
    show(p, n, full=True)
    print("Rejecting is permanent — later sweeps will not re-offer this cluster.")
    try:
        if input("  type REJECT to confirm > ").strip() != "REJECT":
            print("Aborted. Nothing written.")
            return
    except (KeyboardInterrupt, EOFError):
        print("\nAborted. Nothing written.")
        return

    q = f"?note={urllib.request.quote(note)}" if note else ""
    st, d = call("POST", f"/routine_proposals/{p['proposal_id']}/reject{q}")
    if st != 200:
        print(f"Failed ({st}): {d.get('detail')}")
        sys.exit(1)
    print("Rejected.")


def cmd_sweep():
    st, d = call("POST", "/routine_proposals/sweep")
    if st != 200:
        print(f"Failed ({st}): {d.get('detail')}")
        sys.exit(1)
    print(f"Swept: {d['created']} new, {d['refreshed']} refreshed, "
          f"{d.get('deferred', 0)} deferred, {d['pending']} pending.")
    if d.get("skipped_rejected"):
        print(f"{DIM}{d['skipped_rejected']} previously rejected, left alone.{RESET}")


def main():
    ap = argparse.ArgumentParser(
        description="Review and confirm MMU routine crystallization proposals.")
    ap.add_argument("n", nargs="?", type=int, help="proposal number from the listing")
    ap.add_argument("--crystallize", action="store_true", help="confirm proposal n")
    ap.add_argument("--reject", action="store_true", help="decline proposal n")
    ap.add_argument("--note", default="", help="reason, stored with a rejection")
    ap.add_argument("--sweep", action="store_true", help="look for new candidates now")
    ap.add_argument("--all", action="store_true", help="include non-pending proposals")
    args = ap.parse_args()

    if args.sweep:
        cmd_sweep()
    elif args.n and args.crystallize:
        cmd_crystallize(args.n)
    elif args.n and args.reject:
        cmd_reject(args.n, args.note)
    elif args.n:
        show(pick(args.n), args.n, full=True)
    else:
        cmd_list(None if args.all else "pending")


if __name__ == "__main__":
    main()
