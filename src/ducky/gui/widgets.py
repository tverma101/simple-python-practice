"""Shared terminal UI helpers for the Ducky TUI."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

from ..progress import ProgressTracker
from ..runner import TestRunReport

logger = logging.getLogger(__name__)

# ── ANSI colour constants ──────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
RESET = "\033[0m"
UNDERLINE = "\033[4m"

# ── Student UI colour palette ──────────────────────────────────────

STUDENT_ACCENT = "\033[32m"  # Green — primary accent
STUDENT_DIM = "\033[2;32m"  # Dim green — subtle separators
STUDENT_HEADING = "\033[1;32m"  # Bold green — headings
STUDENT_PROMPT = "\033[32m"  # Green — prompt question mark
STUDENT_CARD = "\033[32m"  # Green — card borders
STUDENT_BADGE_PASS = "\033[1;32m"  # Bold green badge
STUDENT_BADGE_FAIL = "\033[1;31m"  # Bold red badge
STUDENT_BADGE_INFO = "\033[1;33m"  # Bold yellow badge

# ── Teacher UI colour palette ──────────────────────────────────────

TEACHER_ACCENT = "\033[34m"       # Blue — primary accent
TEACHER_DIM = "\033[2;34m"       # Dim blue — subtle separators
TEACHER_HEADING = "\033[1;34m"   # Bold blue — headings
TEACHER_PROMPT = "\033[34m"      # Blue — prompt question mark
TEACHER_CARD = "\033[34m"        # Blue — card borders

# Status colours (shared between teacher and student)
STATUS_ONLINE = "\033[32m"       # Green — student online
STATUS_OFFLINE = "\033[2m"       # Dim — student offline
STATUS_RUNNING = "\033[32m"      # Green — server running
STATUS_STOPPED = "\033[2m"       # Dim — server stopped
STATUS_PASS = "\033[1;32m"       # Bold green — pass badge
STATUS_FAIL = "\033[1;31m"       # Bold red — fail badge
STATUS_PENDING = "\033[1;33m"    # Bold yellow — pending badge

_NO_COLOR = bool(os.environ.get("NO_COLOR") or os.environ.get("DUCKY_NO_COLOR"))
_ASCII_ONLY = bool(os.environ.get("DUCKY_ASCII"))
if _NO_COLOR:
    class _NoColor(str):
        def __new__(cls):
            return str.__new__(cls, "")

        def __bool__(self) -> bool:
            # Keep exported palette values discoverable for callers while
            # rendering as an empty string in no-color mode.
            return True

    _PLAIN = _NoColor()
    for _name in (
        "BOLD", "DIM", "CYAN", "GREEN", "YELLOW", "RED", "MAGENTA", "RESET", "UNDERLINE",
        "STUDENT_ACCENT", "STUDENT_DIM", "STUDENT_HEADING", "STUDENT_PROMPT", "STUDENT_CARD",
        "STUDENT_BADGE_PASS", "STUDENT_BADGE_FAIL", "STUDENT_BADGE_INFO", "TEACHER_ACCENT",
        "TEACHER_DIM", "TEACHER_HEADING", "TEACHER_PROMPT", "TEACHER_CARD", "STATUS_ONLINE",
        "STATUS_OFFLINE", "STATUS_RUNNING", "STATUS_STOPPED", "STATUS_PASS", "STATUS_FAIL",
        "STATUS_PENDING",
    ):
        globals()[_name] = _PLAIN


# ── Terminal helpers ───────────────────────────────────────────────


def clear() -> None:
    if sys.stdout.isatty() and not os.environ.get("DUCKY_NO_CLEAR"):
        os.system("cls" if os.name == "nt" else "clear")


def terminal_width(default: int = 80) -> int:
    """Return a conservative printable width for narrow terminals and tests."""
    return max(24, shutil.get_terminal_size((default, 24)).columns - 4)


def banner() -> None:
    from .. import __build_type__, __version__

    if _ASCII_ONLY:
        role = "Teacher Platform" if __build_type__ == "full" else "Student Coding Tutor"
        print(f"\n  DUCKY - {role} v{__version__}\n")
        return

    is_teacher = __build_type__ == "full"
    subtitle = (
        f"{GREEN}{BOLD}Teacher Platform{RESET}  {DIM}v{__version__}{RESET}"
        if is_teacher
        else f"{STUDENT_ACCENT}{BOLD}Student Coding Tutor{RESET}  {DIM}v{__version__}{RESET}"
    )
    print(
        f"""
{CYAN}{BOLD}  ██████╗ ██╗   ██╗███████╗    ██╗   ██╗{RESET}
{CYAN}{BOLD}  ██╔══██╗╚██╗ ██╔╝██╔════╝    ██║   ██║{RESET}
{CYAN}{BOLD}  ██║  ██║ ╚████╔╝ █████╗      ██║   ██║{RESET}
{CYAN}{BOLD}  ██║  ██║  ╚██╔╝  ██╔══╝      ╚██╗ ██╔╝{RESET}
{CYAN}{BOLD}  ██████╔╝   ██║   ███████╗     ╚████╔╝ {RESET}
{CYAN}{BOLD}  ╚═════╝    ╚═╝   ╚══════╝      ╚═══╝  {RESET}
  {subtitle}{' ' * 30}
