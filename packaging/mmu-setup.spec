# PyInstaller spec for the mmu-setup binary.
#
# Bundles the project files the installer needs to lay down, so that a user who
# downloads exactly one file still gets a working MMU. Without this, the binary
# would only work inside a git checkout, which defeats the point.
#
#   pyinstaller packaging/mmu-setup.spec

import os

REPO_ROOT = os.path.dirname(SPECPATH)  # noqa: F821 -- injected by PyInstaller

# Kept in step with PAYLOAD_FILES in mmu_cli/payload.py. Anything the Dockerfile
# COPYs has to be here, or the image build fails at `docker compose up`.
PAYLOAD = [
    "docker-compose.yml",
    "Dockerfile",
    "requirements.txt",
    "requirements-host.txt",
    ".env.example",
    "mmu_server.py",
    "neo4j_layer.py",
    "light_index_v2.py",
    "ingest.py",
    "mmu_mcp_server.py",
]

datas = [
    (os.path.join(REPO_ROOT, name), "payload")
    for name in PAYLOAD
    if os.path.exists(os.path.join(REPO_ROOT, name))
]

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "entry_setup.py")],  # noqa: F821
    pathex=[SPECPATH],  # noqa: F821
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # The installer is stdlib-only by design. Excluding the heavy optional
    # stdlib corners keeps the binary near 10 MB rather than 40.
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
    name="mmu-setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
