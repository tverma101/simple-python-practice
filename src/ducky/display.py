"""Rich terminal display helpers for Ducky test output.

Provides colour-coded pass/fail summaries, per-failure breakdowns,
and progress indicators — all with stdlib ANSI codes (no external deps).
"""

from __future__ import annotations

from .runner import TestFailure, TestRunReport

# ── ANSI helpers ────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
WHITE = "\033[37m"
RESET = "\033[0m"

_PASS = f"{GREEN}✓{RESET}"
_FAIL = f"{RED}✗{RESET}"


def _bar(percent: float, width: int = 30) -> str:
    """Draw a simple progress bar: ``[████████░░░░░░░░] 67%``."""
    filled = int(round(percent / 100 * width))
    empty = width - filled
    bar_color = GREEN if percent >= 100 else YELLOW if percent > 0 else RED
    return f"[{bar_color}{'█' * filled}{DIM}{'░' * empty}{RESET}] {percent:.0f}%"


def _format_value(value) -> str:
    """Format an expected/actual value for display.

    Uses ``repr()`` so type differences are visible:
    ``0`` (int) vs ``'0'`` (str) — the latter shows as ``'0'``
    with quotes.
    """
    if value is None:
        return f"{DIM}None{RESET}"
    return f"{WHITE}{value!r}{RESET}"


def render_test_summary(report: TestRunReport) -> str:
    """Render a one-line summary of test results."""
    if report.passed:
        return f"  {_PASS} {GREEN}All {report.tests_total} tests passed{RESET} ({report.duration_seconds:.1f}s)"
    pct = (report.tests_passed / max(report.tests_total, 1) * 100) if report.tests_total else 0
    return f"  {_FAIL} {RED}{report.tests_failed} of {report.tests_total} failed{RESET} — {_bar(pct)}"


def render_failure_detail(failure: TestFailure, index: int = 0) -> str:
    """Render a single test failure with expected vs actual and hint."""
    lines = [
        f"\n  {_FAIL} {BOLD}{failure.name}{RESET}",
        f"  {DIM}{'─' * 50}{RESET}",
    ]

    # Show expected vs actual if available
    if failure.expected is not None or failure.actual is not None:
        lines.append(f"    {YELLOW}Expected:{RESET} {_format_value(failure.expected)}")
        lines.append(f"    {RED}Got:{RESET}      {_format_value(failure.actual)}")

    # Show error snippet
    if failure.error:
        error_preview = failure.error.strip()[:500]
        lines.append(f"    {error_preview}{RESET}")

    # Show hint
    if failure.hint:
        lines.append(f"    {CYAN}Hint:{RESET} {failure.hint}")

    return "\n".join(lines)


def render_text_report_rich(report: TestRunReport) -> str:
    """Full-colour replacement for ``render_text_report``.

    Returns a string suitable for printing to a terminal.
    """
    parts: list[str] = []

    # Header
    parts.append(f"\n{BOLD}  {report.title}{RESET}")
    parts.append(f"  {DIM}lesson: {report.lesson_id}{RESET}")
    parts.append("")

    # Summary bar
    parts.append(render_test_summary(report))

    # Failures
    if report.failures:
        parts.append(f"\n  {BOLD}Failures:{RESET}")
        for i, failure in enumerate(report.failures):
            parts.append(render_failure_detail(failure, i))

    # Footer
    parts.append("")
    if report.passed:
        parts.append(f"  {GREEN}{BOLD}All tests passed! Great work!{RESET}")
    else:
        parts.append(f"  {YELLOW}Edit your code and run{RESET} {BOLD}ducky test{RESET} {YELLOW}again.{RESET}")
        if report.tests_total > 0:
            failed = report.tests_failed
            passed = report.tests_passed
            parts.append(
                f"  {_bar(passed / max(report.tests_total, 1) * 100)}  "
                f"{GREEN}{passed} passed{RESET} / {RED}{failed} failed{RESET}"
            )

    parts.append("")
    return "\n".join(parts)


