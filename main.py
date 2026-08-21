"""Entry point for running from source and for the PyInstaller build."""
import sys

from chatgrab.app import run

if __name__ == "__main__":
    sys.exit(run())
