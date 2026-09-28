"""Red-team cases for the student-facing grading boundary.

These are intentionally small submissions that have repeatedly caused false
greens, misleading reports, or leaked processes in educational autograders.
They are not a security sandbox: a student who can read the trusted tests can
still reverse-engineer expected answers. The cases here protect correctness,
test execution, and cleanup boundaries.
"""

from __future__ import annotations

import time
from pathlib import Path

from ducky.pack import DuckyManifest, create_duckypack
from ducky.runner import run_pack

DEFAULT_TESTS = """
from main import value


def test_value():
    assert value() == 1
"""


def _make_pack(
    root: Path,
    *,
    test_source: str = DEFAULT_TESTS,
    diagnostic_cases: list[dict] | None = None,
    test_command: str = "python -m pytest tests/",
    extra_test_files: dict[str, str] | None = None,
) -> Path:
    starter = root / "starter"
    tests = root / "tests"
    starter.mkdir(parents=True)
    tests.mkdir()
    (starter / "main.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (tests / "test_main.py").write_text(test_source, encoding="utf-8")
    for name, content in (extra_test_files or {}).items():
        (tests / name).write_text(content, encoding="utf-8")
    pack = root / "lesson.duckypack"
    create_duckypack(
        starter,
        tests,
        pack,
        DuckyManifest(
            lesson_id="adversarial",
            title="Adversarial",
            entry_file="main.py",
            test_command=test_command,
            diagnostic_cases=diagnostic_cases or [],
        ),
    )
    return pack


def _run(
    tmp_path: Path,
    student_code: str,
    *,
    test_source: str = DEFAULT_TESTS,
    diagnostic_cases: list[dict] | None = None,
    test_command: str = "python -m pytest tests/",
    extra_test_files: dict[str, str] | None = None,
    timeout_seconds: int = 3,
    enforce_ast: bool = True,
):
    pack = _make_pack(
        tmp_path / "pack",
        test_source=test_source,
        diagnostic_cases=diagnostic_cases,
        test_command=test_command,
        extra_test_files=extra_test_files,
    )
    student = tmp_path / "student.py"
    student.write_text(student_code, encoding="utf-8")
    return run_pack(
        pack,
        student_file=student,
        timeout_seconds=timeout_seconds,
        enforce_ast=enforce_ast,
    )


def _run_without_ast_filter(tmp_path: Path, student_code: str, **kwargs):
    """Grade a submission with the layer-1 static filter switched off.

    Several gates below (the no-tests-run check, the test-tree tamper check, the
    process-group kill) sit *downstream* of the AST filter, which now rejects
    the code they use as fixtures before it can ever run. Without this escape
    hatch those gates would silently stop being exercised and the suite would
    go green while covering nothing. The filter's own behaviour is asserted
    separately in test_sandbox.py.
    """
    kwargs.setdefault("enforce_ast", False)
    return _run(tmp_path, student_code, **kwargs)


def test_missing_required_function_is_a_failure(tmp_path):
    report = _run(
        tmp_path,
        "def other_name():\n    return 1\n",
        test_source="from main import value\ndef test_value():\n    assert value() == 1\n",
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "function_not_found"


def test_missing_student_submission_returns_a_report_instead_of_crashing(tmp_path):
    pack = _make_pack(tmp_path / "pack")
    report = run_pack(pack, student_file=tmp_path / "does-not-exist.py")
    assert not report.passed
    assert report.failures[0].name == "student submission"
    assert "not found" in report.failures[0].error.lower()


def test_wrong_parameter_count_is_a_failure(tmp_path):
    report = _run(
        tmp_path,
        "def value(one):\n    return 1\n",
        test_source="from main import value\ndef test_value():\n    assert value(1, 2) == 1\n",
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "wrong_parameter_count"


def test_syntax_error_is_a_failure(tmp_path):
    report = _run(tmp_path, "def value(:\n    return 1\n")
    assert not report.passed
    assert report.failures[0].diagnosis == "syntax_error"


def test_import_error_is_a_failure(tmp_path):
    report = _run(tmp_path, "import package_that_does_not_exist\ndef value():\n    return 1\n")
    assert not report.passed
    assert report.failures[0].diagnosis == "import_error"


def test_runtime_exception_is_a_failure(tmp_path):
    report = _run(tmp_path, "def value():\n    return 1 / 0\n")
    assert not report.passed
    assert report.failures[0].diagnosis == "division_by_zero"


def test_print_instead_of_return_is_caught_by_probe(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    print(1)\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "value", "function_name": "value", "expected": 1}],
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "prints_instead_of_returns"


def test_bool_int_mismatch_is_not_python_equality_pass(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    return True\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "value", "function_name": "value", "expected": 1}],
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "wrong_type"


def test_list_order_mismatch_is_reported(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    return [3, 2, 1]\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "value", "function_name": "value", "expected": [1, 2, 3]}],
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "list_order_wrong"


