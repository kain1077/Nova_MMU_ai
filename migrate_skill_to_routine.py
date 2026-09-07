"""
migrate_skill_to_routine.py -- Skill -> Routine rename
======================================================
Renames the crystallized-memory concept from "Skill" to "Routine" in an
existing graph, so it stops colliding with agent SKILL.md files (issue #2).

Nothing about the behaviour changes. Only names change:

    :Skill              ->  :Routine
    :SkillProposal      ->  :RoutineProposal
    [:EXTENDS_SKILL]    ->  [:EXTENDS_ROUTINE]
    sk.skill_id         ->  sk.routine_id
    p.skill_score       ->  p.routine_score
    m.pre_skill_color   ->  m.pre_routine_color

The old `skill_id` constraint and `skill_embedding` vector index are dropped;
the server recreates them under the new names at startup.

RUN ONCE, WITH THE SERVER STOPPED:
    docker compose stop mmu-server
    python migrate_skill_to_routine.py
    docker compose build mmu-server && docker compose up -d mmu-server

Unlike the valence migration this one is NOT safe to run live -- the server
queries :Skill by label, so a half-relabelled graph is a graph where routines
are invisible.

Run with --dry-run to preview without writing anything.
Run with --status to just count what would be migrated.
"""

import os
import sys
import argparse

NEO4J_URI  = os.environ.get("NEO4J_URI",  "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS")

# (label, cypher) -- ordered. Relabel first, then rewrite properties on the
# new label, so a re-run after a partial failure is still correct.
STEPS = [
    ("Skill nodes -> Routine",
     "MATCH (n:Skill) SET n:Routine REMOVE n:Skill RETURN count(n) AS n"),
    ("SkillProposal nodes -> RoutineProposal",
     "MATCH (n:SkillProposal) SET n:RoutineProposal REMOVE n:SkillProposal RETURN count(n) AS n"),
    ("EXTENDS_SKILL -> EXTENDS_ROUTINE",
     "MATCH (a)-[r:EXTENDS_SKILL]->(b) "
     "MERGE (a)-[:EXTENDS_ROUTINE]->(b) DELETE r RETURN count(r) AS n"),
    ("routine_id property",
     "MATCH (n:Routine) WHERE n.skill_id IS NOT NULL "
     "SET n.routine_id = n.skill_id REMOVE n.skill_id RETURN count(n) AS n"),
    ("proposal routine_id property",
     "MATCH (p:RoutineProposal) WHERE p.skill_id IS NOT NULL "
     "SET p.routine_id = p.skill_id REMOVE p.skill_id RETURN count(p) AS n"),
    ("proposal routine_score property",
     "MATCH (p:RoutineProposal) WHERE p.skill_score IS NOT NULL "
     "SET p.routine_score = p.skill_score REMOVE p.skill_score RETURN count(p) AS n"),
    ("member pre_routine_color property",
     "MATCH (m) WHERE m.pre_skill_color IS NOT NULL "
     "SET m.pre_routine_color = m.pre_skill_color REMOVE m.pre_skill_color "
     "RETURN count(m) AS n"),
]

# Counts for --status and --dry-run, in the same order as STEPS.
COUNTS = [
    ("Skill nodes -> Routine",              "MATCH (n:Skill) RETURN count(n) AS n"),
    ("SkillProposal nodes -> RoutineProposal", "MATCH (n:SkillProposal) RETURN count(n) AS n"),
    ("EXTENDS_SKILL -> EXTENDS_ROUTINE",    "MATCH ()-[r:EXTENDS_SKILL]->() RETURN count(r) AS n"),
    ("routine_id property",                 "MATCH (n) WHERE n.skill_id IS NOT NULL RETURN count(n) AS n"),
    ("proposal routine_id property",        "MATCH (p) WHERE p.skill_id IS NOT NULL RETURN count(p) AS n"),
    ("proposal routine_score property",     "MATCH (p) WHERE p.skill_score IS NOT NULL RETURN count(p) AS n"),
    ("member pre_routine_color property",   "MATCH (m) WHERE m.pre_skill_color IS NOT NULL RETURN count(m) AS n"),
]

DROPS = [
    "DROP CONSTRAINT skill_id IF EXISTS",
    "DROP INDEX skill_embedding IF EXISTS",
]


def connect():
    if not NEO4J_PASS:
        sys.exit("NEO4J_PASS is not set. Export it, or source your .env, and retry.")
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        driver.verify_connectivity()
        print(f"Connected to Neo4j at {NEO4J_URI}")
        return driver
    except Exception as e:
        sys.exit(f"Could not connect to Neo4j at {NEO4J_URI}: {e}")


def report(session):
    total = 0
    for label, cypher in COUNTS:
        n = session.run(cypher).single()["n"]
        total += n
        print(f"  {n:>6}  {label}")
    return total


def main():
    ap = argparse.ArgumentParser(description="Rename Skill -> Routine in an existing graph.")
    ap.add_argument("--dry-run", action="store_true", help="preview without writing")
    ap.add_argument("--status", action="store_true", help="count what needs migrating and exit")
    args = ap.parse_args()

    driver = connect()
    with driver.session() as s:
        print("\nPending:")
        total = report(s)

        if args.status:
            driver.close()
            return

        if total == 0:
            print("\nNothing to migrate. This graph is already on Routine names.")
            driver.close()
            return

        if args.dry_run:
            print("\n-- DRY RUN complete. Re-run without --dry-run to apply. --")
            driver.close()
            return

        print()
        for label, cypher in STEPS:
            n = s.run(cypher).single()["n"]
            print(f"  {n:>6}  {label}")

        for cypher in DROPS:
            s.run(cypher)
        print(f"\nDropped the old skill_id constraint and skill_embedding index; "
              f"the server recreates them as routine_* at startup.")

    driver.close()
    print("\nMigration complete.")
    print("\nNext steps:")
    print("  1. docker compose build mmu-server")
    print("  2. docker compose up -d mmu-server")
    print("  3. curl http://127.0.0.1:8765/health")
    print("     Then GET /routines and confirm the count matches what you had.")


if __name__ == "__main__":
    main()
