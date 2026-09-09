# Packaging

This directory builds two binaries per platform:

| Binary | What it is | Who runs it |
|---|---|---|
| `mmu-setup` | Installer, launcher and doctor | The user, once to install, then whenever something looks wrong |
| `mmu-mcp` | The MCP bridge, frozen | The chat client, automatically, every session |

Both are single files with no dependencies. Neither needs Python installed.

---

## What the executable can and cannot be

MMU's server is Neo4j plus a FastAPI container. Neo4j is a JVM database with its
own storage format and its own lifecycle; it is not something you fold into an
`.exe`. So `mmu-setup` is not "MMU in a file" — it is **everything around MMU in
a file**. Docker remains a genuine prerequisite.

What that buys is still most of the install:

- Docker is checked, and a missing daemon is distinguished from a missing
  install, with the right fix for the platform you are on.
- `.env` is generated, with a random Neo4j password, in place inside the
  template so every explanatory comment survives.
- **The embedding endpoint is found and measured.** The README asks users to
  `curl` their endpoint and count numbers in the response to discover
  `MMU_EMBEDDING_DIM`. That number is baked into the Neo4j vector index at
  creation time, and getting it wrong produces a graph that accepts saves and
  silently never embeds them. `mmu-setup` probes LM Studio, Ollama, llama.cpp and
  vLLM, embeds a test string, counts the vector itself, and rewrites
  `127.0.0.1` to `host.docker.internal` so the container can actually reach it.
- `docker compose up --build` runs, health is polled, and the startup self-check
  is parsed — so a dimension mismatch is reported as a failure rather than left
  in a log for the user to grep.
- The MCP config is **merged** into Claude Desktop and LM Studio, with a
  timestamped backup, leaving every other server the user had wired up intact.

`mmu-mcp` is the smaller and more clear-cut win: freezing the bridge is what
removes host Python from MMU's requirements entirely.

---

## Building

```bash
pip install -r packaging/requirements-build.txt
python packaging/build.py
```

Artifacts land in `dist/`, named for the platform they were built on
(`mmu-setup-windows-x64.exe`, `mmu-mcp-macos-arm64`, and so on).

```bash
python packaging/build.py --only mcp    # just the bridge
python packaging/build.py --no-rename   # leave them as mmu-setup / mmu-mcp
```

PyInstaller cannot cross-compile. A Windows `.exe` must be built on Windows and
a macOS binary on macOS, which is why `.github/workflows/release.yml`
fans out across four runners (Windows, Linux, Intel Mac, Apple silicon Mac) rather than
building everything in one job. `build.py` is what each runner calls, so a local
build reproduces a release build exactly.

Linux binaries are built on the oldest supported runner image on purpose:
glibc is forward-compatible but not backward-compatible, so a binary built
against a new glibc will not start on an older distribution.

---

## Layout

```
packaging/
  build.py              driver: builds, renames, chmods
  mmu-setup.spec        installer, with the project files bundled in
  mmu-mcp.spec          bridge, with requests bundled in
  entry_setup.py        PyInstaller entry point
  entry_mcp.py          PyInstaller entry point
  mmu_cli/
    cli.py              verbs, and the setup wizard
    dockerctl.py        Docker detection and compose invocation
    embedding.py        endpoint probe and dimension measurement
    envfile.py          .env generation that preserves comments
    mcpconfig.py        merge into client configs, never replace
    payload.py          find an existing checkout, or lay one down
    server.py           health polling and self-check parsing
    state.py            remember where MMU was installed
    ui.py               prompts and colour, no dependencies
```

`mmu_cli` is standard library only, deliberately. It is the code that runs
*before* anything is installed, so it cannot depend on anything being installed.

---

## The bundled payload

`mmu-setup.spec` bundles `docker-compose.yml`, the `Dockerfile`, the four server
modules the Dockerfile `COPY`s, `.env.example` and `mmu_mcp_server.py` into the
binary. That is what lets a user who downloaded exactly one file get a working
MMU: `payload.materialize()` writes them into the install directory and compose
builds from there.

Run inside a git checkout, the installer uses the checkout instead — the user's
edits, their `.env` and their existing Docker volumes all live there, and
quietly installing a second copy elsewhere would strand them.

Two lists have to stay in step with the `Dockerfile`: `PAYLOAD_FILES` in
`payload.py` and `PAYLOAD` in `mmu-setup.spec`. `tests/test_packaging.py` fails
if either drifts, because the alternative is discovering it during someone's
image build.

---

## Frozen-mode differences in the bridge

`mmu_mcp_server.py` runs both as a script and frozen, and two things had to
change for the frozen case:

- **`.env` lookup.** Unfrozen, `.env` sits beside `__file__`. Frozen, `__file__`
  points inside PyInstaller's temporary extraction directory, which is created
  fresh at launch and deleted at exit. A frozen build looks beside its own
  executable, then in the platform install directory.
- **The staleness check.** It compares the mtime of its own source against what
  is on disk, to catch a client running code that has since been edited. Frozen,
  the extracted source is always seconds old, so it watches the executable
  instead — replacing the binary while a client holds it open produces exactly
  the same silent staleness.

---

## Signing

The binaries are **not code-signed**. There is no certificate, and pretending
otherwise would be worse than saying so.

In practice:

- **Windows** — SmartScreen shows "Windows protected your PC" on first run.
  *More info* → *Run anyway*.
- **macOS** — Gatekeeper refuses an unsigned, un-notarised download outright.
  Either right-click → *Open* → *Open*, or strip the quarantine attribute:

  ```bash
  xattr -d com.apple.quarantine ./mmu-setup-macos-arm64
  chmod +x ./mmu-setup-macos-arm64
  ```

- **Linux** — nothing objects; just `chmod +x`.

Releases publish `SHA256SUMS.txt` so downloads can at least be verified against
what CI produced. That is not a substitute for signing, and is not claimed as
one. Anyone who would rather not run an unsigned binary can still install from
source; the manual path is unchanged and is not going away.