"""
    )


def separator() -> None:
    glyph = "-" if _ASCII_ONLY else "─"
    print(f"{DIM}{glyph * min(60, terminal_width())}{RESET}")


def prompt(msg: str, default: str = "") -> str:
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    val = input(f"  {CYAN}?{RESET} {msg}{hint}: ").strip()
    return val if val else default


def confirm(msg: str) -> bool:
    val = input(f"  {CYAN}?{RESET} {msg} [y/N]: ").strip().lower()
    return val in ("y", "yes")


def success(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def error(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def warning(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {YELLOW}→{RESET} {msg}")


def heading(msg: str) -> None:
    print(f"\n{BOLD}{UNDERLINE}{msg}{RESET}\n")


# ── Student UI primitives ──────────────────────────────────────────


def student_banner() -> None:
    """Print student-specific ASCII banner with green accent."""
    from .. import __version__

    if _ASCII_ONLY:
        print(f"\n  DUCKY - Student Coding Tutor v{__version__}\n")
        return

    print(
        f"""
{STUDENT_ACCENT}{BOLD}  ██████╗ ██╗   ██╗███████╗    ██╗   ██╗{RESET}
{STUDENT_ACCENT}{BOLD}  ██╔══██╗╚██╗ ██╔╝██╔════╝    ██║   ██║{RESET}
{STUDENT_ACCENT}{BOLD}  ██║  ██║ ╚████╔╝ █████╗      ██║   ██║{RESET}
{STUDENT_ACCENT}{BOLD}  ██║  ██║  ╚██╔╝  ██╔══╝      ╚██╗ ██╔╝{RESET}
{STUDENT_ACCENT}{BOLD}  ██████╔╝   ██║   ███████╗     ╚████╔╝ {RESET}
{STUDENT_ACCENT}{BOLD}  ╚═════╝    ╚═╝   ╚══════╝      ╚═══╝  {RESET}
{STUDENT_DIM}  Student Coding Tutor  v{__version__}{RESET}
"""
    )


def student_heading(msg: str) -> None:
    """Print a green bold heading (no underline — distinct from teacher)."""
    print(f"\n{STUDENT_HEADING}{msg}{RESET}\n")


def student_separator() -> None:
    """Print a green-tinted separator using dots instead of dashes."""
    glyph = "." if _ASCII_ONLY else "·"
    print(f"{STUDENT_DIM}{glyph * min(60, terminal_width())}{RESET}")


def student_prompt(msg: str, default: str = "", *, strip: bool = True) -> str:
    """Green-themed input prompt.

    Most student answers are commands or short values and should be trimmed.
    Predicted-output quiz answers are the exception: leading spaces can be
    part of the program's output, so callers may opt out of trimming.
    """
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    val = input(f"  {STUDENT_PROMPT}?{RESET} {msg}{hint}: ")
    if strip:
        val = val.strip()
    return val if val else default


def student_confirm(msg: str) -> bool:
    """Green-themed yes/no confirmation."""
    val = input(f"  {STUDENT_PROMPT}?{RESET} {msg} [y/N]: ").strip().lower()
    return val in ("y", "yes")


def card(title: str, lines: list[str], *, width: int = 56) -> None:
    """Print a bordered card using box-drawing characters.

    ┌─── Title ───────────────────────────────┐
    │  Line 1                                 │
    │  Line 2                                 │
    └─────────────────────────────────────────┘
    """
    import re

    width = min(width, terminal_width())
    left, right = ("+", "+") if _ASCII_ONLY else ("┌", "┐")
    vertical = "|" if _ASCII_ONLY else "│"
    bottom_left, bottom_right = ("+", "+") if _ASCII_ONLY else ("└", "┘")
    horizontal = "-" if _ASCII_ONLY else "─"

    # Title line
    title_str = f" {title} "
    pad = width - len(title_str) - 2  # -2 for ┌ and ┐
    if pad < 0:
        pad = 0
    print(f"  {STUDENT_CARD}{left}{horizontal * 2}{title_str}{horizontal * pad}{right}{RESET}")

    # Content lines. Long paths, URLs, and teacher messages are common in the
    # TUI. Wrap them before drawing the border so a narrow terminal never
    # pushes the right edge off screen. Overflowing lines keep their readable
    # content but may lose inline colour codes on wrapped fragments.
    content_width = max(8, width - 4)
    inner_width = width - 2  # inside the │ │
    for line in lines:
        raw = str(line)
        clean = re.sub(r"\033\[[0-9;]*m", "", raw)
        if len(clean) <= content_width:
            fragments = [raw]
        else:
            fragments = textwrap.wrap(
                clean,
                width=content_width,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]
        for fragment in fragments:
            visible_fragment = re.sub(r"\033\[[0-9;]*m", "", fragment)
            padding = max(0, inner_width - len(visible_fragment))
            print(
                f"  {STUDENT_CARD}{vertical}{RESET}  {fragment}"
                f"{' ' * padding}{STUDENT_CARD}{vertical}{RESET}"
            )

    # Bottom border — must span the same width as the top/content rows
    print(f"  {STUDENT_CARD}{bottom_left}{horizontal * width}{bottom_right}{RESET}")


def progress_bar(current: int, total: int, *, width: int = 30) -> str:
    """Return a visual progress bar string: [████████░░░░░░░░] 5/10"""
    total = max(0, int(total))
    current = max(0, int(current))
    if total <= 0:
        filled = 0
    else:
        filled = min(width, int((min(current, total) / total) * width))
    empty = width - filled
    fill, blank = ("#", ".") if _ASCII_ONLY else ("█", "░")
    bar = f"{STUDENT_ACCENT}{fill * filled}{DIM}{blank * empty}{RESET}"
    fraction = f"{min(current, total) if total else 0}/{total}"
    return f"[{bar}] {fraction}"


def badge(text: str, kind: str = "info") -> str:
    """Return a colored inline badge string.

    kind: 'pass' → green, 'fail' → red, 'info' → yellow, 'dim' → gray
    """
    colors = {
        "pass": STUDENT_BADGE_PASS,
        "fail": STUDENT_BADGE_FAIL,
        "info": STUDENT_BADGE_INFO,
        "dim": DIM,
    }
    color = colors.get(kind, STUDENT_BADGE_INFO)
    return f"{color}[{text}]{RESET}"


def student_success(msg: str) -> None:
    """Green success message."""
    print(f"  {GREEN}✓{RESET} {msg}")


def student_error(msg: str) -> None:
    """Red error message."""
    print(f"  {RED}✗{RESET} {msg}")


def student_warning(msg: str) -> None:
    """Yellow warning message."""
    print(f"  {YELLOW}!{RESET} {msg}")


def student_info(msg: str) -> None:
    """Green info message."""
    print(f"  {STUDENT_ACCENT}→{RESET} {msg}")


def navigation_hint(role: str, path: str, *, back_key: str = "0") -> None:
    """Show a compact breadcrumb and the universal back key.

    The teacher and student menus used to expose different meanings for the
    same screen (and often omitted a way back entirely).  Keeping this small
    primitive shared makes every main submenu communicate where the user is
    and how to leave it without learning a new key scheme.
    """
    print(f"  {DIM}{role} / {path}    [{back_key}] Back    [?] Help{RESET}")


# ── Notification system ────────────────────────────────────────────


def show_notifications(notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Display pending notifications as colored banners, return remaining (non-displayable).

    Each notification dict::
        {"level": "error"|"warning"|"success"|"info", "message": str, "source": str}

    Error/warning notifications are printed and consumed (removed from the caller's list
    after this function returns). Success/info are printed once then consumed.
    """
    if not notifications:
        return []

    shown = []
    for n in notifications:
        level = n.get("level", "info")
        msg = n.get("message", "")
        source = n.get("source", "")
        prefix = f"  [{source}] " if source else "  "

        if level == "error":
            print(f"  {RED}✗{RESET} {prefix}{msg}")
            shown.append(n)
        elif level == "warning":
            print(f"  {YELLOW}!{RESET} {prefix}{msg}")
            shown.append(n)
        elif level == "success":
            print(f"  {GREEN}✓{RESET} {prefix}{msg}")
            shown.append(n)
        elif level == "info":
            print(f"  {STUDENT_ACCENT}→{RESET} {prefix}{msg}")
            shown.append(n)

    # Return remaining (currently none, but allows future persistent notifs)
    # Shown notifications are consumed by the caller after this returns
    return [n for n in notifications if n not in shown]