_DIAGNOSIS_LABELS = {
    "incorrect_logic": "The result is different from what the test expects",
    "assertion_failed": "The test check did not pass",
    "prints_instead_of_returns": "Your function printed a value instead of returning it",
    "none_returned": "Your function returned None",
    "wrong_type": "The value has the wrong type",
    "extra_spaces": "The text has different spacing",
    "case_mismatch": "The text has different capitalization",
    "boolean_logic": "The True/False logic is reversed",
    "container_value_mismatch": "The collection contains a different value",
    "collection_type_mismatch": "The collection type is different",
    "list_order_wrong": "The values are right, but their order is different",
    "syntax_error": "Python could not read the code",
    "function_not_found": "The expected function could not be found",
    "wrong_parameter_count": "The function accepts a different number of arguments",
    "runtime_error": "The code stopped because of an error",
    "type_error": "Python received a value of the wrong type",
    "undefined_variable": "The code uses a name that has not been defined",
    "index_out_of_range": "The code accessed a position outside the sequence",
    "division_by_zero": "The code tried to divide by zero",
    "timeout": "The code took too long to finish",
    "interactive_input": "The code asked for keyboard input during an automated test",
}


def _friendly_error(error: str, max_lines: int = 4) -> str:
    """Keep the useful part of an exception while hiding traceback noise."""
    lines = [line.strip() for line in str(error or "").splitlines() if line.strip()]
    if not lines:
        return ""
    useful = [line for line in lines if not line.startswith(("File ", "Traceback "))]
    return "\n".join((useful or lines)[-max_lines:])


def render_student_report(report: TestRunReport) -> str:
    """Render an assignment result for a student-facing terminal GUI.

    This deliberately avoids raw traceback dumps and labels values in plain
    language. The richer diagnostic data remains in the saved JSON report.
    """
    lines = [f"\n{BOLD}  {report.title}{RESET}", f"  {DIM}{report.lesson_id}{RESET}", ""]
    if report.passed:
        lines.append(f"  {_PASS} {GREEN}Nice work — all {report.tests_total} checks passed.{RESET}")
    else:
        lines.append(
            f"  {_FAIL} {RED}{report.tests_failed} of {report.tests_total} checks need another look.{RESET}"
        )

    for index, failure in enumerate(report.failures, 1):
        lines.extend(["", f"  {BOLD}Check {index}: {failure.name}{RESET}"])
        explanation = _DIAGNOSIS_LABELS.get(failure.diagnosis, "This check did not match the expected result")
        lines.append(f"    {YELLOW}What happened:{RESET} {explanation}.")
        if failure.expected is not None or failure.actual is not None:
            lines.append(f"    {YELLOW}Expected:{RESET} {_format_value(failure.expected)}")
            lines.append(f"    {RED}Your result:{RESET} {_format_value(failure.actual)}")
        if failure.stdout.strip():
            printed = failure.stdout.strip()
            if len(printed) > 360:
                printed = printed[:360] + "..."
            lines.append(f"    {CYAN}Printed output:{RESET} {printed!r}")
        error = _friendly_error(failure.error)
        if error:
            lines.append(f"    {DIM}Python said:{RESET} {error}")
        if failure.hint:
            lines.append(f"    {CYAN}Try this:{RESET} {failure.hint}")

    if not report.passed:
        lines.extend(["", f"  {YELLOW}Edit the code and choose re-run to check your next attempt.{RESET}"])
    return "\n".join(lines) + "\n"


def render_attempt_feedback(attempt_number: int, report: TestRunReport) -> str:
    """Return a short encouragement string based on attempt count."""
    if report.passed:
        return f"{GREEN}Completed on attempt #{attempt_number}!{RESET}"

    messages = {
        1: f"{YELLOW}Attempt #{attempt_number}. Don't worry — read the hints above and try again.{RESET}",
        2: f"{YELLOW}Attempt #{attempt_number}. Getting closer! Check the expected vs got values.{RESET}",
        3: f"{YELLOW}Attempt #{attempt_number}. Still not quite right — try running {BOLD}ducky hint{RESET}{YELLOW} for more help.{RESET}",
    }

    if attempt_number in messages:
        return messages[attempt_number]
    return (
        f"{YELLOW}Attempt #{attempt_number}. If you're stuck, "
        f"try running {BOLD}ducky hint{RESET}{YELLOW} or read the instructions again.{RESET}"
    )