def test_unserializable_return_is_not_silently_stringified(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    return {1, 2}\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "value", "function_name": "value", "expected": [1, 2]}],
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "unserializable_return"


def test_diagnostic_cases_do_not_share_module_state(tmp_path):
    report = _run(
        tmp_path,
        "state = 0\ndef value():\n    global state\n    state += 1\n    return state\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[
            {"name": "first", "function_name": "value", "expected": 1},
            {"name": "second", "function_name": "value", "expected": 1},
        ],
    )
    assert report.passed


def test_diagnostic_runtime_error_cannot_be_hidden_by_green_pytest(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    raise RuntimeError('student code failed')\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "value", "function_name": "value", "expected": 1}],
    )
    assert not report.passed
    assert any(f.diagnosis == "runtime_error" for f in report.failures)


def test_malformed_diagnostic_case_is_a_grading_failure(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    return 1\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "missing expected", "function_name": "value"}],
    )
    assert not report.passed
    assert any("expected value" in f.error for f in report.failures)


def test_infinite_student_code_times_out(tmp_path):
    report = _run(
        tmp_path,
        "def value():\n    while True:\n        pass\n",
        timeout_seconds=1,
    )
    assert not report.passed
    assert report.failures[0].diagnosis == "timeout"


def test_interactive_student_code_fails_fast_instead_of_waiting_for_tui_input(tmp_path):
    started = time.monotonic()
    report = _run(
        tmp_path,
        "def value():\n    return input('enter a value: ')\n",
        test_source="def test_smoke():\n    assert True\n",
        diagnostic_cases=[{"name": "value", "function_name": "value", "expected": "answer"}],
        timeout_seconds=3,
    )
    elapsed = time.monotonic() - started
    assert not report.passed
    assert report.failures[0].diagnosis == "interactive_input"
    assert elapsed < 2, "interactive code should fail fast, not wait for the grader timeout"


def test_skipped_only_suite_is_not_green(tmp_path):
    report = _run(tmp_path, "import pytest\npytest.skip('not ready', allow_module_level=True)\n")
    assert not report.passed
    assert any(f.diagnosis == "no_tests_run" for f in report.failures)


def test_clean_exit_without_junit_report_is_not_green(tmp_path):
    # Runs with the layer-1 filter off on purpose: `os._exit(0)` is now rejected
    # statically, so with the filter on this test would prove nothing about the
    # no-tests-run gate it exists to cover.
    report = _run_without_ast_filter(tmp_path, "import os\nos._exit(0)\n")
    assert not report.passed
    assert any(f.diagnosis == "no_tests_run" for f in report.failures)


def test_student_cannot_rewrite_trusted_tests(tmp_path):
    # As above: `from pathlib import Path` is blocked statically, so the tamper
    # gate needs the filter off to be exercised at all.
    report = _run_without_ast_filter(
        tmp_path,
        "from pathlib import Path\n"
        "Path('tests/test_two.py').write_text('def test_two(): pass')\n"
        "def value():\n    return 1\n",
        test_source="from main import value\ndef test_one():\n    assert value() == 1\n",
        extra_test_files={"test_two.py": "def test_two():\n    assert False\n"},
    )
    assert not report.passed
    assert any(f.diagnosis == "test_tampering" for f in report.failures)


def test_timed_out_child_process_is_killed(tmp_path):
    marker = tmp_path / "child-survived"
    child_script = f"import time; time.sleep(2.0); open({str(marker)!r}, 'w').write('alive')"
    student = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
        "while True:\n    time.sleep(0.01)\n"
    )
    # Filter off: otherwise `import subprocess` is rejected statically and the
    # child is never spawned, which would make this test pass without ever
    # checking that the process group gets killed.
    report = _run_without_ast_filter(tmp_path, student, timeout_seconds=1)
    time.sleep(2.3)
    assert not report.passed
    assert not marker.exists()
