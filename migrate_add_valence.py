"""
migrate_add_valence.py -- Phase 6.5 Migration
==============================================
Backfills the ~VAL segment (Phase 6.5 valence) onto every Memory address
that was written before Phase 6.5.

OLD format:  CON.PRI.GRP.USE,ARC|SRC_TYPE.CHUNK.LINE
NEW format:  CON.PRI.GRP.USE,ARC~000|SRC_TYPE.CHUNK.LINE  (000 = unrated)

Also adds valence_type=0, valence_intensity=0, valence_rated_at=null as
Neo4j properties on every migrated Memory node.

ALSO updates the v2 index JSON file so the light index keys match Neo4j.

RUN ONCE:
    py -3.14 migrate_add_valence.py

Run with --dry-run to preview without writing anything.
Run with --status to just count how many memories still need migration.

SAFE TO RUN WHILE SERVER IS LIVE:
    Neo4j MERGE/SET is atomic. Each rename is a single property write.
    The server's _parse_addr() accepts both old and new formats during migration
    (the ~VAL group is optional in the regex).

    After migration, rebuild the Docker image so the server's _gen_addr() also
    starts writing new addresses:
        docker compose build mmu-server
        docker compose up -d mmu-server
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path

NEO4J_URI  = os.environ.get("NEO4J_URI",  "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
# Required, never defaulted. A script that falls back to a known password
# silently tries that password against whatever is listening.
NEO4J_PASS = os.environ.get("NEO4J_PASS")

# V2 index file -- mounted from the Docker volume onto the host. The default
# is relative to this file, so it resolves the same on any platform.
V2_INDEX_PATH = os.environ.get(
    "MMU_V2_INDEX_PATH",
    str(Path(__file__).resolve().parent / "data" / "memory_index_v2.json"),
)

OLD_ADDR_RE = re.compile(
    r"^(\d{3}\.\d{3}\.\d{3}\.\d{3},\d{3})\|(\d+\.\d{3}\.\d{3})$"
)


def connect():
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        driver.verify_connectivity()
        print(f"Connected to Neo4j at {NEO4J_URI}")
        return driver
    except Exception as e:
        print(f"ERROR: cannot connect to Neo4j -- {e}")
        sys.exit(1)


def get_all_addresses(session):
    result = session.run("MATCH (m:Memory) RETURN m.address AS addr")
    return [r["addr"] for r in result if r["addr"]]


def needs_migration(addr):
    """True when address has no ~VAL segment."""
    return addr is not None and "~" not in addr and OLD_ADDR_RE.match(addr)


def make_new_addr(old_addr):
    m = OLD_ADDR_RE.match(old_addr)
    if not m:
        return None
    return f"{m.group(1)}~000|{m.group(2)}"


def migrate_neo4j(session, old_addr, new_addr, dry_run):
    if dry_run:
        return
    session.run("""
        MATCH (m:Memory {address: $old})
        SET m.address           = $new,
            m.valence_type      = 0,
            m.valence_intensity = 0,
            m.valence_rated_at  = null
    """, old=old_addr, new=new_addr)


def migrate_v2_index(path, addr_map, dry_run):
    """
    Load the v2 index JSON, rename every key in addr_map, write back.
    addr_map: {old_addr: new_addr}
    """
    if not os.path.exists(path):
        print(f"  [v2 index] Not found at {path} -- skipping JSON update.")
        print( "  You will need to delete the file and let the server rebuild it on next start.")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  [v2 index] Could not read {path}: {e}")
        return

    # The v2 index stores entries under a 'shortcut_cache' key (or top-level)
    cache_key = "shortcut_cache" if "shortcut_cache" in data else None
    cache = data[cache_key] if cache_key else data

    renamed = 0
    for old, new in addr_map.items():
        if old in cache:
            cache[new] = cache.pop(old)
            renamed += 1

    if cache_key:
        data[cache_key] = cache

    if dry_run:
        print(f"  [v2 index] Would rename {renamed} entries in {path}")
        return

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"  [v2 index] Renamed {renamed} entries and saved {path}")


def main():
    parser = argparse.ArgumentParser(description="Phase 6.5 valence migration")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing anything")
    parser.add_argument("--status", action="store_true",
                        help="Count memories needing migration and exit")
    args = parser.parse_args()

    driver = connect()
    addr_map = {}  # old -> new

    with driver.session() as s:
        all_addrs = get_all_addresses(s)
        print(f"Total Memory nodes: {len(all_addrs)}")

        to_migrate = [a for a in all_addrs if needs_migration(a)]
        already_done = [a for a in all_addrs if a and "~" in a]

        print(f"Already migrated:   {len(already_done)}")
        print(f"Need migration:     {len(to_migrate)}")

        if args.status:
            driver.close()
            return

        if not to_migrate:
            print("Nothing to migrate. All addresses already have ~VAL segment.")
            driver.close()
            return

        if args.dry_run:
            print("\n-- DRY RUN (no writes) --")

        for old_addr in to_migrate:
            new_addr = make_new_addr(old_addr)
            if new_addr is None:
                print(f"  SKIP (unexpected format): {old_addr}")
                continue
            addr_map[old_addr] = new_addr
            print(f"  {old_addr}")
            print(f"    -> {new_addr}")
            migrate_neo4j(s, old_addr, new_addr, args.dry_run)

        if not args.dry_run:
            print(f"\nNeo4j: migrated {len(addr_map)} addresses.")

    driver.close()

    # Update v2 index file
    print(f"\nUpdating v2 index at {V2_INDEX_PATH} ...")
    migrate_v2_index(V2_INDEX_PATH, addr_map, args.dry_run)

    if args.dry_run:
        print("\n-- DRY RUN complete. Re-run without --dry-run to apply. --")
    else:
        print("\nMigration complete.")
        print("\nNext steps:")
        print("  1. docker compose build mmu-server")
        print("  2. docker compose up -d mmu-server")
        print("  3. Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health'")
        print("     Confirm all memory counts look correct.")


if __name__ == "__main__":
    main()
