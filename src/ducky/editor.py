from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def is_vscode_available() -> bool:
    return shutil.which("code") is not None


def open_in_finder(path: Path) -> bool:
    """Open a folder in Finder on macOS. Returns True if successful."""
    if sys.platform != "darwin":
        return False
    resolved = path.resolve()
    if not resolved.exists():
        return False
    try:
        subprocess.Popen(
            ["open", str(resolved)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (FileNotFoundError, OSError):
        return False


def open_in_file_manager(path: Path) -> bool:
    """Open folder in the OS file manager (Finder on Mac, Nautilus etc. on Linux)."""
    resolved = path.resolve()
    if not resolved.exists():
        return False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(resolved)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "linux":
            # Try common file managers
            for cmd in ["nautilus", "dolphin", "thunar", "pcmanfm", "xdg-open"]:
                fm = shutil.which(cmd)
                if fm:
                    subprocess.Popen(
                        [fm, str(resolved)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return True
            return False
        else:
            return False
    except (FileNotFoundError, OSError):
        return False
    return True


def open_lesson(workspace_dir: Path, entry_file: str) -> bool:
    """Open a lesson in VS Code if available.

    Returns True if VS Code was launched, False otherwise.
    Callers should NOT force this — show the folder path instead.
    """
    code_path = shutil.which("code")
    if not code_path:
        return False
    code_file = workspace_dir / entry_file
    # In Codespaces the current VS Code window is already the student's
    # remote workspace. Passing the folder to `code` creates a second window
    # or tab and can even detach from the remote context. Reuse the active
    # window and open only the file instead. The same flag is also friendlier
    # for a locally launched GUI.
    in_codespaces = any(
        os.environ.get(name)
        for name in ("CODESPACES", "GITHUB_CODESPACES", "REMOTE_CONTAINERS")
    )
    try:
        if code_file.exists():
            command = ["code", "--reuse-window", "--goto", str(code_file)]
            if not in_codespaces:
                command.append(str(workspace_dir))
        else:
            command = ["code", "--reuse-window", str(workspace_dir)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, OSError):
        return False
    return True


def open_file(file_path: Path) -> bool:
    code_path = shutil.which("code")
    if code_path:
        try:
            subprocess.Popen(
                ["code", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            return False
        return True
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "linux":
            subprocess.Popen(
                ["xdg-open", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            return False
    except (FileNotFoundError, OSError):
        return False
    return True


def show_lesson_info(lesson_dir: Path, entry_file: str, title: str) -> None:
    """Print clear lesson folder information for the student.

    This replaces the old VS Code force-open with a visible,
    actionable display of where the lesson files are and how to
    work with them.
    """
    entry_path = lesson_dir / entry_file if entry_file else None
    tests_dir = lesson_dir / "tests"

    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    print(f"\n  {BOLD}📁 Your Working Directory{RESET}")
    print(f"  {'─' * 55}")
    print(f"  {BOLD}  {lesson_dir}{RESET}")
    print(f"  {'─' * 55}")
    print(f"  {DIM}Ducky installation: {Path(__file__).resolve().parent}{RESET}")
    print(f"  {DIM}Student files are stored separately in the folder above.{RESET}")

    print(f"\n  {BOLD}Files in this folder:{RESET}")
    if entry_path and entry_path.is_file():
        print(f"    {CYAN}📝 {entry_file}{RESET}  ← edit this file")
    if tests_dir.is_dir():
        test_count = len(list(tests_dir.glob("*.py")))
        print(f"    {CYAN}🧪 tests/{RESET}       ({test_count} test file(s))")
    # Prefer the exact on-disk spelling. macOS is usually case-insensitive,
    # so checking the lowercase path first would still print the wrong name
    # for a legacy uppercase-only pack.
    instructions_file = lesson_dir / "INSTRUCTIONS.md"
    if not instructions_file.is_file():
        instructions_file = lesson_dir / "instructions.md"
    if instructions_file.is_file():
        print(f"    {CYAN}📋 {instructions_file.name}{RESET} ← read this first")
    runner_file = lesson_dir / "run_tests.py"
    if runner_file.is_file():
        print(f"    {CYAN}▶ run_tests.py{RESET}   ← run this to test your work")

    print(f"\n  {BOLD}Quick start:{RESET}")
    if entry_path and entry_path.is_file():
        print(f"    {DIM}1.{RESET}  {YELLOW}cd {lesson_dir}{RESET}")
        print(f"    {DIM}2.{RESET}  Open {CYAN}{entry_file}{RESET} in your editor")
        print(f"    {DIM}3.{RESET}  Run {CYAN}python run_tests.py{RESET} to check your work")

    print(f"\n  {DIM}  → All your lesson files are in: {lesson_dir}{RESET}")


def get_setup_instructions() -> str:
    return (
        "VS Code 'code' command not found.\n"
        "To enable auto-open:\n"
        "  1. Open VS Code\n"
        "  2. Press Ctrl+Shift+P (or Cmd+Shift+P on Mac)\n"
        "  3. Type 'Shell Command: Install code command'\n"
        "  4. Select it and restart your terminal\n"
    )
