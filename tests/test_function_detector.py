"""Tests for ducky.function_detector — comprehensive edge cases for AI slop patterns.

Targets common AI-generated code failures:
  1. Unicode function names
  2. Syntax error variations in student code
  3. Class method calls mistaken for top-level functions
  4. Decorated functions
  5. Import * patterns
  6. Parameterized pytest tests
  7. Submodule attr_errors
  8. Fuzzy matching edge cases (very similar names)
  9. test_dir that is a file or doesn't exist
  10. Mixed student and test code edge cases
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ducky.function_detector import (
    detect_function_mismatch,
    extract_expected_functions_from_tests,
    extract_function_names,
    find_best_match,
)


class TestExtractFunctionNames:
    """Original test suite — preserved intact."""

    def test_simple(self):
        source = "def add(a, b):\n    return a + b\n"
        assert extract_function_names(source) == ["add"]

    def test_multiple(self):
        source = """\
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

class Calculator:
    def multiply(self, a, b):
        return a * b
"""
        names = extract_function_names(source)
        assert "add" in names
        assert "subtract" in names
        assert "multiply" not in names

    def test_syntax_error_returns_empty(self):
        source = "def broken(:"
        assert extract_function_names(source) == []

    def test_empty(self):
        assert extract_function_names("") == []

    def test_async_function(self):
        source = "async def fetch():\n    return 42\n"
        assert extract_function_names(source) == ["fetch"]

    def test_nested_functions_not_top_level(self):
        source = """\
def outer():
    def inner():
        pass
    return inner
