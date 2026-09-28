"""Direct launcher for the full-build Teacher GUI."""

from __future__ import annotations

from .app import DuckyTerminal


def main() -> None:
    """Open the teacher dashboard without making the teacher choose a mode."""
    app = DuckyTerminal()
    try:
        app._teacher_menu()
    except KeyboardInterrupt:
        print("\n\nBye!\n")