# ── Teacher UI primitives ──────────────────────────────────────────


def teacher_banner() -> None:
    """Print teacher-specific ASCII banner with blue accent."""
    from .. import __version__

    if _ASCII_ONLY:
        print(f"\n  DUCKY - Teacher Platform v{__version__}\n")
        return

    print(
        f"""
{TEACHER_ACCENT}{BOLD}  ██████╗ ██╗   ██╗███████╗    ██╗   ██╗{RESET}
{TEACHER_ACCENT}{BOLD}  ██╔══██╗╚██╗ ██╔╝██╔════╝    ██║   ██║{RESET}
{TEACHER_ACCENT}{BOLD}  ██║  ██║ ╚████╔╝ █████╗      ██║   ██║{RESET}
{TEACHER_ACCENT}{BOLD}  ██║  ██║  ╚██╔╝  ██╔══╝      ╚██╗ ██╔╝{RESET}
{TEACHER_ACCENT}{BOLD}  ██████╔╝   ██║   ███████╗     ╚████╔╝ {RESET}
{TEACHER_ACCENT}{BOLD}  ╚═════╝    ╚═╝   ╚══════╝      ╚═══╝  {RESET}
{TEACHER_DIM}  Teacher Platform  v{__version__}{RESET}
"""
    )


def teacher_heading(msg: str) -> None:
    """Print a blue bold heading."""
    print(f"\n{TEACHER_HEADING}{msg}{RESET}\n")