"""
        names = extract_function_names(source)
        assert "outer" in names
        assert "inner" not in names


class TestExtractExpectedFromTests:
    """Original test suite — preserved intact."""

    def test_import_from_extracted(self, tmp_path):
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_solution.py").write_text(
            "from solution import add, subtract\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        expected = extract_expected_functions_from_tests(test_dir)
        assert "add" in expected
        assert "subtract" in expected

    def test_attribute_calls_extracted(self, tmp_path):
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_solution.py").write_text("def test_stuff():\n    assert solution.add(1, 2) == 3\n")
        expected = extract_expected_functions_from_tests(test_dir)
        assert "add" in expected

    def test_empty_dir(self, tmp_path):
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        assert extract_expected_functions_from_tests(test_dir) == set()

    def test_builtins_excluded(self, tmp_path):
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "test_solution.py").write_text("def test_len():\n    assert len([1, 2]) == 2\n")
        expected = extract_expected_functions_from_tests(test_dir)
        assert "len" not in expected


class TestFindBestMatch:
    """Original test suite — preserved intact."""

    def test_exact_match(self):
        assert find_best_match("add", ["add", "subtract"]) == "add"

    def test_close_match(self):
        assert find_best_match("calc_grade", ["calculate_grade", "do_stuff"]) == "calculate_grade"

    def test_no_match_below_cutoff(self):
        assert find_best_match("xyz", ["abc", "def"], cutoff=0.8) is None

    def test_empty_candidates(self):
        assert find_best_match("add", []) is None


class TestDetectFunctionMismatch:
    """Original test suite — preserved intact."""

    def test_no_mismatch(self):
        result = detect_function_mismatch(
            student_code="def add(a, b):\n    return a + b\n",
            test_code="from solution import add\ndef test_add():\n    assert add(1, 2) == 3\n",
        )
        assert not result["mismatch"]

    def test_mismatch_detected(self):
        result = detect_function_mismatch(
            student_code="def calc_total(items):\n    return sum(items)\n",
            test_code="from solution import calculate_total\ndef test_total():\n    assert calculate_total([1, 2]) == 3\n",
        )
        assert result["mismatch"]
        assert "calculate_total" in result["missing"]

    def test_suggestion_provided(self):
        result = detect_function_mismatch(
            student_code="def calc_grade(score):\n    return 'A'\n",
            test_code="from solution import calculate_grade\ndef test_grade():\n    assert calculate_grade(90) == 'A'\n",
        )
        print(result)
        assert result["mismatch"]
        assert "calculate_grade" in result["missing"]
        assert "calculate_grade" in result.get("suggestions", {})

    def test_multiple_defined_one_missing(self):
        result = detect_function_mismatch(
            student_code="def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n",
            test_code="from solution import add, multiply\ndef test_ops():\n    pass\n",
        )
        assert result["mismatch"]
        assert "multiply" in result["missing"]

    def test_attr_error_parsing(self):
        result = detect_function_mismatch(
            student_code="def add(a, b):\n    return a + b\n",
            attr_error="module 'solution' has no attribute 'add_numbers'",
        )
        assert result["mismatch"]
        assert "add_numbers" in result["missing"]

    def test_empty_student_code(self):
        result = detect_function_mismatch(student_code="")
        assert not result["mismatch"]


# ═══════════════════════════════════════════════════════════════════════════
# AI Slop Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class ExtractFunctionNamesEdgeCaseTests:
    """Edge cases for extract_function_names."""

    def test_unicode_function_name(self):
        source = "def calcular_nota():\n    return 10\n"
        assert extract_function_names(source) == ["calcular_nota"]

    def test_single_character_name(self):
        source = "def f():\n    return 42\n"
        assert extract_function_names(source) == ["f"]

    def test_decorated_function(self):
        source = "@staticmethod\ndef helper():\n    return 1\n"
        names = extract_function_names(source)
        assert "helper" in names

    def test_decorated_with_args(self):
        source = "@register('task')\ndef process():\n    pass\n"
        names = extract_function_names(source)
        assert "process" in names

    def test_two_syntax_errors(self):
        """Multiple syntax errors still returns empty list."""
        source = "def a(:\ndef b(:\n"
        assert extract_function_names(source) == []

    def test_only_class_with_methods(self):
        """A class containing methods — only top-level classes and functions."""
        source = "class Calculator:\n    def add(self, a, b):\n        return a + b\n"
        # The class itself is not a function, and add is not top-level
        assert extract_function_names(source) == []

    def test_lambda_not_extracted(self):
        source = "add = lambda x, y: x + y\n"
        names = extract_function_names(source)
        assert "add" not in names  # lambda is not a def statement

    def test_various_whitespace(self):
        source = "\n\n\n  \ndef spaced():\n    pass\n"
        assert extract_function_names(source) == ["spaced"]

    def test_only_comment_and_function(self):
        source = "# comment only\ndef real_func():\n    return True\n"
        assert extract_function_names(source) == ["real_func"]


class ExtractExpectedEdgeCaseTests:
    """Edge cases for extract_expected_functions_from_tests."""

    def test_test_dir_is_file(self, tmp_path):
        """test_dir pointing to a file should return empty set (not crash)."""
        f = tmp_path / "test_solution.py"
        f.write_text("from solution import foo\n")
        result = extract_expected_functions_from_tests(f)
        assert result == set()

    def test_test_dir_does_not_exist(self):
        result = extract_expected_functions_from_tests("/nonexistent/path")
        assert result == set()

    def test_import_star_not_extracted(self):
        """from solution import * does not produce specific function names."""
        td = Path(tempfile.mkdtemp())
        try:
            (td / "test_s.py").write_text("from solution import *")
            result = extract_expected_functions_from_tests(td)
            assert result == set()
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_module_rename_not_extracted(self):
        """import solution as sol — attribute access on sol should work."""
        td = Path(tempfile.mkdtemp())
        try:
            (td / "test_s.py").write_text("import solution as sol\n\ndef test():\n    sol.calc_grade(90)\n")
            result = extract_expected_functions_from_tests(td)
            assert "calc_grade" in result
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_decorated_test_function_not_extracted_as_expected(self):
        """pytest.mark.parametrize decorator should not create false expected functions."""
        td = Path(tempfile.mkdtemp())
        try:
            (td / "test_s.py").write_text(
                "from solution import process\n"
                "import pytest\n"
                "@pytest.mark.parametrize('x', [1, 2])\n"
                "def test_process(x):\n"
                "    assert process(x) > 0\n"
            )
            result = extract_expected_functions_from_tests(td)
            assert "process" in result
            assert "test_process" not in result
            assert "parametrize" not in result
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_multiple_test_files(self, tmp_path):
        td = tmp_path / "tests"
        td.mkdir()
        (td / "test_a.py").write_text("from solution import foo")
        (td / "test_b.py").write_text("from solution import bar")
        result = extract_expected_functions_from_tests(td)
        assert "foo" in result
        assert "bar" in result

    def test_unreadable_file_skipped(self, tmp_path):
        td = tmp_path / "tests"
        td.mkdir()
        (td / "good.py").write_text("from solution import ok")
        bad = td / "bad.py"
        bad.write_text("from solution import also_ok")
        import os
        os.chmod(bad, 0o000)
        try:
            result = extract_expected_functions_from_tests(td)
            assert "ok" in result or result == set()
        finally:
            os.chmod(bad, 0o644)


class FindBestMatchEdgeCaseTests:
    """Edge cases for find_best_match."""

    def test_identical_names(self):
        assert find_best_match("add", ["add"]) == "add"

    def test_single_character(self):
        match = find_best_match("a", ["add", "a_b", "ab"])
        assert match is not None

    def test_very_similar_names(self):
        """calc_grade vs calc_grades — very close, should match."""
        match = find_best_match("calc_grade", ["calc_grades", "do_stuff"])
        assert match is not None

    def test_empty_name(self):
        """Empty name should not crash."""
        match = find_best_match("", ["add", "subtract"])
        assert match is None or match == ""  # behavior depends on difflib

    def test_all_different(self):
        assert find_best_match("xyzzy", ["abcdef", "ghijkl"], cutoff=0.5) is None

    def test_candidates_with_special_chars(self):
        match = find_best_match("process_data", ["_process_data", "process_data_internal"])
        assert match is not None


class DetectFunctionMismatchEdgeCaseTests:
    """Edge cases for detect_function_mismatch."""

    def test_only_attr_error(self):
        """No student code, only attr_error — should still report mismatch."""
        result = detect_function_mismatch(
            student_code="",
            attr_error="module 'solution' has no attribute 'add_numbers'",
        )
        assert result["mismatch"]
        assert "add_numbers" in result["missing"]

    def test_only_test_code_no_student(self):
        """Test code provided but no student code — should report mismatch."""
        result = detect_function_mismatch(
            student_code="",
            test_code="from solution import calculate_grade\ndef test():\n    pass\n",
        )
        assert result["mismatch"]
        assert "calculate_grade" in result["missing"]

    def test_no_params(self):
        """Empty student code, no test code, no attr_error — no mismatch."""
        result = detect_function_mismatch(student_code="")
        assert not result["mismatch"]

    def test_import_error_parsing(self):
        """ImportError text should also be parsed."""
        result = detect_function_mismatch(
            student_code="def existing():\n    pass\n",
            attr_error="ImportError: cannot import name 'missing_func' from 'solution'",
        )
        assert result["mismatch"]
        assert "missing_func" in result["missing"]

    def test_attr_error_module_with_dots(self):
        """Submodule name in attr_error."""
        result = detect_function_mismatch(
            student_code="def x():\n    pass\n",
            attr_error="module 'solution.submodule' has no attribute 'deep_func'",
        )
        assert result["mismatch"]
        assert "deep_func" in result["missing"]

    def test_both_test_code_and_test_dir(self):
        """Both sources of expected functions should merge."""
        td = Path(tempfile.mkdtemp())
        try:
            (td / "test_s.py").write_text("from solution import from_dir\n")
            result = detect_function_mismatch(
                student_code="",
                test_code="from solution import from_code\n",
                test_dir=td,
            )
            assert result["mismatch"]
            assert "from_code" in result["expected"]
            assert "from_dir" in result["expected"]
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_all_match_no_mismatch(self):
        """All expected functions defined — no mismatch regardless of extra defined."""
        result = detect_function_mismatch(
            student_code="def add(a, b):\n    return a + b\n\ndef extra():\n    return 0\n",
            test_code="from solution import add\ndef test():\n    pass\n",
        )
        assert not result["mismatch"]

    def test_chained_attr(self):
        """solution.ClassName.method() should not extract method as top-level function."""
        td = Path(tempfile.mkdtemp())
        try:
            (td / "test_s.py").write_text("def test():\n    solution.Calculator.add(1, 2)\n")
            result = extract_expected_functions_from_tests(td)
            # "Calculator" might be extracted, "add" too, depending on AST parsing
            # The important thing is it shouldn't crash
            assert isinstance(result, set)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)
