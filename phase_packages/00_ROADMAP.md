# MMU Remaining Roadmap -- Phase Packages for Claude Code

This folder has one self-contained design doc per remaining phase (or phase-group),
written so you can hand any one of them to Claude Code running on the development machine and it has
everything it needs: exact file/line anchors in the current codebase, schema and code
changes, open design decisions flagged explicitly rather than silently assumed, and a
deploy-and-validate sequence Claude Code runs itself with real terminal and Docker
access.

Files in this folder:

1. `phase9_semantic_embeddings.md` -- semantic recall layer **(SHIPPED, see
   `phase9_deploy_report.md`)**
2. `phase11_knowledge_seeding.md` -- document ingestion and chunking **(SHIPPED, see
   `phase11_deploy_report.md`; voice transcription deferred)**
3. `phase10_proactive_memory.md` -- pattern detection, temporal tags, anticipation.
   **Also now carries the aging redesign** (count + time-since-touch, Yellow pre-hold)
4. `phase12_13_procedural_memory.md` -- skill crystallization + skill tree
3.5. `phase10_deploy_report.md`, `phase12_13_deploy_report.md` -- shipped
5. `phase_final_shareable_release.md` -- **FINAL STAGE, SHIPPED.** Clean install, empty
   memory graph, validated by standing up a second instance from scratch. See
   `phase_final_deploy_report.md`.

**All roadmap phases are complete as of 2026-08-30.** Remaining known items are listed
in section 9 of `phase_final_deploy_report.md`.

Not in the sequence:
- `maybe_retrieval_control.md` -- semantic zoom, emotion sieve, working memory. Drafted
  2026-08-30, deferred the same day; revisit after everything above, if at all.

## Recommended order and why

**9, then 11, then 10, then 12+13 combined, then the final shareable-release stage.**

> **Update 2026-08-30.** Phases 9 and 11 are shipped. Two changes to what follows:
>
> - **Aging redesign folded into Phase 10.** Archiving currently keys off recall *count*
>   alone, so twenty recalls archive a memory whether they span three months or twenty
>   minutes. Validation runs archived the conversational graph twice and both needed a
>   manual restore. The user's original design was count **and** time-since-touch with a
>   holding state before archive; that goes into Phase 10, which is already adding the
>   temporal machinery it needs.
> - **A final shareable-release stage added.** Making the system something other people
>   can run is deliberately last: it hardens whatever the system has become, so doing it
>   before 10 and 12+13 means doing it twice.
>
> Retrieval control (scoped recall, emotion sieve, working-memory buffer) was drafted as
> a "Phase 11.5" ahead of Phase 10 and then deferred out of the sequence, on the
> reasoning that reordering the roadmap mid-stream in response to a feature request is
> how sequencing drifts. Also settled: proactive surfacing stays **pull-based via tool
> call**, not server push -- tool calls are the only primitive supported universally
> across local and cloud models, and push would fragment compatibility for a system
> intended to be shared.

Phase 9 (embeddings) goes first because every phase after it benefits from better
recall. Ingested documents (11) are only useful if they can actually be found again,
and Phase 9's semantic fallback is what makes a paraphrased query find them without
sharing exact keywords. Pattern detection for proactive memory (10) is far more
convincing against a graph that already has both a semantic signal and a good volume
of real content from ingestion. Crystallization (12+13) benefits from a mature,
well-populated graph with real CO_RECALLED weight worth compressing, so it goes last
of the four, and it's also the one phase that changes the memory model itself (Memory
nodes get demoted into a Skill's root system), which is a bigger structural step than
the other three and deserves the most mature graph to test against.

This is my honest recommendation, not the only reasonable one. Phase 9 and Phase 11
don't have a hard dependency on each other, only a "makes more sense in this order"
argument, so if a real document the user wants ingested is sitting there ready to go, it's
fine to build 11 first and 9 second, nothing breaks. Phase 10 genuinely benefits from
9 existing first (temporal/cluster anticipation is much thinner without a semantic
layer already in place), and Phase 12+13 genuinely benefits from a denser graph, so
those two are less swappable.

## My honest take on combining phases, for the record

Combining is a good idea for cutting calendar time, and I don't think it trades away
the project's actual safety margin, as long as "combined" means *the deploy cadence
speeds up*, not *the validation discipline gets skipped*. Three real bugs surfaced
this project so far specifically because something was checked in isolation before
being trusted: the Docker build-cache issue that left `/rate` silently 404ing for a
day, the malformed-address bug caught from real LM Studio logs, and a boolean/string
mismatch in Phase 8's own session-close logic that I found by re-reading the code
before shipping it, not by it failing loudly. None of those would have been easier to
find if two phases' changes had landed at once instead of one.

So: combine 12 and 13 because they share one concept (the Skill node) and building
the tree without a crystallized skill to hang it on doesn't even make sense --  that's
a real, structural reason to combine, not just a scheduling one. Each individual
package above still asks Claude Code to validate its own core piece before moving to
the next thing inside that same package (see each doc's deploy sequence). That's the
balance I'd defend if asked: fewer separate week-long phases, same discipline of "prove
the last thing works before trusting it under the next thing."

## Workflow for handing these to Claude Code

Each package doc opens with a ready-to-paste block titled "Copy this to Claude Code to
start." The intended flow:

1. The user pastes that block into Claude Code on the development machine (it references the doc's full
   path under `C:\MMU\phase_packages\`, so Claude Code reads the rest of the context
   itself).
2. Claude Code implements, deploys (real `docker compose`/terminal access, which this
   Cowork session does not have), and validates against the running system, following
   that doc's deploy sequence and acceptance checklist.
3. Claude Code reports back to the user with real output from its validation steps, not
   just a description of what it built -- each doc says this explicitly, worth holding
   it to that standard.
4. If the user wants a second pair of eyes on what changed before moving to the next
   package, he can paste the diff or Claude Code's report back into this Cowork
   session and I'll review it the same way I reviewed Phase 8's code before shipping
   it -- that review step is exactly where the boolean/string bug and the missing
   `/rate` activity-path bug got caught, so it's worth keeping in the loop even at a
   faster pace, not skipping it because Claude Code already "tested" its own work.
5. Move to the next package in order once the current one's acceptance checklist is
   genuinely satisfied, not just attempted.

## Phase 6.6 follow-up -- separate, and not part of this batch

The handoff doc also lists an unscheduled Phase 6.6 follow-up: designing the first
real recall/archiving boost curve from observed `emotion_breakdown` and
`suggested_neg_weight` data. This is deliberately not included as a fifth package
here, because it's gated on accumulated rating volume, not on writing more code --
forcing it into this batch would mean designing a curve before there's enough real
data to design it from, which is exactly the mistake Phase 6.5 and 6.6 were both
careful to avoid by shipping inert first. Revisit it whenever the user's own bar for "enough
ratings across enough sessions" is met, independent of where the four code phases
above stand.

## What "done" looks like

Each package's own acceptance checklist at the bottom of its doc is the real
definition of done for that phase, not this summary. If a checklist item can't
honestly be checked off, the phase isn't done yet, even if the code compiles and the
container starts.
