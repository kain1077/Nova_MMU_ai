"""
PyInstaller entry point for the mmu-mcp binary.

Freezing the bridge is what removes host Python from MMU's requirements: the
chat client launches this executable directly over stdio, with no interpreter,
no pip and no virtualenv anywhere in the picture.
"""

import multiprocessing
import sys

import mmu_mcp_server

if __name__ == "__main__":
    multiprocessing.freeze_support()
    mmu_mcp_server.main()
    sys.exit(0)
