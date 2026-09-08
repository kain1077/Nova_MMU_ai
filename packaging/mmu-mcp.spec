# PyInstaller spec for the mmu-mcp binary -- the MCP bridge, frozen.
#
# This is the piece that removes host Python from MMU's requirements. The chat
# client spawns this executable directly and speaks JSON-RPC over its stdio; no
# interpreter, no pip, no virtualenv.
#
#   pyinstaller packaging/mmu-mcp.spec

import os

REPO_ROOT = os.path.dirname(SPECPATH)  # noqa: F821 -- injected by PyInstaller

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "entry_mcp.py")],  # noqa: F821
    # mmu_mcp_server.py lives at the repo root, not in packaging/.
    pathex=[SPECPATH, REPO_ROOT],  # noqa: F821
    binaries=[],
    datas=[],
    # requests pulls these in lazily, so static analysis can miss them and the
    # binary then fails at the first HTTPS call rather than at build time.
    hiddenimports=["requests", "urllib3", "certifi", "idna", "charset_normalizer"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data", "lib2to3"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mmu-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Must stay a console binary: MCP is a stdio protocol, and a windowed build
    # has no stdin to read requests from.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