def teacher_separator() -> None:
    """Print a blue-tinted separator using em-dash instead of dots."""
    glyph = "-" if _ASCII_ONLY else "─"
    print(f"{TEACHER_DIM}{glyph * min(60, terminal_width())}{RESET}")


def teacher_prompt(msg: str, default: str = "") -> str:
    """Blue-themed input prompt."""
    hint = f" {DIM}[{default}]{RESET}" if default else ""
    val = input(f"  {TEACHER_PROMPT}?{RESET} {msg}{hint}: ").strip()
    return val if val else default


def teacher_confirm(msg: str) -> bool:
    """Blue-themed yes/no confirmation."""
    val = input(f"  {TEACHER_PROMPT}?{RESET} {msg} [y/N]: ").strip().lower()
    return val in ("y", "yes")


def teacher_card(title: str, lines: list[str], *, width: int = 56) -> None:
    """Print a bordered card with blue accent.

    ┌─── Title ───────────────────────────────┐
    │  Line 1                                 │
    │  Line 2                                 │
    └─────────────────────────────────────────┘
    """
    import re

    width = min(width, terminal_width())
    left, right = ("+", "+") if _ASCII_ONLY else ("┌", "┐")
    vertical = "|" if _ASCII_ONLY else "│"
    bottom_left, bottom_right = ("+", "+") if _ASCII_ONLY else ("└", "┘")
    horizontal = "-" if _ASCII_ONLY else "─"

    title_str = f" {title} "
    pad = width - len(title_str) - 2
    if pad < 0:
        pad = 0
    print(f"  {TEACHER_CARD}{left}{horizontal * 2}{title_str}{horizontal * pad}{right}{RESET}")

    content_width = max(8, width - 4)
    inner_width = width - 2
    for line in lines:
        raw = str(line)
        clean = re.sub(r"\033\[[0-9;]*m", "", raw)
        if len(clean) <= content_width:
            fragments = [raw]
        else:
            fragments = textwrap.wrap(
                clean,
                width=content_width,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]
        for fragment in fragments:
            visible_fragment = re.sub(r"\033\[[0-9;]*m", "", fragment)
            padding = max(0, inner_width - len(visible_fragment))
            print(
                f"  {TEACHER_CARD}{vertical}{RESET}  {fragment}"
                f"{' ' * padding}{TEACHER_CARD}{vertical}{RESET}"
            )

    print(f"  {TEACHER_CARD}{bottom_left}{horizontal * width}{bottom_right}{RESET}")


