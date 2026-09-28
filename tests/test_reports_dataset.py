import json
import tempfile
import unittest
from pathlib import Path

from ducky.dataset_builder import report_to_dataset_rows, write_dataset_jsonl
from ducky.reports import ReportStore
from ducky.runner import TestFailure, TestRunReport


class ReportsDatasetTests(unittest.TestCase):
    def make_report(self):
        return TestRunReport(
            lesson_id="x.y",
            title="Demo",
            passed=False,
            returncode=1,
            duration_seconds=0.1,
            tests_total=1,
            tests_passed=0,
            tests_failed=1,
            failures=[TestFailure(name="case", diagnosis="wrong_type", expected=5, actual="5", hint="Check type")],
            code_redacted="def f():\n    return '5'\n",
        )

    def test_report_store_saves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ReportStore(tmp).save(self.make_report())
            self.assertTrue(paths["json"].exists())
            self.assertTrue(paths["html"].exists())
            self.assertTrue((Path(tmp) / "logs" / "events.jsonl").exists())

    def test_dataset_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.make_report()
            rows = report_to_dataset_rows(report)
            self.assertEqual(rows[0]["test_failure"]["diagnosis"], "wrong_type")
            out = write_dataset_jsonl([report], Path(tmp) / "data.jsonl")
            loaded = json.loads(out.read_text().splitlines()[0])
            self.assertEqual(loaded["task"], "explain_python_test_failure")
