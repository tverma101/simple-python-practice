from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl
    fcntl = None
import html
import json
import uuid

from .paths import ensure_app_dirs
from .runner import TestRunReport


class ReportStore:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.paths = ensure_app_dirs(Path(base_dir) if base_dir else None)

    def save(self, report: TestRunReport) -> dict[str, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_id = f"run_{stamp}_{uuid.uuid4().hex[:8]}"
        json_path = self.paths["reports"] / f"{run_id}.json"
        html_path = self.paths["reports"] / f"{run_id}.html"
        try:
            json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        except TypeError:
            json_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        html_path.write_text(render_html_report(report), encoding="utf-8")
        events_path = self.paths["logs"] / "events.jsonl"
        with open(events_path, "a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(report.to_dict(), default=str) + "\n")
            finally:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_UN)
        return {"json": json_path, "html": html_path}


def _truncate(s, maxlen=200):
    if s is None:
        return ""
    s = str(s)
    return s[:maxlen] + "..." if len(s) > maxlen else s


def render_text_report(report: TestRunReport) -> str:
    """Render a test report as coloured terminal text.

    Uses ANSI escape codes for rich, colour-coded output.
    For plain-text output (e.g. logging, HTML), use ``render_text_report_plain``.
    """
    from .display import render_text_report_rich

    return render_text_report_rich(report)


def render_text_report_plain(report: TestRunReport) -> str:
    """Render a test report as plain text (no colour codes)."""
    lines = [
        f"Ducky Report: {report.title}",
        f"Lesson: {report.lesson_id}",
        f"Status: {'PASS' if report.passed else 'FAIL'}",
        f"Tests: {report.tests_passed} passed / {report.tests_failed} failed",
        "",
    ]
    if report.failures:
        lines.append("Failure summary:")
        for index, failure in enumerate(report.failures, 1):
            lines.append(f"  {index}. {failure.name} [{failure.diagnosis}]")
        lines.append("")
    for failure in report.failures:
        lines.extend(
            [
                f"Failure: {failure.name}",
                f"Diagnosis: {failure.diagnosis}",
                f"Expected: {_truncate(failure.expected)!r}",
                f"Actual: {_truncate(failure.actual)!r}",
                f"Stdout: {_truncate(failure.stdout)!r}",
                f"Hint: {failure.hint}",
                "",
            ]
        )
    return "\n".join(lines)


def render_html_report(report: TestRunReport) -> str:
    body = html.escape(render_text_report_plain(report))
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Ducky Report</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:900px;margin:40px auto;line-height:1.45}}code,pre{{background:#f5f5f5;padding:8px;border-radius:8px;display:block;white-space:pre-wrap}}</style>
</head><body><h1>Ducky Report</h1><pre>{body}</pre></body></html>"""
