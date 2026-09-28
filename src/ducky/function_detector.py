"""Function name detection and fuzzy matching for Ducky diagnostics.

Parses student code and test files via AST to detect function-name
mismatches and suggest accurate renames.
"""

from __future__ import annotations

import ast
import difflib
from pathlib import Path


def extract_function_names(source: str) -> list[str]:
    """Extract top-level function names from Python source code."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return sorted(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_top_level(node, tree)
    )


def extract_function_names_from_file(path: str | Path) -> list[str]:
    """Extract function names from a Python file."""
    try:
        return extract_function_names(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []


def _is_top_level(node: ast.AST, tree: ast.AST) -> bool:
    """Check if a function definition is at module level (not nested)."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if child is node:
                return isinstance(parent, ast.Module)
    return False


def _find_test_function_calls_in_source(source: str) -> set[str]:
    """Parse test files to find function names called in test bodies.

    Looks for patterns like ``student_code.function_name(...)``,
    ``from solution import function_name``, or bare function calls
    that match student-defined names.

    Avoids collecting methods on pytest fixtures (monkeypatch, capsys, etc.)
    and stdlib module attributes (inspect.getsource, ast.walk, etc.).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    called: set[str] = set()

    for node in ast.walk(tree):
        # import from solution: from solution import add, sub
        if isinstance(node, ast.ImportFrom):
            if node.module and "solution" in node.module.lower():
                for alias in node.names:
                    called.add(alias.name)

        # attribute access: solution.add(...), student_code.add(...)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                # e.g. solution.add() — collect 'add'
                if isinstance(node.func.value, ast.Name):
                    base_name = node.func.value.id
                    # Skip calls on pytest fixtures and stdlib modules
                    # to avoid collecting inspect.getsource, capsys.readouterr, etc.
                    if base_name not in _TEST_HELPER_NAMES:
                        attr = node.func.attr
                        if not attr.startswith("_") and attr not in _BUILTINS:
                            called.add(attr)
            elif isinstance(node.func, ast.Name):
                # bare function call: add(1, 2) — could be anything
                # Only add if it looks like a student function (not builtin)
                name = node.func.id
                if not name.startswith("_") and name not in _BUILTINS:
                    called.add(name)

    return called


def extract_expected_functions_from_tests(test_dir: str | Path) -> set[str]:
    """Extract function names that tests expect the student to define.

    Scans all .py files in the test directory for:
    - Import statements from student modules
    - Attribute access on student modules
    """
    test_dir = Path(test_dir)
    if not test_dir.is_dir():
        return set()

    expected: set[str] = set()
    for test_file in sorted(test_dir.glob("*.py")):
        try:
            source = test_file.read_text(encoding="utf-8")
            expected.update(_find_test_function_calls_in_source(source))
        except Exception:
            pass
    return expected


def _extract_imported_module(attr_error: str) -> str | None:
    """Extract module name from AttributeError or ImportError like
    ``module 'solution' has no attribute 'calc_grade'`` or
    ``cannot import name 'calc_grade' from 'solution'``.
    """
    import re

    match = re.search(r"module\s+['\"]([^'\"]+)['\"]", attr_error)
    if match:
        return match.group(1)
    match = re.search(r"from\s+(?:partially initialized module\s+)?['\"]([^'\"]+)['\"]", attr_error)
    if match:
        return match.group(1)
    return None


def _extract_missing_attr(attr_error: str) -> str | None:
    """Extract missing attribute name from AttributeError like
    ``...has no attribute 'calc_grade'``.
    """
    import re

    match = re.search(r"has no attribute\s+['\"]([^'\"]+)['\"]", attr_error)
    if match:
        return match.group(1)
    return None


def _extract_import_error_name(error_text: str) -> str | None:
    """Extract the missing name from ``ImportError: cannot import name 'X'``."""
    import re

    match = re.search(r"cannot import name\s+['\"]([^'\"]+)['\"]", error_text)
    if match:
        return match.group(1)
    return None


def find_best_match(name: str, candidates: list[str], cutoff: float = 0.4) -> str | None:
    """Find the closest matching name from candidates using difflib.

    Returns ``None`` if no match meets the cutoff threshold.
    """
    if not candidates:
        return None
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def detect_function_mismatch(
    student_code: str,
    test_code: str | None = None,
    test_dir: str | Path | None = None,
    attr_error: str = "",
) -> dict:
    """Detect function name mismatches between student code and tests.

    Returns a dict with keys:
    - ``mismatch``: bool — True if a mismatch was detected
    - ``expected``: list[str] — function names the test expects
    - ``defined``: list[str] — function names the student defined
    - ``missing``: list[str] — expected but not defined
    - ``suggestions``: dict[str, str] — mapping of expected → closest defined match
    """
    result: dict = {
        "mismatch": False,
        "expected": [],
        "defined": [],
        "missing": [],
        "suggestions": {},
    }

    # Parse AttributeError or ImportError for hints
    if attr_error:
        missing = _extract_missing_attr(attr_error) or _extract_import_error_name(attr_error)
        if missing:
            result["missing"].append(missing)

    # Extract student-defined functions
    defined = extract_function_names(student_code)
    result["defined"] = defined

    # Extract expected functions from test code/files
    expected: set[str] = set()
    if test_code:
        expected.update(_find_test_function_calls_in_source(test_code))
    if test_dir:
        expected.update(extract_expected_functions_from_tests(test_dir))

    # Also add any missing attribute/import name from error text
    if attr_error:
        missing_name = _extract_missing_attr(attr_error) or _extract_import_error_name(attr_error)
        if missing_name:
            expected.add(missing_name)

    if not expected:
        return result

    result["expected"] = sorted(expected)

    # Find which expected functions are missing from student code
    defined_set = set(defined)
    missing = expected - defined_set
    result["missing"] = sorted(missing)

    if missing:
        result["mismatch"] = True
        # Find fuzzy matches
        for miss in missing:
            match = find_best_match(miss, defined)
            if match:
                result["suggestions"][miss] = match

    return result


# Pytest fixture / stdlib module names whose method calls should NOT be
# treated as "expected student functions" (they're test infrastructure).
_TEST_HELPER_NAMES: set[str] = {
    "monkeypatch",
    "capsys",
    "capfd",
    "capfdbinary",
    "caplog",
    "tmpdir",
    "tmp_path",
    "recwarn",
    "request",
    "doctest_namespace",
    "ast",
    "inspect",
    "builtins",
    "_pytest",
}

# Common builtins and framework methods we exclude from "expected function" detection
_BUILTINS: set[str] = {
    "abs",
    "all",
    "any",
    "bool",
    "chr",
    "dict",
    "dir",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "callable",
    "getattr",
    "hasattr",
    "hash",
    "hex",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "oct",
    "ord",
    "pow",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
    "True",
    "False",
    "None",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "StopIteration",
    "RuntimeError",
    "ImportError",
    "OSError",
    "IOError",
    "assert",
    "input",
    "open",
    "super",
    "staticmethod",
    "classmethod",
    "property",
    # Common unittest / test framework methods
    "assertEqual",
    "assertNotEqual",
    "assertTrue",
    "assertFalse",
    "assertIs",
    "assertIsNot",
    "assertIsNone",
    "assertIsNotNone",
    "assertIn",
    "assertNotIn",
    "assertIsInstance",
    "assertNotIsInstance",
    "assertRaises",
    "assertRaisesRegex",
    "assertWarns",
    "assertWarnsRegex",
    "assertLogs",
    "assertAlmostEqual",
    "assertNotAlmostEqual",
    "assertGreater",
    "assertGreaterEqual",
    "assertLess",
    "assertLessEqual",
    "assertRegex",
    "assertNotRegex",
    "assertCountEqual",
    "assertMultiLineEqual",
    "assertSequenceEqual",
    "assertListEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertDictEqual",
    "setUp",
    "tearDown",
    "setUpClass",
    "tearDownClass",
    "run",
    "main",
    "skip",
    "skipIf",
    "skipUnless",
    "expectedFailure",
    # pytest fixtures / hooks
    "pytestmark",
    "pytest_fixture_setup",
    "pytest_configure",
    "pytest_collection_modifyitems",
    "pytest_addoption",
    "pytest_runtest_setup",
    "pytest_runtest_teardown",
    "pytest_runtest_call",
}
