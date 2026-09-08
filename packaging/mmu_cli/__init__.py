"""
mmu_cli -- the packaged installer, launcher and doctor for MMU.

Frozen by PyInstaller into a single binary per platform (mmu-setup.exe,
mmu-setup-macos, mmu-setup-linux). Stdlib only, on purpose: the whole point of
this package is to be the thing that runs before the user has installed
anything, so it cannot depend on anything being installed.
"""

__version__ = "1.0.0"
