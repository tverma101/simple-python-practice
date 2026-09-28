import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ducky.runner import TestFailure, TestRunReport
from ducky.teacher_backend import TeacherBackendClient, TeacherBackendError


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/octet-stream") -> None:
        self._body = body
        self.headers = _FakeHeaders(content_type)

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TeacherBackendTests(unittest.TestCase):
    def _report(self) -> TestRunReport:
        return TestRunReport(
            lesson_id="lesson.complex_01",
            title="Normalize Scores",
            passed=False,
            returncode=1,
            duration_seconds=0.2,
            tests_total=2,
            tests_passed=0,
            tests_failed=2,
            failures=[
                TestFailure(
                    name="runtime path",
                    diagnosis="runtime_error",
                    expected={"count": 3, "total": 8},
                    actual=None,
                    stdout="debug: starting\ntrace: processing list",
                    error="ValueError: invalid score",
                    hint="Check the input data before summing it.",
                    raw_stdout="debug: starting\ntrace: processing list",
                    raw_error="ValueError: invalid score",
                )
            ],
            code_redacted="def normalize_scores(scores):\n    return scores\n",
            raw_stdout="debug: starting\ntrace: processing list",
            raw_stderr="Traceback (most recent call last):\n  ValueError: invalid score",
            raw_code="def normalize_scores(scores):\n    return scores\n",
        )

    def test_submit_report_posts_json_payload(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(b'{"ok": true, "status": "queued"}', "application/json")

        client = TeacherBackendClient(
            submit_url="https://teacher.example/api/submit",
            token="secret-token",
            timeout_seconds=5,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.submit_report(self._report(), student_name="Ada")

        self.assertEqual(result["status"], "queued")
        self.assertEqual(captured["url"], "https://teacher.example/api/submit")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["student_name"], "Ada")
        self.assertEqual(captured["body"]["report"]["lesson_id"], "lesson.complex_01")
        self.assertEqual(
            captured["body"]["report"]["code_redacted"], "def normalize_scores(scores):\n    return scores\n"
        )
        self.assertEqual(captured["body"]["metadata"]["passed"], False)
        self.assertFalse(captured["body"]["metadata"]["include_raw"])
        self.assertNotIn("raw_code", captured["body"]["report"])

    def test_submit_report_can_include_raw_payload(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(b"{}", "application/json")

        client = TeacherBackendClient(
            submit_url="https://teacher.example/api/submit",
            token="secret-token",
            timeout_seconds=5,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.submit_report(self._report(), student_name="Ada", include_raw=True)

        self.assertEqual(result, {})
        self.assertEqual(captured["body"]["report"]["raw_code"], "def normalize_scores(scores):\n    return scores\n")
        self.assertEqual(captured["body"]["report"]["raw_stdout"], "debug: starting\ntrace: processing list")
        self.assertEqual(captured["body"]["report"]["failures"][0]["raw_error"], "ValueError: invalid score")
        self.assertTrue(captured["body"]["metadata"]["include_raw"])

    def test_fetch_file_writes_downloaded_code(self):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse(b"print('new code')\n", "text/plain")

        client = TeacherBackendClient(
            fetch_url="https://teacher.example/api/latest_code.py",
            timeout_seconds=5,
        )

        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = client.fetch_file(output_path=Path(tmp) / "downloaded.py")
            self.assertEqual(out.read_text(encoding="utf-8"), "print('new code')\n")

    def test_fetch_file_follows_nested_download_url(self):
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) == 1:
                return _FakeResponse(b'{"download_url": "https://student.example/api/latest.py"}', "application/json")
            return _FakeResponse(b"print('student code')\n", "text/plain")

        client = TeacherBackendClient(
            fetch_url="https://teacher.example/api/latest.py",
            timeout_seconds=5,
        )

        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = client.fetch_file(output_path=Path(tmp) / "downloaded.py")
            self.assertEqual(out.read_text(encoding="utf-8"), "print('student code')\n")

        self.assertEqual(
            calls,
            [
                "https://teacher.example/api/latest.py",
                "https://student.example/api/latest.py",
            ],
        )

    def test_rejects_invalid_teacher_url(self):
        client = TeacherBackendClient(submit_url="ftp://teacher.example/api/submit", timeout_seconds=5)

        with self.assertRaises(TeacherBackendError):
            client.submit_report(self._report(), student_name="Ada")