def status_dot(online: bool) -> str:
    """Return a coloured ● for online/offline status."""
    if online:
        return f"{STATUS_ONLINE}●{RESET}"
    return f"{STATUS_OFFLINE}○{RESET}"


def status_badge(text: str, kind: str = "info") -> str:
    """Return a coloured inline badge for teacher use.

    kind: 'online' → green, 'offline' → dim, 'pass' → green, 'fail' → red, 'pending' → yellow
    """
    colors = {
        "online": STATUS_ONLINE,
        "offline": STATUS_OFFLINE,
        "pass": STATUS_PASS,
        "fail": STATUS_FAIL,
        "pending": STATUS_PENDING,
        "info": STATUS_PENDING,
    }
    color = colors.get(kind, STATUS_PENDING)
    return f"{color}[{text}]{RESET}"


def teacher_success(msg: str) -> None:
    """Blue success message."""
    print(f"  {GREEN}✓{RESET} {msg}")


def teacher_error(msg: str) -> None:
    """Red error message."""
    print(f"  {RED}✗{RESET} {msg}")


def teacher_warning(msg: str) -> None:
    """Yellow warning message."""
    print(f"  {YELLOW}!{RESET} {msg}")


def teacher_info(msg: str) -> None:
    """Blue info message."""
    print(f"  {TEACHER_ACCENT}→{RESET} {msg}")


# ── Progress helpers ───────────────────────────────────────────────


def record_progress(report: TestRunReport) -> None:
    diagnosis = report.failures[0].diagnosis if report.failures else ""
    tracker = ProgressTracker()
    try:
        tracker.record_attempt(report.lesson_id, report.passed, diagnosis)

        from ..config import load_config

        config = load_config()
        if config.get("auto_sync_progress", False):
            _try_sync_progress(tracker)
    finally:
        tracker.close()


def _try_sync_progress(tracker: ProgressTracker | None = None) -> None:
    from ..student_client import StudentClient

    client = StudentClient.from_config()
    if not client.teacher_url:
        return

    should_close = False
    if tracker is None:
        tracker = ProgressTracker()
        should_close = True

    try:
        unsynced = tracker.get_unsynced_attempts()
        if not unsynced:
            return
        result = client.sync_progress(unsynced)
        if result.get("synced_ids"):
            tracker.mark_attempt_synced(result["synced_ids"])
    except Exception as exc:
        logger.warning("Progress sync to teacher failed: %s", exc)
    finally:
        if should_close:
            tracker.close()


# ── Pack helper ────────────────────────────────────────────────────


def copy_pack_to_student_packs(pack_path: Path) -> Path | None:
    """Copy a built .duckypack to ~/.ducky/packs/ so students can see it.

    Returns the destination path, or None if no copy was needed or it failed.
    """
    try:
        import shutil

        dest = Path.home() / ".ducky" / "packs" / pack_path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == pack_path.stat().st_size:
            return dest
        shutil.copy2(str(pack_path), str(dest))
        return dest
    except OSError:
        return None
