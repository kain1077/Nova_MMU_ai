"""PyInstaller entry point for the mmu-setup binary."""

import multiprocessing
import sys

from mmu_cli.cli import main

if __name__ == "__main__":
    # Frozen binaries that re-exec themselves must be told they are frozen, or a
    # child process restarts the installer instead of doing its job.
    multiprocessing.freeze_support()
    sys.exit(main())
