"""Entry point for running from source and for the PyInstaller build."""
# no-op touch: re-trigger the Windows build after clearing old artifacts
import sys

from chatgrab.app import run

if __name__ == "__main__":
    sys.exit(run())
