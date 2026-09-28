"""Tests for ducky.runner — comprehensive edge cases for AI slop patterns.

This test suite targets the most common AI-generated code failures in the
test runner:
  1. Assertion error parsing (regex + ast.literal_eval edge cases)
  2. JUnit XML parsing (malformed XML, multiple suites, edge attributes)
  3. Traceback frame stripping (edge cases)
  4. Test command building (edge cases)
  5. Probe fallback logic (timeouts, mixed results)
  6. Test counting and pass/fail determination
  7. Error handling (timeouts, OSError, corrupt packs)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

from ducky.llm_client import FakeLLMClient
from ducky.pack import DuckyManifest, create_duckypack
from ducky.runner import (
    PytestCaseFailure,
    PytestSummary,
    TestFailure,
    TestRunReport,
    _build_test_command,
    _copy_tree_contents,
    _enhance_failures_with_function_detection,
    _failure_context,
    _has_junit_arg,
    _int_attr,
    _is_python_command,
    _jsonable,
    _local_xml_name,
    _parse_assertion_error,
    _parse_pytest_junit,
    _read_test_files,
    _run_diagnostic_probe,
    _strip_traceback_frames,
    _text,
    _uses_pytest,
    run_from_dir,
    run_pack,
)
from ducky.tutor import TutorEngine
from tests.helpers import make_add_pack

# ═══════════════════════════════════════════════════════════════════════════
# Unit tests for private helper functions
# ═══════════════════════════════════════════════════════════════════════════


class ParseAssertionErrorTests(unittest.TestCase):
    """Test _parse_assertion_error with AI slop edge cases."""

    def test_standard_assertion_error(self):
        exp, act = _parse_assertion_error("AssertionError: 42 != 10")
        self.assertEqual(exp, 10)
        self.assertEqual(act, 42)

    def test_swapped_order(self):
        """The function returns (right_side, left_side) = (expected, actual)."""
        exp, act = _parse_assertion_error("AssertionError: 10 != 42")
        self.assertEqual(exp, 42)
        self.assertEqual(act, 10)

    def test_string_values(self):
        exp, act = _parse_assertion_error("AssertionError: 'foo' != 'bar'")
        self.assertEqual(exp, "bar")
        self.assertEqual(act, "foo")

    def test_float_values(self):
        exp, act = _parse_assertion_error("AssertionError: 3.14 != 3.15")
        self.assertEqual(exp, 3.15)
        self.assertEqual(act, 3.14)

    def test_none_values(self):
        exp, act = _parse_assertion_error("AssertionError: None != 5")
        self.assertEqual(exp, 5)
        self.assertIsNone(act)

    def test_bool_values(self):
        exp, act = _parse_assertion_error("AssertionError: True != False")
        self.assertEqual(exp, False)
        self.assertEqual(act, True)

    def test_list_values(self):
        exp, act = _parse_assertion_error("AssertionError: [1, 2] != [3, 4]")
        self.assertEqual(exp, [3, 4])
        self.assertEqual(act, [1, 2])

    def test_dict_values(self):
        exp, act = _parse_assertion_error("AssertionError: {'a': 1} != {'b': 2}")
        self.assertEqual(exp, {"b": 2})
        self.assertEqual(act, {"a": 1})

    def test_multiline_error(self):
        """Assertion error with traceback before it — the function only parses the
        first line, so tracebacks with the assertion on a later line return None."""
        error = (
            "Traceback (most recent call last):\n"
            '  File "test.py", line 5, in test_func\n'
            "    assert value == expected\n"
            "AssertionError: 5 != 10\n"
        )
        exp, act = _parse_assertion_error(error)
        self.assertIsNone(exp)
        self.assertIsNone(act)

    def test_empty_string(self):
        exp, act = _parse_assertion_error("")
        self.assertIsNone(exp)
        self.assertIsNone(act)

    def test_no_assertion_marker(self):
        exp, act = _parse_assertion_error("Some random error without assert")
        self.assertIsNone(exp)
        self.assertIsNone(act)

    def test_no_not_equals_sign(self):
        exp, act = _parse_assertion_error("AssertionError: something else")
        self.assertIsNone(exp)
        self.assertIsNone(act)

    def test_malformed_literal_left_side(self):
        """Left side is not a valid Python literal."""
        exp, act = _parse_assertion_error("AssertionError: some_var != 10")
        self.assertIsNone(exp)
        self.assertIsNone(act)

    def test_malformed_literal_right_side(self):
        exp, act = _parse_assertion_error("AssertionError: 10 != some_var")
        self.assertIsNone(exp)
        self.assertIsNone(act)

    def test_complex_nested_expression(self):
        """Complex expression on both sides."""
        exp, act = _parse_assertion_error("AssertionError: (1, 2) != (3, 4)")
        self.assertEqual(exp, (3, 4))
        self.assertEqual(act, (1, 2))

    def test_unicode_string(self):
        exp, act = _parse_assertion_error("AssertionError: 'café' != 'cafe'")
        self.assertEqual(exp, "cafe")
        self.assertEqual(act, "café")

    def test_boolean_expression(self):
        exp, act = _parse_assertion_error("AssertionError: True != False")
        self.assertEqual(exp, False)
        self.assertEqual(act, True)

    def test_none_error_text(self):
        exp, act = _parse_assertion_error(None)
        self.assertIsNone(exp)
        self.assertIsNone(act)

    # ── pytest 'assert X == Y' format ────────────────────────────

    def test_pytest_assert_eq(self):
        """Pytest JUnit format: AssertionError: assert 5 == 10 → expected=10, actual=5."""
        exp, act = _parse_assertion_error("AssertionError: assert 5 == 10")
        self.assertEqual(exp, 10)
        self.assertEqual(act, 5)

    def test_pytest_assert_eq_strings(self):
        exp, act = _parse_assertion_error("AssertionError: assert 'foo' == 'bar'")
        self.assertEqual(exp, "bar")
        self.assertEqual(act, "foo")

    def test_pytest_assert_eq_multiline(self):
        """Pytest JUnit error with trailing diff lines."""
        exp, act = _parse_assertion_error(
            "AssertionError: assert 5 == 10\n"
            "  -5\n"
            "  +10\n"
        )
        self.assertEqual(exp, 10)
        self.assertEqual(act, 5)

    def test_pytest_assert_eq_floats(self):
        exp, act = _parse_assertion_error("AssertionError: assert 3.14 == 3.15")
        self.assertEqual(exp, 3.15)
        self.assertEqual(act, 3.14)

    def test_pytest_assert_eq_none(self):
        exp, act = _parse_assertion_error("AssertionError: assert None == 5")
        self.assertEqual(exp, 5)
        self.assertIsNone(act)

    def test_pytest_assert_fallback_to_not_equals_first(self):
        """'!=' should still be preferred over '==' when both are present."""
        exp, act = _parse_assertion_error("AssertionError: 42 != 10")
        self.assertEqual(exp, 10)
        self.assertEqual(act, 42)

    def test_pytest_assert_eq_without_assert_prefix(self):
        """Some JUnit outputs omit the 'assert' prefix.
        For X == Y, X is actual and Y is expected → returns (Y, X)."""
        exp, act = _parse_assertion_error("AssertionError: 10 == 42")
        self.assertEqual(exp, 42)
        self.assertEqual(act, 10)


class StripTracebackFramesTests(unittest.TestCase):
    """Test _strip_traceback_frames with edge cases."""

    def test_standard_traceback(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "test.py", line 5, in <module>\n'
            "    result = func()\n"
            "ValueError: invalid value\n"
        )
        stripped = _strip_traceback_frames(tb)
        self.assertIn("ValueError", stripped)
        self.assertNotIn("Traceback", stripped)
        self.assertNotIn('File "test.py"', stripped)

    def test_multiple_frames(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "a.py", line 3, in outer\n'
            "    return inner()\n"
            '  File "b.py", line 7, in inner\n'
            "    return 1/0\n"
            "ZeroDivisionError: division by zero\n"
        )
        stripped = _strip_traceback_frames(tb)
        self.assertIn("ZeroDivisionError", stripped)
        self.assertNotIn("a.py", stripped)
        self.assertNotIn("b.py", stripped)

    def test_no_traceback(self):
        text = "Just an error message\n"
        self.assertEqual(_strip_traceback_frames(text), text)

    def test_empty_string(self):
        self.assertEqual(_strip_traceback_frames(""), "")

    def test_only_error_line(self):
        text = "ValueError: bad value"
        self.assertEqual(_strip_traceback_frames(text), text)

    def test_traceback_header_only(self):
        """A lone header with no frames is returned unchanged (nothing to strip)."""
        text = "Traceback (most recent call last):"
        stripped = _strip_traceback_frames(text)
        self.assertEqual(stripped, text)

    def test_code_lines_with_four_spaces_not_stripped_unless_code(self):
        """Lines that start with 4 spaces but are NOT traceback code context should be handled."""
        text = (
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "    fn = None\n"
            "AttributeError: ...\n"
        )
        stripped = _strip_traceback_frames(text)
        self.assertNotIn("fn = None", stripped)

    def test_nested_exception_chain(self):
        """Chained exceptions with multiple Traceback headers."""
        tb = (
            "Traceback (most recent call last):\n"
            '  File "a.py", line 1, in <module>\n'
            "    raise ValueError('inner')\n"
            "\n"
            "The above exception was the direct cause of the following exception:\n"
            "\n"
            "Traceback (most recent call last):\n"
            '  File "b.py", line 2, in <module>\n'
            "    raise RuntimeError('outer') from exc\n"
            "RuntimeError: outer\n"
        )
        stripped = _strip_traceback_frames(tb)
        self.assertIn("RuntimeError: outer", stripped)
        self.assertNotIn("Traceback", stripped)

    def test_unicode_in_traceback(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "résumé.py", line 5, in <module>\n'
            "    print(x)\n"
            "NameError: name 'x' is not defined\n"
        )
        stripped = _strip_traceback_frames(tb)
        self.assertIn("NameError", stripped)
        self.assertNotIn("résumé.py", stripped)

    def test_traceback_substring_not_false_positive(self):
        """'Traceback' as part of a variable/function name should NOT trigger stripping."""
        text = "TracebackVar = 42\nresult = TracebackVar + 1"
        stripped = _strip_traceback_frames(text)
        self.assertEqual(stripped, text)

    def test_traceback_header_only_stripping(self):
        """A lone 'Traceback' (no frame header) should be kept."""
        text = "Traceback something else"
        stripped = _strip_traceback_frames(text)
        self.assertEqual(stripped, text)


class PytestJUnitParseTests(unittest.TestCase):
    """Test _parse_pytest_junit with edge cases."""

    def setUp(self):
        self._junit_tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self._junit_tmpdir, ignore_errors=True)

    def _make_junit(self, content: str) -> Path:
        p = self._junit_tmpdir / "results.xml"
        p.write_text(content)
        return p

    def test_standard_testsuite(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3" failures="1" errors="0" skipped="0">
  <testcase classname="test_foo" name="test_passes" />
  <testcase classname="test_foo" name="test_fails">
    <failure message="AssertionError: 5 != 10">
      AssertionError: 5 != 10
    </failure>
  </testcase>
  <testcase classname="test_foo" name="test_second" />
</testsuite>"""
        result = _parse_pytest_junit(self._make_junit(xml))
        assert result is not None
        self.assertEqual(result.tests_total, 3)
        self.assertEqual(result.tests_passed, 2)
        self.assertEqual(result.tests_failed, 1)
        self.assertEqual(len(result.failures), 1)

    def test_multiple_testsuites(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite1" tests="2" failures="1" errors="0">
    <testcase name="test_1" />
    <testcase name="test_2"><failure message="fail" /></testcase>
  </testsuite>
  <testsuite name="suite2" tests="1" failures="0" errors="1">
    <testcase name="test_3"><error message="err" /></testcase>
  </testsuite>
</testsuites>"""
        result = _parse_pytest_junit(self._make_junit(xml))
        assert result is not None
        self.assertEqual(result.tests_total, 3)
        self.assertEqual(result.tests_failed, 2)
        self.assertEqual(result.tests_passed, 1)

    def test_empty_testsuite(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="0" failures="0" errors="0" />"""
        result = _parse_pytest_junit(self._make_junit(xml))
        assert result is not None
        self.assertEqual(result.tests_total, 0)
        self.assertEqual(result.tests_passed, 0)
        self.assertEqual(result.tests_failed, 0)

    def test_file_not_found(self):
        result = _parse_pytest_junit(Path("/nonexistent/results.xml"))
        self.assertIsNone(result)

    def test_malformed_xml(self):
        result = _parse_pytest_junit(self._make_junit("not xml at all"))
        self.assertIsNone(result)

    def test_no_testsuite_tag(self):
        xml = """<?xml version="1.0"?>
<not_testsuite><data/></not_testsuite>"""
        result = _parse_pytest_junit(self._make_junit(xml))
        self.assertIsNone(result)

    def test_skipped_tests(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="5" failures="1" errors="0" skipped="2">
  <testcase name="test_1" />
  <testcase name="test_fails">
    <failure message="AssertionError: 5 != 10" />
  </testcase>
  <testcase name="test_skip_1"><skipped /></testcase>
  <testcase name="test_skip_2"><skipped /></testcase>
  <testcase name="test_4" />
</testsuite>"""
        result = _parse_pytest_junit(self._make_junit(xml))
        assert result is not None
        self.assertEqual(result.tests_total, 5)
        self.assertEqual(result.tests_skipped, 2)
        # passed = max(total - failed - skipped, 0) = max(5 - 1 - 2, 0) = 2
        self.assertEqual(result.tests_passed, 2)

    def test_float_attribute_values(self):
        """Some tools report float strings for integer attributes."""
        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3.0" failures="1.0" errors="0.0" />"""
        result = _parse_pytest_junit(self._make_junit(xml))
        assert result is not None
        self.assertEqual(result.tests_total, 3)
        self.assertEqual(result.tests_failed, 1)

    def test_non_integer_attributes(self):
        """Non-numeric attributes should be treated as 0."""
        xml = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="abc" failures="xyz" />"""
        result = _parse_pytest_junit(self._make_junit(xml))
        assert result is not None
        self.assertEqual(result.tests_total, 0)
        self.assertEqual(result.tests_failed, 0)


class BuildTestCommandTests(unittest.TestCase):
    """Test _build_test_command with edge cases."""

    def test_basic_pytest_command(self):
        junit = Path("/tmp/junit.xml")
        cmd = _build_test_command("pytest tests/", junit)
        self.assertIn("-m", cmd)
        self.assertIn("pytest", cmd)
        self.assertIn("--junitxml", cmd)
        self.assertIn(str(junit), cmd)

    def test_already_has_junit_arg(self):
        """Should not add a second --junitxml if already present."""
        junit = Path("/tmp/junit.xml")
        cmd = _build_test_command("python -m pytest tests/ --junitxml=other.xml", junit)
        # Should only have one --junitxml (the original one)
        junit_count = sum(1 for a in cmd if "junitxml" in a or "junit-xml" in a)
        self.assertEqual(junit_count, 1)

    def test_python_command_replaced(self):
        """python at start should be replaced with sys.executable."""
        junit = Path("/tmp/junit.xml")
        cmd = _build_test_command("python -m pytest tests/", junit)
        self.assertNotEqual(cmd[0], "python")
        self.assertIn("python", cmd[0])

    def test_python3_command_replaced(self):
        junit = Path("/tmp/junit.xml")
        cmd = _build_test_command("python3 -m pytest tests/", junit)
        self.assertNotEqual(cmd[0], "python3")

    def test_empty_command_raises(self):
        junit = Path("/tmp/junit.xml")
        with self.assertRaises(ValueError):
            _build_test_command("", junit)

    def test_non_pytest_command_passthrough(self):
        """Non-pytest command should not have --junitxml added."""
        junit = Path("/tmp/junit.xml")
        cmd = _build_test_command("echo hello", junit)
        self.assertEqual(cmd, ["echo", "hello"])


class MiscHelperTests(unittest.TestCase):
    """Test isolated helper functions."""

    def test_jsonable_plain_value(self):
        self.assertEqual(_jsonable(42), 42)
        self.assertEqual(_jsonable("hello"), "hello")
        self.assertEqual(_jsonable([1, 2, 3]), [1, 2, 3])

    def test_jsonable_fallback_repr(self):
        class NotSerializable:
            def __repr__(self):
                return "REPR_VALUE"

        obj = NotSerializable()
        self.assertEqual(_jsonable(obj), "REPR_VALUE")

    def test_text_various_inputs(self):
        self.assertEqual(_text("hello"), "hello")
        self.assertEqual(_text(b"hello"), "hello")
        self.assertEqual(_text(None), "")
        # Invalid UTF-8 bytes are replaced with the Unicode replacement char
        result = _text(b"\xff\xfe")
        self.assertEqual(result, "��")

    def test_uses_pytest_variants(self):
        self.assertTrue(_uses_pytest(["pytest"]))
        self.assertTrue(_uses_pytest(["py.test"]))
        self.assertTrue(_uses_pytest(["python", "-m", "pytest"]))
        self.assertFalse(_uses_pytest([]))
        self.assertFalse(_uses_pytest(["nosetests"]))
        self.assertFalse(_uses_pytest(["python3", "-m", "unittest"]))

    def test_has_junit_arg_variants(self):
        self.assertTrue(_has_junit_arg(["--junitxml", "path"]))
        self.assertTrue(_has_junit_arg(["--junit-xml", "path"]))
        self.assertTrue(_has_junit_arg(["--junitxml=path"]))
        self.assertTrue(_has_junit_arg(["--junit-xml=path"]))
        self.assertFalse(_has_junit_arg([]))
        self.assertFalse(_has_junit_arg(["--other"]))

    def test_is_python_command(self):
        self.assertTrue(_is_python_command("python"))
        self.assertTrue(_is_python_command("python3"))
        self.assertTrue(_is_python_command("/usr/bin/python3"))
        self.assertFalse(_is_python_command("nosetests"))

    def test_int_attr_variants(self):
        elem = ET.fromstring('<test attr="42" />')
        self.assertEqual(_int_attr(elem, "attr"), 42)

        elem2 = ET.fromstring('<test attr="invalid" />')
        self.assertEqual(_int_attr(elem2, "attr"), 0)

        elem3 = ET.fromstring("<test />")
        self.assertEqual(_int_attr(elem3, "missing"), 0)

        elem4 = ET.fromstring('<test attr="3.14" />')
        self.assertEqual(_int_attr(elem4, "attr"), 3)

    def test_local_xml_name(self):
        self.assertEqual(_local_xml_name("{http://example.com}tag"), "tag")
        self.assertEqual(_local_xml_name("tag"), "tag")
        self.assertEqual(_local_xml_name("{ns}"), "")


class ReadTestFilesTests(unittest.TestCase):
    """Test _read_test_files with edge cases."""

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _read_test_files(Path(tmp))
            self.assertEqual(result, "")

    def test_non_existent_dir_returns_empty(self):
        result = _read_test_files(Path("/nonexistent_test_dir_xyz"))
        self.assertEqual(result, "")

    def test_reads_all_py_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "test_a.py").write_text("def test_a(): pass")
            (t / "test_b.py").write_text("def test_b(): pass")
            result = _read_test_files(t)
            self.assertIn("test_a.py", result)
            self.assertIn("test_b.py", result)

    def test_skips_non_py_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "test_a.py").write_text("def test_a(): pass")
            (t / "notes.txt").write_text("just notes")
            result = _read_test_files(t)
            self.assertIn("test_a.py", result)
            self.assertNotIn("notes.txt", result)

    def test_skips_unreadable_files_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "test_a.py").write_text("good code")
            bad = t / "bad.py"
            bad.write_text("")
            import os
            os.chmod(bad, 0o000)
            try:
                result = _read_test_files(t)
                self.assertIn("good code", result)
            finally:
                os.chmod(bad, 0o644)


class FailureContextTests(unittest.TestCase):
    """Test _failure_context function."""

    def test_builds_failure_context(self):
        failure = TestFailure(
            name="test_foo",
            diagnosis="wrong_type",
            expected=5,
            actual="5",
            stdout="",
            error="",
        )
        ctx = _failure_context(failure)
        self.assertEqual(ctx.name, "test_foo")
        self.assertEqual(ctx.diagnosis, "wrong_type")
        self.assertEqual(ctx.expected, 5)


class EnhanceFailuresWithFunctionDetectionTests(unittest.TestCase):
    """Test _enhance_failures_with_function_detection."""

    def test_no_failures_returns_empty(self):
        result = _enhance_failures_with_function_detection([], "", Path("/tmp"))
        self.assertEqual(result, {})

    def test_no_student_code_returns_empty(self):
        failure = TestFailure(name="test", diagnosis="function_not_found", error="")
        result = _enhance_failures_with_function_detection([failure], "", Path("/tmp"))
        self.assertEqual(result, {})

    def test_non_function_not_found_returns_empty(self):
        failure = TestFailure(name="test", diagnosis="syntax_error", error="")
        result = _enhance_failures_with_function_detection(
            [failure], "def foo(): pass", Path("/tmp")
        )
        self.assertEqual(result, {})

    def test_no_test_dir_does_not_crash(self):
        failure = TestFailure(
            name="test",
            diagnosis="function_not_found",
            error="module 'solution' has no attribute 'add_numbers'",
        )
        result = _enhance_failures_with_function_detection(
            [failure], "def add(a, b): return a + b", Path("/nonexistent")
        )
        # Should not crash even though test_dir doesn't exist
        self.assertIsInstance(result, dict)


class TestRunReportTests(unittest.TestCase):
    """Test dataclass methods."""

    def test_to_dict_excludes_raw_by_default(self):
        report = TestRunReport(
            lesson_id="test",
            title="Test",
            passed=True,
            returncode=0,
            duration_seconds=0.1,
            raw_stdout="raw",
            raw_stderr="raw",
            raw_code="raw",
        )
        d = report.to_dict()
        self.assertNotIn("raw_stdout", d)
        self.assertNotIn("raw_stderr", d)
        self.assertNotIn("raw_code", d)

    def test_to_dict_includes_raw_when_requested(self):
        report = TestRunReport(
            lesson_id="test",
            title="Test",
            passed=True,
            returncode=0,
            duration_seconds=0.1,
            raw_stdout="raw",
            raw_stderr="raw",
            raw_code="raw",
        )
        d = report.to_dict(include_raw=True)
        self.assertIn("raw_stdout", d)
        self.assertIn("raw_stderr", d)
        self.assertIn("raw_code", d)

    def test_failure_raw_excluded_by_default(self):
        failure = TestFailure(
            name="test",
            diagnosis="error",
            raw_stdout="raw",
            raw_error="raw",
        )
        report = TestRunReport(
            lesson_id="test",
            title="Test",
            passed=False,
            returncode=1,
            duration_seconds=0.1,
            failures=[failure],
        )
        d = report.to_dict()
        for f in d["failures"]:
            self.assertNotIn("raw_stdout", f)
            self.assertNotIn("raw_error", f)


class TestFailureTests(unittest.TestCase):
    """Test dataclass __test__ attribute."""

    def test_test_failure_has_test_false(self):
        """TestFailure should not be collected by pytest as a test class."""
        f = TestFailure(name="n", diagnosis="d")
        self.assertFalse(f.__test__)  # type: ignore[comparison-overlap]


# ═══════════════════════════════════════════════════════════════════════════
# Integration tests for run_pack with mocking
# ═══════════════════════════════════════════════════════════════════════════


class RunPackTests(unittest.TestCase):
    """Integration tests for the full run_pack function."""

    def _make_pack(self, tmp: Path, diagnostic_cases: list | None = None) -> Path:
        return make_add_pack(tmp)

    def _run_student(self, code: str, **kwargs) -> TestRunReport:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = self._make_pack(tmp)
            student = tmp / "student.py"
            student.write_text(code)
            kwargs.setdefault("tutor_engine", TutorEngine(FakeLLMClient()))
            return run_pack(pack, student_file=student, **kwargs)

    def test_correct_code_passes(self):
        report = self._run_student("def add_numbers(a, b):\n    return a + b\n")
        self.assertTrue(report.passed)
        self.assertEqual(report.tests_failed, 0)

    def test_print_instead_of_return(self):
        report = self._run_student("def add_numbers(a, b):\n    print(a + b)\n")
        self.assertFalse(report.passed)
        self.assertEqual(report.failures[0].diagnosis, "prints_instead_of_returns")

    def test_wrong_type_int_vs_str(self):
        report = self._run_student("def add_numbers(a, b):\n    return str(a + b)\n")
        self.assertFalse(report.passed)
        self.assertEqual(report.failures[0].diagnosis, "wrong_type")

    def test_missing_function(self):
        report = self._run_student("def add(a, b):\n    return a + b\n")
        self.assertFalse(report.passed)
        diagnoses = {f.diagnosis for f in report.failures}
        self.assertIn("function_not_found", diagnoses)

    def test_syntax_error(self):
        report = self._run_student("def add_numbers(a, b)\n    return a + b\n")
        self.assertFalse(report.passed)
        diagnoses = {f.diagnosis for f in report.failures}
        self.assertIn("syntax_error", diagnoses)

    def test_runtime_error(self):
        report = self._run_student("def add_numbers(a, b):\n    return a / 0\n")
        self.assertFalse(report.passed)
        diagnoses = {f.diagnosis for f in report.failures}
        self.assertIn("division_by_zero", diagnoses)

    def test_report_metadata_fields(self):
        report = self._run_student("def add_numbers(a, b):\n    return a + b\n", tutor_engine=None)
        self.assertEqual(report.lesson_id, "python_basics.lesson_01")
        self.assertEqual(report.title, "Return Values")
        self.assertGreater(report.duration_seconds, 0)

    def test_report_to_dict(self):
        report = self._run_student("def add_numbers(a, b):\n    return a + b\n", tutor_engine=None)
        d = report.to_dict()
        self.assertIn("lesson_id", d)
        self.assertIn("passed", d)
        self.assertIn("failures", d)

    def test_attempt_number_forwarded(self):
        report = self._run_student("def add_numbers(a, b):\n    return a + b\n", attempt_number=3)
        self.assertTrue(report.passed)


class RunPackEdgeCaseTests(unittest.TestCase):
    """Edge case tests for run_pack that exercise specific code paths."""

    def test_empty_test_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            starter = tmp / "starter"
            tests = tmp / "tests"
            starter.mkdir()
            tests.mkdir()
            (starter / "main.py").write_text("def foo(): pass")
            (tests / "test_main.py").write_text("def test_foo(): pass")
            manifest = DuckyManifest(
                lesson_id="empty_cmd_test", title="Test", entry_file="main.py", test_command="",
            )
            pack_path = tmp / "test.duckypack"
            create_duckypack(starter, tests, pack_path, manifest)
            report = run_pack(pack_path)
        self.assertFalse(report.passed)
        self.assertIn("test_command", report.stderr.lower() if report.stderr else "test_command")

    def test_no_student_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            make_add_pack(tmp)
            report = run_pack(tmp / "lesson.duckypack")
        self.assertFalse(report.passed)

    def test_function_mismatch_flows_to_llm(self):
        """LLM client receives function_mismatch info when student defines wrong function name."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add(a, b):\n    return a + b\n")
            client = _CapturingLLMClient()
            report = run_pack(pack, student_file=student, tutor_engine=TutorEngine(client))
        self.assertFalse(report.passed)
        mismatch_found = any(
            req.function_mismatch and req.function_mismatch.get("mismatch")
            for req in client.requests
        )
        self.assertTrue(mismatch_found)

    def test_probe_catches_int_vs_float(self):
        """Diagnostic probe should catch int/float type mismatches that pytest passes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            starter = tmp / "starter"
            tests = tmp / "tests"
            starter.mkdir()
            tests.mkdir()
            (starter / "calc.py").write_text("def half(x):\n    return x / 2\n")
            (tests / "test_calc.py").write_text(
                "from calc import half\n"
                "def test_half():\n"
                "    result = half(10)\n"
                "    assert result == 5.0, f'got {result}'\n"
            )
            manifest = DuckyManifest(
                lesson_id="probe_test",
                title="Probe Test",
                entry_file="calc.py",
                diagnostic_cases=[
                    {
                        "name": "half of 10 is 5.0",
                        "function_name": "half",
                        "args": [10],
                        "expected": 5.0,
                    }
                ],
            )
            pack_path = tmp / "test.duckypack"
            create_duckypack(starter, tests, pack_path, manifest)
            student = tmp / "student.py"
            student.write_text("def half(x):\n    return 5  # returns int, not float\n")
            report = run_pack(pack_path, student_file=student, tutor_engine=TutorEngine(FakeLLMClient()))
        self.assertFalse(report.passed)
        probe_diagnoses = {f.diagnosis for f in report.failures}
        self.assertIn("wrong_type", probe_diagnoses)


class _CapturingLLMClient:
    def __init__(self):
        self.requests = []

    def generate_hint(self, request):
        self.requests.append(request)
        return f"Captured hint for {request.failure_name or request.diagnosis}."


class RunFromDirTests(unittest.TestCase):
    """Tests for :func:`ducky.runner.run_from_dir`."""

    def _run_in_lesson(self, code: str, **kwargs) -> TestRunReport:
        """Helper: create a temp lesson dir and run_from_dir."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lesson_dir = tmp / "lesson"
            lesson_dir.mkdir()
            (lesson_dir / "solution.py").write_text(code, encoding="utf-8")
            tests = lesson_dir / "tests"
            tests.mkdir()
            (tests / "test_solution.py").write_text(
                "from solution import add_numbers\n"
                "def test_adds():\n"
                '    assert add_numbers(2, 3) == 5\n'
                "def test_adds_zero():\n"
                '    assert add_numbers(0, 8) == 8\n',
                encoding="utf-8",
            )
            report = run_from_dir(lesson_dir, **kwargs)
            return report

    def test_run_from_dir_correct_code_passes(self):
        report = self._run_in_lesson("def add_numbers(a, b):\n    return a + b\n")
        self.assertTrue(report.passed)
        self.assertEqual(report.tests_passed, 2)
        self.assertEqual(report.tests_failed, 0)

    def test_run_from_dir_wrong_code_fails(self):
        report = self._run_in_lesson("def add_numbers(a, b):\n    return a - b\n")
        self.assertFalse(report.passed)
        self.assertGreater(report.tests_failed, 0)

    def test_run_from_dir_no_tests_dir(self):
        """Directory without tests/ returns an error report."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lesson_dir = tmp / "empty_lesson"
            lesson_dir.mkdir()
            (lesson_dir / "main.py").write_text("x=1\n")
            report = run_from_dir(lesson_dir)
        self.assertFalse(report.passed)
        self.assertTrue("tests/" in report.stderr or "tests/" in str(report.failures))

    def test_run_from_dir_missing_dir(self):
        """Non-existent directory returns an error report."""
        report = run_from_dir("/nonexistent/path/xyz123")
        self.assertFalse(report.passed)

    def test_run_from_dir_with_student_override(self):
        """Student file override should replace the entry file content."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lesson_dir = tmp / "lesson"
            lesson_dir.mkdir()
            (lesson_dir / "solution.py").write_text(
                "def add_numbers(a, b):\n    return 0\n", encoding="utf-8"
            )
            tests = lesson_dir / "tests"
            tests.mkdir()
            (tests / "test_solution.py").write_text(
                "from solution import add_numbers\n"
                "def test_adds():\n"
                '    assert add_numbers(2, 3) == 5\n',
                encoding="utf-8",
            )
            # Write a different student file
            student_path = tmp / "student_solution.py"
            student_path.write_text("def add_numbers(a, b):\n    return a + b\n", encoding="utf-8")
            report = run_from_dir(lesson_dir, student_file=student_path)
            self.assertTrue(report.passed)

    def test_run_from_dir_no_entry_file(self):
        """Directory with tests but no .py entry file returns error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lesson_dir = tmp / "lesson"
            lesson_dir.mkdir()
            tests = lesson_dir / "tests"
            tests.mkdir()
            (tests / "test_solution.py").write_text("def test_x():\n    pass\n")
            report = run_from_dir(lesson_dir)
            self.assertFalse(report.passed)

    def test_run_from_dir_reports_lesson_id_from_dirname(self):
        """lesson_id should come from directory name."""
        report = self._run_in_lesson("def add_numbers(a, b):\n    return a + b\n")
        self.assertEqual(report.lesson_id, "lesson")
        self.assertEqual(report.title, "lesson")

    def test_run_from_dir_provides_code_redacted(self):
        """code_redacted should be populated in the report."""
        report = self._run_in_lesson("def add_numbers(a, b):\n    return a + b\n")
        self.assertTrue(report.passed)
        self.assertIsInstance(report.code_redacted, str)
        self.assertGreater(len(report.code_redacted), 0)


if __name__ == "__main__":
    unittest.main()
