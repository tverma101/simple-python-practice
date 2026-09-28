from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .redactor import redact_text
from .runner import TestRunReport


def report_to_dataset_rows(report: TestRunReport) -> list[dict]:
    rows = []
    for failure in report.failures:
        rows.append(
            {
                "task": "explain_python_test_failure",
                "lesson_id": report.lesson_id,
                "title": report.title,
                "student_code_redacted": redact_text(report.code_redacted, strip_comments=True),
                "test_failure": {
                    "name": failure.name,
                    "expected": failure.expected,
                    "actual": failure.actual,
                    "stdout": redact_text(failure.stdout),
                    "diagnosis": failure.diagnosis,
                },
                "target": {
                    "hint": redact_text(failure.hint),
                },
                "metadata": {
                    "verified_by_tests": report.passed,
                    "returncode": report.returncode,
                    "ducky_schema": "0.1",
                },
            }
        )
    return rows


def write_dataset_jsonl(reports: Iterable[TestRunReport], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for report in reports:
            for row in report_to_dataset_rows(report):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out_path
