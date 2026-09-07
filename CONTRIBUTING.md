# Contributing to MMU

MMU has had exactly one author so far, which means every design decision in here has been
reviewed by exactly one person. Outside eyes are the most useful thing this project can
receive right now — including the kind that says a choice was wrong.

## What's most wanted

Roughly in order:

1. **Platform reports.** Development has been on Windows. The server is containerized and
   the host scripts are plain Python, so macOS and Linux *should* be clean — but "should
   be" isn't "is". If you run it on either, an issue saying so either way is genuinely
   useful.
2. **Retrieval-quality benchmarking.** MMU has latency numbers and no quality numbers. A
   LongMemEval or LoCoMo harness is the single highest-value contribution available, and
   it's the one thing that would let MMU be compared to anything else honestly. See
   [Benchmarks](README.md#benchmarks).
3. **Bug reports against the parts marked unproven.** [Known
   Limitations](README.md#known-limitations) lists what hasn't been worn in by real use —
   skill crystallization's confirm-and-write path especially. Anything in that list that
   breaks for you is expected to break; a report still helps.
4. **Docs that fix a place you got stuck.** If something in the README cost you an hour,
   that's a bug in the README.

## Before you open a PR

There's no CLA and no style checklist. Two practical asks:

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

The pure tests need nothing running and should pass before and after your change. The live
tests skip themselves unless a server is listening — if you run them, point
`MMU_TEST_BASE` at a throwaway instance, never at a graph you care about. [Running a
second instance](README.md#running-a-second-instance) covers standing one up.

Second: say what you tried and what happened. A PR description that includes the case that
was broken is worth more than one that describes the patch, because the patch is already
in the diff.

## Comment style

The existing code explains *why*, not *what* — usually with the specific incident that
caused the line to exist ("this happened twice during Phase 9 validation, which is why the
guard is there"). If you're changing something subtle, that's the register to aim for.
Matching it isn't a requirement for a fix to be accepted.

## Scope

MMU is deliberately single-user, local-first, and does not browse. Those are choices, not
gaps — see [Privacy](README.md#privacy) and [Security](README.md#security). A PR that
adds multi-tenancy or outbound network calls to the server is likely to be declined on
scope rather than quality, so it's worth opening an issue first if you're headed that way.

## Security

If you find something with security impact, please open a private advisory through GitHub's
"Report a vulnerability" rather than a public issue.

## License

MMU is AGPL-3.0. Contributions are accepted under the same license.
