from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .diagnostics import (
    _build_runtime_specific_hint,
    _parse_attribute_error,
    classify_failure,
)
from .function_detector import detect_function_mismatch
from .function_detector import extract_function_names as _extract_fn_names
from .function_detector import (
    find_best_match as _find_best_match,  # noqa: F401 — used in _enhance_failures_with_function_detection
)
from .llm_client import FailureContext, HintRequest
from .pack import DuckyManifest, manifest_relative_path, unpack_duckypack
from .redactor import redact_text
from .sandbox import (
    DEFAULT_POLICY,
    SandboxPolicy,
    SandboxViolation,
    build_sandbox_env,
    check_student_code,
    write_bootstrap,
)
from .tutor import TutorEngine


@dataclass
class TestFailure:
    __test__ = False

    name: str
    diagnosis: str
    expected: Any = None
    actual: Any = None
    stdout: str = ""
    error: str = ""
    hint: str = ""
    raw_stdout: str = ""
    raw_error: str = ""


@dataclass
class TestRunReport:
    __test__ = False

    lesson_id: str
    title: str
    passed: bool
    returncode: int
    duration_seconds: float
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    stdout: str = ""
    stderr: str = ""
    failures: list[TestFailure] = field(default_factory=list)
    code_redacted: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    raw_code: str = ""

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["failures"] = [asdict(f) for f in self.failures]
        if not include_raw:
            data.pop("raw_stdout", None)
            data.pop("raw_stderr", None)
            data.pop("raw_code", None)
            for failure in data.get("failures", []):
                failure.pop("raw_stdout", None)
                failure.pop("raw_error", None)
        return data


@dataclass
class PytestCaseFailure:
    name: str
    error: str


@dataclass
class PytestSummary:
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0
    failures: list[PytestCaseFailure] = field(default_factory=list)


_IGNORED_TEST_TREE_DIRS = {"__pycache__", ".pytest_cache"}

#: Exit code reported when a run is ended by the CPU budget rather than the
#: wallclock. Deliberately the same value as the wallclock path: both mean the
#: same thing to a student -- the code did not finish -- and ``diagnostics``
#: keys off the marker text rather than the code, so a distinct value here would
#: buy nothing and would split the timeout handling in two.
_CPU_LIMIT_EXIT = 124


def _snapshot_test_tree(tests_dir: Path) -> dict[str, str]:
    """Hash source/config files in ``tests/`` for tamper detection.

    Pytest is allowed to create cache directories, but a student submission
    must not rewrite, remove, or add test/config source files while the suite
    is being collected. This is a guardrail against accidental test mutation
    and the simplest class of false-pass submission.
    """
    if not tests_dir.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    tracked_suffixes = {".py", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml"}
    for path in sorted(tests_dir.rglob("*")):
        if not path.is_file() or any(part in _IGNORED_TEST_TREE_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in tracked_suffixes:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "<unreadable>"
        snapshot[str(path.relative_to(tests_dir))] = digest
    return snapshot


def _kill_process_tree(process: subprocess.Popen[bytes | str]) -> None:
    """Stop a timed-out child and its descendants when the platform supports it."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, OSError):
        pass


def _drain_stream(
    stream: Any,
    cap: int,
    on_overflow: Callable[[], None],
) -> tuple[str, bool]:
    """Read a pipe to EOF, keeping at most ``cap`` characters.

    Returns ``(text, truncated)``. The stream is always drained to EOF even
    after the cap is hit, because a child that fills its pipe and blocks would
    otherwise look exactly like an infinite loop. Once the cap is exceeded we
    stop *storing* and signal the caller to kill the process group.

    Reading in chunks rather than via ``communicate()`` is the whole point: the
    old implementation let ``while True: print("x")`` accumulate unbounded
    memory in the parent, which on a shared machine is a denial of service
    aimed at the grader rather than at the submission.
    """
    chunks: list[str] = []
    stored = 0
    truncated = False
    overflowed = False
    while True:
        try:
            chunk = stream.read(65536)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        if stored < cap:
            room = cap - stored
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
            if len(text) <= room:
                chunks.append(text)
                stored += len(text)
            else:
                chunks.append(text[:room])
                stored = cap
                truncated = True
        else:
            truncated = True
        if truncated and not overflowed:
            overflowed = True
            on_overflow()
    return "".join(chunks), truncated


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    max_output_bytes: int = DEFAULT_POLICY.max_output_bytes,
) -> tuple[str, str, int]:
    """Run a grading subprocess with descendant cleanup and output capping.

    Three independent containment mechanisms apply here, on top of whatever the
    caller already put in ``command`` (see :mod:`ducky.sandbox`):

    * ``stdin=DEVNULL`` so interactive starter code fails fast instead of
      hanging on a read.
    * ``start_new_session=True`` plus a process-group ``SIGKILL``, so a timed-out
      submission cannot leave helper processes behind.
    * A hard cap on captured output, enforced by killing the group the moment
      the cap is passed.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        # A lesson test must never inherit the teacher/student TUI's stdin.
        # Interactive starter code otherwise waits forever for input and looks
        # like a broken runner. Tests can still provide input explicitly by
        # monkeypatching builtins.input or using a controlled fixture.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=env,
        start_new_session=(os.name == "posix"),
    )

    overflowed = threading.Event()
    collected: dict[str, tuple[str, bool]] = {}

    def reader(stream: Any, key: str) -> None:
        collected[key] = _drain_stream(
            stream,
            max_output_bytes,
            lambda: (overflowed.set(), _kill_process_tree(process)),
        )

    threads = [
        threading.Thread(target=reader, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=reader, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for thread in threads:
            thread.join(timeout=2)
        stdout, _ = collected.get("stdout", ("", False))
        stderr, _ = collected.get("stderr", ("", False))
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout, stderr=stderr
        ) from None

    for thread in threads:
        thread.join(timeout=5)
        # A reader still blocked on a pipe we never closed would mean the child
        # inherited a descriptor we do not know about. Close explicitly so the
        # thread cannot wedge.
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass

    stdout, out_truncated = collected.get("stdout", ("", False))
    stderr, err_truncated = collected.get("stderr", ("", False))
    returncode = int(process.returncode or 0)

    if overflowed.is_set():
        # Distinguish "produced too much output" from "ran too long": a student
        # whose loop prints a million lines needs a different hint than one whose
        # loop prints nothing.
        #
        # The process is SIGKILLed on overflow, so returncode arrives as -9.
        # `-9 or 125` would keep -9, because -9 is truthy, and the distinctive
        # code would never reach the caller.
        note = f"\nDuckyOutputLimitError: output exceeded {max_output_bytes} bytes; run stopped"
        if out_truncated:
            stdout += note
        if err_truncated:
            stderr += note
        if returncode < 0:
            returncode = 125
        return stdout, stderr, returncode

    if returncode < 0:
        # Killed by a signal. Attribute it rather than reporting a bare -9.
        #
        # The signal a CPU-limit kill arrives as is platform-dependent, which is
        # why this is more than a lookup table. On macOS RLIMIT_CPU delivers
        # SIGXCPU. On Linux, when the soft and hard limits are set to the same
        # value, the kernel escalates straight to SIGKILL and SIGXCPU is never
        # observed. Getting this wrong made an infinite loop report as a generic
        # runtime error on every Linux box, so the SIGKILL case is resolved by
        # elimination: we did not trip the overflow kill (handled above) and we
        # are inside the wallclock, so the CPU budget is what ended it.
        signum = -returncode
        if signum == signal.SIGXCPU:
            stderr += (
                "\nDuckyCpuLimitError: run exceeded its CPU budget and was stopped. "
                "Check for a loop that never finishes, or a very slow operation."
            )
            return stdout, stderr, _CPU_LIMIT_EXIT
        if signum == getattr(signal, "SIGKILL", 9) and not out_truncated and not err_truncated:
            stderr += (
                "\nDuckyCpuLimitError: run was stopped after exhausting its CPU budget. "
                "Check for a loop that never finishes, or a very slow operation."
            )
            return stdout, stderr, _CPU_LIMIT_EXIT
        stderr += f"\nDuckySignalError: run was terminated by signal {signum}"

    return stdout, stderr, returncode


def _no_tests_failure(message: str) -> TestFailure:
    return TestFailure(
        name="test run",
        diagnosis="no_tests_run",
        error=message,
        raw_error=message,
    )


def _setup_failure_report(
    manifest: DuckyManifest | None,
    *,
    name: str,
    message: str,
) -> TestRunReport:
    """Return a failed report for an unusable lesson boundary.

    Student-facing callers should receive a normal diagnosis instead of a raw
    ``FileNotFoundError``/``BadZipFile`` when a lesson or submission is broken.
    """
    lesson_id = manifest.lesson_id if manifest else ""
    title = manifest.title if manifest else "Lesson unavailable"
    failure = TestFailure(
        name=name,
        diagnosis="runtime_error",
        error=message,
        raw_error=message,
    )
    return TestRunReport(
        lesson_id=lesson_id,
        title=title,
        passed=False,
        returncode=1,
        duration_seconds=0,
        tests_total=0,
        tests_passed=0,
        tests_failed=1,
        stderr=message,
        failures=[failure],
    )


def _copy_tree_contents(src: Path, dest: Path) -> None:
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def _is_python_command(value: str) -> bool:
    return Path(value).name in {"python", "python3"}


def _uses_pytest(command: list[str]) -> bool:
    if not command:
        return False
    command_name = Path(command[0]).name
    if command_name in {"pytest", "py.test"}:
        return True
    return len(command) >= 3 and command[1] == "-m" and command[2] == "pytest"


def _has_junit_arg(command: list[str]) -> bool:
    return any(
        arg in {"--junitxml", "--junit-xml"} or arg.startswith(("--junitxml=", "--junit-xml=")) for arg in command
    )


def _build_test_command(test_command: str, junit_xml_path: Path) -> list[str]:
    command = shlex.split(test_command)
    if not command:
        raise ValueError("manifest test_command is empty")

    if _uses_pytest(command) and Path(command[0]).name in {"pytest", "py.test"}:
        command = [sys.executable, "-m", "pytest", *command[1:]]
    elif _is_python_command(command[0]):
        command[0] = sys.executable

    if _uses_pytest(command) and not _has_junit_arg(command):
        command.extend(["--junitxml", str(junit_xml_path)])
    return command


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int_attr(element: ET.Element, name: str) -> int:
    try:
        return int(float(element.attrib.get(name, "0")))
    except ValueError:
        return 0


def _parse_pytest_junit(path: Path) -> PytestSummary | None:
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None

    if _local_xml_name(root.tag) == "testsuite":
        suites = [root]
    else:
        suites = [element for element in root.iter() if _local_xml_name(element.tag) == "testsuite"]

    if not suites:
        return None

    total = sum(_int_attr(suite, "tests") for suite in suites)
    failed = sum(_int_attr(suite, "failures") + _int_attr(suite, "errors") for suite in suites)
    skipped = sum(_int_attr(suite, "skipped") for suite in suites)
    passed = max(total - failed - skipped, 0)
    failures: list[PytestCaseFailure] = []

    for case in root.iter():
        if _local_xml_name(case.tag) != "testcase":
            continue
        failure_element = next(
            (child for child in case if _local_xml_name(child.tag) in {"failure", "error"}),
            None,
        )
        if failure_element is None:
            continue
        case_name = ".".join(part for part in [case.attrib.get("classname"), case.attrib.get("name")] if part)
        message = failure_element.attrib.get("message", "")
        details = "\n".join(part for part in [message, failure_element.text or ""] if part).strip()
        failures.append(PytestCaseFailure(name=case_name or "pytest case", error=details))

    return PytestSummary(
        tests_total=total,
        tests_passed=passed,
        tests_failed=failed,
        tests_skipped=skipped,
        failures=failures,
    )


def _failure_context(failure: TestFailure) -> FailureContext:
    return FailureContext(
        name=failure.name,
        diagnosis=failure.diagnosis,
        expected=failure.expected,
        actual=failure.actual,
        stdout=failure.stdout,
        error=failure.error,
    )


def _parse_assertion_error(error_text: str) -> tuple[Any | None, Any | None]:
    """Extract (actual, expected) from a unittest/pytest assertion error message.

    The function always returns ``(actual, expected)``.

    Handles the format produced by pytest/unittest:
        ``AssertionError: 42 != 10``  →  (42, 10)   # left=actual, right=expected
        ``AssertionError: 'foo' != 'bar'``  →  ('foo', 'bar')
        ``AssertionError: assert 5 == 10``  →  (5, 10)   # left=actual, right=expected
        ``AssertionError: assert 5 == 10\\n  -5\\n  +10``  →  (5, 10)

    Note: for a natural ``assert a != b`` failure the message is ``assert a != b``,
    which would be misparsed as ``actual=a, expected=b`` — but such failures are
    degenerate (the values were *not* equal, so the parsed pair is meaningless)
    and the caller falls through to a generic ``incorrect_logic`` diagnosis rather
    than relying on the swap.

    Returns (None, None) when parsing fails.
    """
    if not error_text:
        return None, None

    import ast
    import re

    first_line = error_text.split("\n", 1)[0]

    # Remove "AssertionError: " prefix if present
    stripped = re.sub(r"^AssertionError:\s*", "", first_line).strip()

    # Strip leading "assert " from the pytest format
    # e.g. "assert 10 == 42" → "10 == 42"
    if stripped.startswith("assert "):
        stripped = stripped[7:].strip()

    def _try_parse_pair(left: str, right: str) -> tuple[Any | None, Any | None] | None:
        """Try to literal_eval both sides. Returns (left_val, right_val) or None."""
        for val_str in (left, right):
            try:
                ast.literal_eval(val_str)
            except (ValueError, SyntaxError):
                return None
        try:
            return ast.literal_eval(left), ast.literal_eval(right)
        except (ValueError, SyntaxError):
            return None

    # Try " != " first (unittest style: left=actual, right=expected)
    idx = stripped.find(" != ")
    if idx >= 0:
        left = stripped[:idx].strip()
        right = stripped[idx + 4 :].strip()
        pair = _try_parse_pair(left, right)
        if pair is not None:
            # Return (expected, actual) = (right_val, left_val)
            return pair[1], pair[0]

    # Try " == " second (pytest assert style: X is actual, Y is expected)
    # e.g. "assert 5 == 10" → actual=5, expected=10 → return (10, 5)
    idx = stripped.find(" == ")
    if idx >= 0:
        left = stripped[:idx].strip()
        right = stripped[idx + 4 :].strip()
        pair = _try_parse_pair(left, right)
        if pair is not None:
            return pair[1], pair[0]

    return None, None


def _read_test_files(tests_dir: Path) -> str:
    """Read all test files and return combined content."""
    if not tests_dir.is_dir():
        return ""
    parts = []
    for test_file in sorted(tests_dir.glob("*.py")):
        try:
            content = test_file.read_text(encoding="utf-8")
            parts.append(f"# {test_file.name}\n{content}")
        except Exception:
            pass
    return "\n\n".join(parts)


def _attach_llm_hints(
    failures: list[TestFailure],
    *,
    manifest: DuckyManifest,
    code_redacted: str,
    stdout: str,
    stderr: str,
    tutor_engine: TutorEngine,
    instructions: str = "",
    test_cases: str = "",
    student_code: str = "",
    attempt_number: int = 1,
    function_mismatches: dict[str, dict] | None = None,
) -> None:
    if not failures:
        return

    function_mismatches = function_mismatches or {}
    breakdown = [_failure_context(failure) for failure in failures]
    output_excerpt = redact_text("\n".join(part for part in [stdout.strip(), stderr.strip()] if part))
    for failure in failures:
        # Carry over function mismatch info if available for this failure
        mismatch = function_mismatches.get(failure.name)
        failure.hint = tutor_engine.build_hint(
            HintRequest(
                lesson_id=manifest.lesson_id,
                title=manifest.title,
                code_snippet=code_redacted,
                diagnosis=failure.diagnosis,
                failure_name=failure.name,
                expected=failure.expected,
                actual=failure.actual,
                stdout=failure.stdout,
                error=failure.error,
                raw_output=output_excerpt,
                concept_tags=list(manifest.concept_tags),
                failure_breakdown=breakdown,
                instructions=instructions,
                test_cases=test_cases,
                student_code=student_code,
                attempt_number=attempt_number,
                function_mismatch=mismatch,
            ),
            attempt_number=attempt_number,
        )


def _strip_traceback_frames(text: str) -> str:
    """Strip Python traceback frame lines, keeping only the last error line.

    ``Traceback (most recent call last):``
    ``  File "...", line N, in <module>``  ← stripped
    ``    code``                             ← stripped
    ``AttributeError: ...``                  ← kept

    Returns the full text unchanged if it doesn't look like a traceback.
    """
    if not text or "Traceback (most recent call last)" not in text:
        return text
    lines = text.splitlines()
    # Keep only lines that are NOT traceback headers or stack frames
    kept = []
    for line in lines:
        # Skip traceback header
        if line.strip().startswith("Traceback"):
            continue
        # Skip stack frame lines:  File "...", line N
        if line.strip().startswith("File ") and "line " in line:
            continue
        # Skip code lines (indented source shown in traceback, like "    fn = ...")
        if line.startswith("    ") and line[4:].strip():
            continue
        kept.append(line)
    result = "\n".join(kept).strip()
    return result if result else text


def _enhance_failures_with_function_detection(
    failures: list[TestFailure],
    student_code: str,
    work_dir: Path,
) -> dict[str, dict]:
    """Post-process failures with function-name mismatch detection.

    For ``function_not_found`` failures, parses the AttributeError to
    suggest renames when the student defined the function under a different name.

    Returns a dict mapping failure names to their mismatch info, for use
    by the LLM hint prompt.
    """
    mismatches: dict[str, dict] = {}

    if not failures or not student_code:
        return mismatches

    has_function_not_found = any(f.diagnosis == "function_not_found" for f in failures)
    if not has_function_not_found:
        return mismatches

    test_dir = work_dir / "tests"

    for failure in failures:
        if failure.diagnosis != "function_not_found":
            continue

        # Pre-parse attribute error to extract missing function name
        # even when detect_function_mismatch has no test files to scan
        attr_text = failure.raw_error or failure.error
        _parsed_module, _parsed_attr = _parse_attribute_error(attr_text)

        mismatch = detect_function_mismatch(
            student_code=student_code,
            test_dir=test_dir,
            attr_error=attr_text,
        )

        # Fallback: use parsed attribute name if detection found nothing
        if not mismatch.get("missing") and _parsed_attr:
            mismatch.setdefault("missing", []).append(_parsed_attr)
            mismatch["mismatch"] = True
            mismatch.setdefault("expected", []).append(_parsed_attr)
            # Try fuzzy match against student-defined functions
            defined = _extract_fn_names(student_code)
            _closest = _find_best_match(_parsed_attr, defined) if defined else None
            if _closest:
                mismatch.setdefault("suggestions", {})[_parsed_attr] = _closest

        if mismatch.get("mismatch") and mismatch.get("suggestions"):
            mismatches[failure.name] = mismatch

            # Build a detailed hint with rename suggestions
            missing = mismatch["missing"]
            suggestions = mismatch["suggestions"]
            hint_parts = []
            for miss in missing:
                if miss in suggestions:
                    hint_parts.append(f"Rename ``{suggestions[miss]}()`` → ``{miss}()``")
                else:
                    hint_parts.append(f"Add a function named ``{miss}()``")

            if hint_parts:
                suffix = "; ".join(hint_parts)
                if failure.hint:
                    failure.hint = suffix + ". " + failure.hint
                else:
                    failure.hint = suffix

                # Store mismatch info in error for display
                if not failure.error or "Rename" not in failure.error:
                    extra = "\n".join(f"  → {h}" for h in hint_parts)
                    clean_error = _strip_traceback_frames(failure.error or "")
                    failure.error = f"Function name mismatch:\n{extra}\n{clean_error}"

    return mismatches


def _diagnostic_case_errors(cases: Any) -> list[str]:
    """Validate diagnostic metadata before generating the probe script."""
    if cases in (None, []):
        return []
    if not isinstance(cases, list):
        return ["manifest diagnostic_cases must be a list"]

    errors: list[str] = []
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"diagnostic case #{index} must be an object")
            continue
        function_name = case.get("function_name")
        if not isinstance(function_name, str) or not function_name.strip():
            errors.append(f"diagnostic case #{index} needs a non-empty function_name")
        if "expected" not in case:
            errors.append(f"diagnostic case #{index} needs an expected value")
        if not isinstance(case.get("args", []), list):
            errors.append(f"diagnostic case #{index} args must be a list")
        if not isinstance(case.get("kwargs", {}), dict):
            errors.append(f"diagnostic case #{index} kwargs must be an object")
    return errors


def _run_diagnostic_probe(
    work_dir: Path,
    manifest: DuckyManifest,
    timeout_seconds: int,
    policy: SandboxPolicy | None = None,
) -> list[dict[str, Any]]:
    policy = policy or policy_for_timeout(timeout_seconds)
    valid_cases = [
        case
        for case in manifest.diagnostic_cases
        if isinstance(case, dict)
        and isinstance(case.get("function_name"), str)
        and case.get("function_name", "").strip()
        and "expected" in case
        and isinstance(case.get("args", []), list)
        and isinstance(case.get("kwargs", {}), dict)
    ]
    if not valid_cases:
        return []
    module_name = Path(manifest.entry_file).with_suffix("").as_posix().replace("/", ".")
    probe = work_dir / "_ducky_probe.py"
    probe.write_text(
        """
import contextlib, importlib, io, json, sys, traceback
cases = __CASES__
module_name = __MODULE__
records = []
for case in cases:
    out = io.StringIO()
    try:
        # Each diagnostic case gets a fresh module import. A student function
        # should not pass or fail merely because an earlier case mutated a
        # module-level counter, cache, or singleton.
        importlib.invalidate_caches()
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
        fn = getattr(module, case["function_name"])
        args = case.get("args", [])
        kwargs = case.get("kwargs", {})
        with contextlib.redirect_stdout(out):
            result = fn(*args, **kwargs)
        try:
            json.dumps(result, allow_nan=False)
            actual = result
            actual_jsonable = True
        except (TypeError, ValueError):
            actual = repr(result)
            actual_jsonable = False
            # Strip any leaked memory addresses just in case
            import re as _re
            actual = _re.sub(r' at 0x[0-9a-fA-F]+', '', actual)
        records.append({
            "name": case.get("name", case["function_name"]),
            "function_name": case.get("function_name", ""),
            "expected": case.get("expected"),
            "actual": actual,
            "actual_type": type(result).__name__,
            "actual_jsonable": actual_jsonable,
            "stdout": out.getvalue(),
            "error": "",
        })
    except BaseException:
        records.append({
            "name": case.get("name", "case"),
            "function_name": case.get("function_name", ""),
            "expected": case.get("expected"),
            "actual": None,
            "actual_type": "NoneType",
            "actual_jsonable": True,
            "stdout": out.getvalue(),
            "error": traceback.format_exc(),
        })
print("DUCKY_PROBE_START")
print(json.dumps(records))
print("DUCKY_PROBE_END")
""".replace("__CASES__", repr(valid_cases)).replace("__MODULE__", repr(module_name)),
        encoding="utf-8",
    )
    env = build_sandbox_env(work_dir)
    bootstrap = write_bootstrap(work_dir / ".ducky-sandbox", policy)
    try:
        probe_stdout, _probe_stderr, _probe_returncode = _run_command(
            [sys.executable, str(bootstrap), sys.executable, str(probe)],
            cwd=work_dir,
            env=env,
            timeout=policy.wall_seconds,
            max_output_bytes=policy.max_output_bytes,
        )
    except subprocess.TimeoutExpired:
        return [
            {
                "name": case.get("name", "diagnostic case"),
                "function_name": case.get("function_name", ""),
                "expected": case.get("expected"),
                "actual": None,
                "actual_type": "NoneType",
                "actual_jsonable": True,
                "stdout": "",
                "error": f"DuckyTimeoutError: diagnostic case exceeded {timeout_seconds} seconds; check for an infinite loop or a very slow operation.",
            }
            for case in valid_cases
        ]
    text = probe_stdout
    if "DUCKY_PROBE_START" not in text:
        return []
    payload = text.split("DUCKY_PROBE_START", 1)[1].split("DUCKY_PROBE_END", 1)[0].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return []


def policy_for_timeout(timeout_seconds: int) -> SandboxPolicy:
    """Derive a sandbox policy from a legacy ``timeout_seconds`` argument.

    ``run_pack``/``run_from_dir`` grew a ``timeout_seconds`` parameter before the
    sandbox existed, and callers pass it inconsistent values (the TUI passes 15,
    the desktop app passes 8, the function default is 8). Rather than leave three
    different numbers meaning three different limits, this makes the wallclock
    the single source of truth and derives the rest: CPU is always a little
    under the wallclock so a spinning process hits the CPU limit and reports
    something more specific than "timed out".
    """
    wall = max(1, int(timeout_seconds))
    return replace(
        DEFAULT_POLICY,
        wall_seconds=wall,
        cpu_seconds=max(1, wall - 2),
    )


def _sandbox_violation_report(
    manifest: DuckyManifest | None,
    violation: SandboxViolation,
    code_redacted: str = "",
) -> TestRunReport:
    """Turn a layer-1 rejection into a normal-looking failed report.

    A student who reached for a blocked import should see the same report shape
    as any other failure, with a diagnosis that names the rule. Returning a
    special exception type to the UI would mean every caller needs to learn
    about sandboxing.
    """
    lesson_id = manifest.lesson_id if manifest else ""
    title = manifest.title if manifest else "Lesson unavailable"
    failure = TestFailure(
        name="code check",
        diagnosis="sandbox_violation",
        error=violation.message,
        hint=_build_runtime_specific_hint("sandbox_violation", 1),
        raw_error=f"[{violation.rule}] {violation}",
    )
    return TestRunReport(
        lesson_id=lesson_id,
        title=title,
        passed=False,
        returncode=1,
        duration_seconds=0,
        tests_total=0,
        tests_passed=0,
        tests_failed=1,
        stderr=violation.message,
        failures=[failure],
        code_redacted=code_redacted,
    )


def run_pack(
    pack_path: str | Path,
    *,
    student_file: str | Path | None = None,
    timeout_seconds: int = 8,
    tutor_engine: TutorEngine | None = None,
    attempt_number: int = 1,
    policy: SandboxPolicy | None = None,
    enforce_ast: bool = True,
) -> TestRunReport:
    """Grade a submission against a .duckypack.

    Args:
        enforce_ast: Set False only to exercise a downstream gate in isolation
            (the test suite does this so that the anti-cheat checks stay
            independently covered) or for trusted content. Student submissions
            must always be checked.
    """
    tutor_engine = tutor_engine or TutorEngine()
    policy = policy or policy_for_timeout(timeout_seconds)
    pack_path = Path(pack_path)
    with tempfile.TemporaryDirectory(prefix="ducky_run_") as tmp:
        tmp_path = Path(tmp)
        unpack_dir = tmp_path / "pack"
        try:
            manifest, _ = unpack_duckypack(pack_path, unpack_dir)
        except Exception as exc:
            return _setup_failure_report(
                None,
                name="lesson pack",
                message=f"Could not open lesson pack: {exc}",
            )
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        starter_dir = unpack_dir / "starter"
        tests_dir = unpack_dir / "tests"
        if not starter_dir.is_dir():
            return _setup_failure_report(
                manifest,
                name="starter files",
                message="Lesson pack is missing its starter/ directory.",
            )
        if not tests_dir.is_dir():
            return _setup_failure_report(
                manifest,
                name="test suite",
                message="Lesson pack is missing its tests/ directory.",
            )
        try:
            _copy_tree_contents(starter_dir, work_dir)
            shutil.copytree(tests_dir, work_dir / "tests")
        except OSError as exc:
            return _setup_failure_report(
                manifest,
                name="lesson files",
                message=f"Could not prepare lesson files: {exc}",
            )

        entry_path = work_dir / manifest_relative_path(manifest.entry_file, "entry_file")
        student_source: str | None = None
        if student_file:
            student_path = Path(student_file)
            if not student_path.is_file():
                return _setup_failure_report(
                    manifest,
                    name="student submission",
                    message=f"Student submission not found: {student_path}",
                )
            try:
                student_source = student_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return _setup_failure_report(
                    manifest,
                    name="student submission",
                    message=f"Could not read student submission '{student_path}': {exc}",
                )
            try:
                entry_path.parent.mkdir(parents=True, exist_ok=True)
                entry_path.write_text(student_source, encoding="utf-8")
            except OSError as exc:
                return _setup_failure_report(
                    manifest,
                    name="student submission",
                    message=f"Could not stage student submission: {exc}",
                )
        code_text = entry_path.read_text(encoding="utf-8") if entry_path.exists() else ""
        code_redacted = redact_text(code_text, strip_comments=True)

        # ── Sandbox layer 1: static AST allowlist ──────────────────────
        # Runs before we spend a subprocess on it. A rejection here is a
        # diagnosis, not a stack trace, so the student gets a usable message.
        # Only an explicitly supplied submission is checked; when we are grading
        # the pack's own starter/ code the teacher already vouched for it.
        if enforce_ast and student_source:
            try:
                check_student_code(student_source, policy=policy)
            except SandboxViolation as violation:
                return _sandbox_violation_report(manifest, violation, code_redacted)
            except SyntaxError:
                # Not a sandbox problem -- let the normal grading path report it
                # so the student sees a syntax error, not a policy message.
                pass

        env = build_sandbox_env(work_dir)
        bootstrap = write_bootstrap(tmp_path, policy)
        junit_xml_path = tmp_path / "pytest-results.xml"
        test_snapshot_before = _snapshot_test_tree(work_dir / "tests")
        try:
            command = _build_test_command(manifest.test_command, junit_xml_path)
        except ValueError:
            return TestRunReport(
                lesson_id=manifest.lesson_id,
                title=manifest.title,
                passed=False,
                returncode=1,
                duration_seconds=0,
                tests_total=0,
                tests_passed=0,
                tests_failed=1,
                stdout="",
                stderr="Invalid manifest: test_command is empty",
                failures=[TestFailure(name="manifest", diagnosis="runtime_error", error="test_command is empty")],
            )
        start = time.perf_counter()
        try:
            stdout, stderr, returncode = _run_command(
                [sys.executable, str(bootstrap), *command],
                cwd=work_dir,
                env=env,
                timeout=policy.wall_seconds,
                max_output_bytes=policy.max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr) + f"\nDuckyTimeoutError: test command exceeded {policy.wall_seconds}s"
            returncode = 124
        except OSError as exc:
            stdout = ""
            stderr = f"Could not start test command {command[0]!r}: {exc}"
            returncode = 127
        duration = time.perf_counter() - start

        pytest_summary = _parse_pytest_junit(junit_xml_path)
        probe_records = _run_diagnostic_probe(work_dir, manifest, timeout_seconds)
        test_snapshot_after = _snapshot_test_tree(work_dir / "tests")
        failures: list[TestFailure] = []

        for diagnostic_error in _diagnostic_case_errors(manifest.diagnostic_cases):
            failures.append(
                TestFailure(
                    name="diagnostic cases",
                    diagnosis="runtime_error",
                    error=diagnostic_error,
                    raw_error=diagnostic_error,
                )
            )

        # ── 1. Evaluate diagnostic probe results (ALWAYS) ─────────────
        # The probe catches type mismatches (e.g. int 80 vs float 80.0)
        # that pytest's == comparison treats as equal.
        if probe_records:
            for record in probe_records:
                expected = record.get("expected")
                actual = record.get("actual")
                actual_type = record.get("actual_type")
                actual_jsonable = record.get("actual_jsonable")
                if actual_type == "tuple" and isinstance(actual, list):
                    actual = tuple(actual)
                case_stdout = record.get("stdout", "")
                error = record.get("error", "")
                # Use ONLY the probe's own stdout/error as raw_output
                # (pytest stdout/stderr may contain AttributeError for
                #  unrelated missing functions, polluting the diagnosis)
                probe_raw = (error or "") + "\n" + (case_stdout or "")
                diagnosis = classify_failure(
                    expected=expected,
                    actual=actual,
                    stdout=case_stdout,
                    error=error,
                    raw_output=probe_raw,
                    actual_jsonable=actual_jsonable,
                )
                if error or expected != actual or (expected is not None and actual is not None and type(expected) is not type(actual)):
                    # Build a human-friendly failure name that includes the function
                    raw_name = record.get("name", "diagnostic case")
                    fn_name = record.get("function_name", "")
                    if fn_name and fn_name not in raw_name:
                        display_name = f"{fn_name}: {raw_name}"
                    else:
                        display_name = raw_name
                    failures.append(
                        TestFailure(
                            name=display_name,
                            diagnosis=diagnosis,
                            expected=expected,
                            actual=actual,
                            stdout=redact_text(case_stdout),
                            error=redact_text(error),
                            raw_stdout=case_stdout,
                            raw_error=error,
                        )
                    )

        # ── 2. Fallback: use pytest failures when probe returned nothing ──
        # Only when the test command itself failed (returncode != 0) and
        # the probe didn't already catch the issue.
        if returncode != 0 and not failures:
            if pytest_summary and pytest_summary.failures:
                for case in pytest_summary.failures:
                    # Try to extract expected/actual from assertion error
                    exp, act = _parse_assertion_error(case.error)
                    diagnosis = classify_failure(
                        expected=exp,
                        actual=act,
                        stdout=stdout,
                        error=case.error,
                        raw_output=stdout + stderr,
                    )
                    failures.append(
                        TestFailure(
                            name=case.name,
                            diagnosis=diagnosis,
                            expected=exp,
                            actual=act,
                            stdout=redact_text(stdout),
                            error=redact_text(case.error),
                            raw_stdout=stdout,
                            raw_error=case.error,
                        )
                    )
            elif not (_uses_pytest(command) and pytest_summary is not None):
                diagnosis = classify_failure(error=stderr, raw_output=stdout + stderr)
                failures.append(
                    TestFailure(
                        name="test run",
                        diagnosis=diagnosis,
                        error=redact_text(stderr or stdout),
                        raw_error=stderr or stdout,
                    )
                    )

        # A pytest command that produced no report, collected no tests, or
        # only skipped tests must never be reported as a pass. This prevents
        # clean exits such as os._exit(0), pytest.skip(...), and empty test
        # selections from becoming false green results.
        quality_gate_failed = False
        if _uses_pytest(command):
            if pytest_summary is None:
                if returncode == 0:
                    failures.append(_no_tests_failure("Pytest exited successfully without producing a JUnit test report."))
                    quality_gate_failed = True
            elif pytest_summary.tests_total <= 0 or (pytest_summary.tests_passed + pytest_summary.tests_failed) <= 0:
                failures.append(_no_tests_failure("Pytest did not execute any tests; skipped-only or empty runs are not passing."))
                quality_gate_failed = True

        if test_snapshot_before != test_snapshot_after:
            failures.append(
                TestFailure(
                    name="test suite",
                    diagnosis="test_tampering",
                    error="Student code changed files under tests/ while the grader was running.",
                    raw_error="Student code changed files under tests/ while the grader was running.",
                )
            )
            quality_gate_failed = True

        # ── 3. Function detection and LLM hints (if we found failures) ──
        if failures:
            function_mismatches = _enhance_failures_with_function_detection(
                failures,
                student_code=code_text,
                work_dir=work_dir,
            )
            _attach_llm_hints(
                failures,
                manifest=manifest,
                code_redacted=code_redacted,
                stdout=stdout,
                stderr=stderr,
                tutor_engine=tutor_engine,
                instructions=manifest.instructions,
                test_cases=_read_test_files(work_dir / "tests"),
                student_code=code_text,
                attempt_number=attempt_number,
                function_mismatches=function_mismatches,
            )

        # ── 4. Totals and pass/fail ────────────────────────────────────
        # The probe may detect failures that pytest silently passed (e.g.
        # int/float type mismatches).  We report those as failures even
        # when returncode is 0.
        # Any diagnostic failure, including an unexpected runtime exception,
        # must turn a pytest-green run red. Previously runtime_error probe
        # failures were excluded here, allowing a broken diagnostic case to
        # report a false pass.
        has_probe_failures = returncode == 0 and bool(failures)

        total_diagnostic_cases = len(manifest.diagnostic_cases) if manifest.diagnostic_cases else 0

        if pytest_summary:
            # Probe may cover more cases than pytest (e.g. 18 probe cases vs 17 pytest tests)
            total = max(pytest_summary.tests_total, total_diagnostic_cases)
            if has_probe_failures:
                # Probe caught something pytest missed — count them as extra failures
                pytest_missed = len(failures)
                failed = pytest_summary.tests_failed + pytest_missed
                passed_count = max(total - failed, 0)
            else:
                # When returncode is non-zero, failures come from both probe and pytest.
                # Use the larger of pytest's count and our detected failures.
                detected = len(failures) if returncode != 0 else 0
                failed = max(pytest_summary.tests_failed, detected)
                passed_count = max(total - failed - pytest_summary.tests_skipped, 0)
        else:
            total = total_diagnostic_cases or (0 if returncode else 1)
            failed = len(failures)
            passed_count = max(total - failed, 0) if total else (1 if returncode == 0 and not has_probe_failures else 0)

        if quality_gate_failed:
            failed = max(failed, 1)
            passed_count = max(total - failed - (pytest_summary.tests_skipped if pytest_summary else 0), 0)

        return TestRunReport(
            lesson_id=manifest.lesson_id,
            title=manifest.title,
            passed=returncode == 0 and not has_probe_failures and not quality_gate_failed,
            returncode=returncode,
            duration_seconds=duration,
            tests_total=total,
            tests_passed=passed_count,
            tests_failed=failed,
            stdout=redact_text(stdout),
            stderr=redact_text(stderr),
            failures=failures,
            code_redacted=code_redacted,
            raw_stdout=stdout,
            raw_stderr=stderr,
            raw_code=code_text,
        )


def _virtual_manifest_for_dir(work_dir: Path) -> DuckyManifest:
    """Build the minimal manifest needed to render a sandbox-violation report.

    ``run_from_dir`` grades a loose directory rather than a pack, so there is no
    real manifest to report against. This exists purely so a layer-1 rejection
    produces the same report shape as every other failure.
    """
    try:
        return DuckyManifest(lesson_id=work_dir.name, title=work_dir.name)
    except Exception:
        return DuckyManifest(lesson_id=work_dir.name, title="Lesson")


def run_from_dir(
    lesson_dir: str | Path,
    *,
    student_file: str | Path | None = None,
    timeout_seconds: int = 8,
    tutor_engine: TutorEngine | None = None,
    attempt_number: int = 1,
    policy: SandboxPolicy | None = None,
    enforce_ast: bool = True,
) -> TestRunReport:
    """Run tests directly from a lesson directory without a .duckypack.

    Creates a virtual manifest on-the-fly from the directory structure
    and runs pytest against the code in-place (no temp directory).

    Args:
        lesson_dir: Path to a lesson directory containing code and tests/.
        student_file: Optional override — use this source file instead
            of the default entry file found in the directory.
        timeout_seconds: Pytest timeout.
        tutor_engine: For LLM-powered hints (optional).
        attempt_number: Current attempt count for hint escalation.
        policy: Sandbox limits. Derived from ``timeout_seconds`` when omitted.
        enforce_ast: Set False only for trusted content -- a teacher grading
            their own material, or a fixture in the test suite. Student
            submissions must always be checked.

    Returns a :class:`TestRunReport`.
    """
    tutor_engine = tutor_engine or TutorEngine()
    policy = policy or policy_for_timeout(timeout_seconds)
    work_dir = Path(lesson_dir).resolve()
    if not work_dir.is_dir():
        return TestRunReport(
            lesson_id="",
            title="",
            passed=False,
            returncode=1,
            duration_seconds=0,
            tests_total=0,
            tests_passed=0,
            tests_failed=1,
            stderr=f"Lesson directory not found: {work_dir}",
            failures=[TestFailure(name="lesson_dir", diagnosis="runtime_error",
                                  error=f"Directory not found: {work_dir}")],
        )

    # ── Build a virtual manifest ────────────────────────────────
    tests_dir = work_dir / "tests"
    if not tests_dir.is_dir():
        return TestRunReport(
            lesson_id=work_dir.name,
            title=work_dir.name,
            passed=False,
            returncode=1,
            duration_seconds=0,
            tests_total=0,
            tests_passed=0,
            tests_failed=1,
            stderr=f"No tests/ directory found in {work_dir}",
            failures=[TestFailure(name="lesson_dir", diagnosis="runtime_error",
                                  error=f"No tests/ directory in {work_dir}")],
        )

    # Find entry file
    entry_files = sorted(
        p for p in work_dir.glob("*.py")
        if p.parent == work_dir and not p.name.startswith(".")
    )
    entry_file = entry_files[0].name if entry_files else ""
    entry_path = work_dir / entry_file if entry_file else None

    if student_file:
        student_path = Path(student_file).resolve()
        if not student_path.is_file():
            return _setup_failure_report(
                None,
                name="student submission",
                message=f"Student submission not found: {student_path}",
            )
        # Determine the target file — same as the entry file if one exists,
        # otherwise the student file's name in the lesson dir.
        target = work_dir / entry_file if entry_file else work_dir / student_path.name
        # Only copy if src and dst are different files
        if student_path.resolve() != target.resolve():
            shutil.copy2(str(student_path), str(target))
        entry_path = target
        entry_file = entry_path.name

    if not entry_path or not entry_path.is_file():
        return TestRunReport(
            lesson_id=work_dir.name,
            title=work_dir.name,
            passed=False,
            returncode=1,
            duration_seconds=0,
            tests_total=0,
            tests_passed=0,
            tests_failed=1,
            stderr=f"No Python entry file found in {work_dir}",
            failures=[TestFailure(name="entry_file", diagnosis="runtime_error",
                                  error=f"No .py entry file in {work_dir}")],
        )

    code_text = entry_path.read_text(encoding="utf-8") if entry_path else ""
    code_redacted = redact_text(code_text, strip_comments=True)

    # ── Sandbox layer 1: static AST allowlist ──────────────────────
    if enforce_ast:
        try:
            check_student_code(code_text, policy=policy)
        except SandboxViolation as violation:
            return _sandbox_violation_report(
                _virtual_manifest_for_dir(work_dir), violation, code_redacted
            )
        except SyntaxError:
            pass

    # ── Run pytest ──────────────────────────────────────────────
    # Grading happens in place, so the child's HOME/TMPDIR are redirected into a
    # throwaway scratch directory instead of the lesson folder itself.
    with tempfile.TemporaryDirectory(prefix="ducky_sandbox_") as scratch:
        env = build_sandbox_env(work_dir, scratch_dir=Path(scratch))
        bootstrap = write_bootstrap(Path(scratch), policy)
        junit_xml_path = work_dir / "_ducky_results.xml"
        junit_xml_path.unlink(missing_ok=True)
        test_snapshot_before = _snapshot_test_tree(tests_dir)
        command = _build_test_command("python -m pytest tests/", junit_xml_path)
        start = time.perf_counter()
        try:
            stdout, stderr, returncode = _run_command(
                [sys.executable, str(bootstrap), *command],
                cwd=work_dir,
                env=env,
                timeout=policy.wall_seconds,
                max_output_bytes=policy.max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr) + f"\nDuckyTimeoutError: test command exceeded {policy.wall_seconds}s"
            returncode = 124
        except OSError as exc:
            stdout = ""
            stderr = f"Could not start pytest: {exc}"
            returncode = 127
    duration = time.perf_counter() - start

    pytest_summary = _parse_pytest_junit(junit_xml_path)
    test_snapshot_after = _snapshot_test_tree(tests_dir)
    failures: list[TestFailure] = []

    # ── Parse failures ──────────────────────────────────────────
    if returncode != 0 and pytest_summary and pytest_summary.failures:
        for case in pytest_summary.failures:
            exp, act = _parse_assertion_error(case.error)
            diagnosis = classify_failure(
                expected=exp,
                actual=act,
                stdout=stdout,
                error=case.error,
                raw_output=stdout + stderr,
            )
            failures.append(
                TestFailure(
                    name=case.name,
                    diagnosis=diagnosis,
                    expected=exp,
                    actual=act,
                    stdout=redact_text(stdout),
                    error=redact_text(case.error),
                    raw_stdout=stdout,
                    raw_error=case.error,
                )
            )
    elif returncode != 0 and pytest_summary is None:
        diagnosis = classify_failure(error=stderr, raw_output=stdout + stderr)
        failures.append(
            TestFailure(
                name="test run",
                diagnosis=diagnosis,
                error=redact_text(stderr or stdout),
                raw_error=stderr or stdout,
            )
        )

    quality_gate_failed = False
    if pytest_summary is None:
        if returncode == 0:
            failures.append(_no_tests_failure("Pytest exited successfully without producing a JUnit test report."))
            quality_gate_failed = True
    elif pytest_summary.tests_total <= 0 or (pytest_summary.tests_passed + pytest_summary.tests_failed) <= 0:
        failures.append(_no_tests_failure("Pytest did not execute any tests; skipped-only or empty runs are not passing."))
        quality_gate_failed = True

    if test_snapshot_before != test_snapshot_after:
        failures.append(
            TestFailure(
                name="test suite",
                diagnosis="test_tampering",
                error="Student code changed files under tests/ while the grader was running.",
                raw_error="Student code changed files under tests/ while the grader was running.",
            )
        )
        quality_gate_failed = True

    # ── LLM hints ───────────────────────────────────────────────
    if failures:
        function_mismatches = _enhance_failures_with_function_detection(
            failures,
            student_code=code_text,
            work_dir=work_dir,
        )
        _attach_llm_hints(
            failures,
            manifest=DuckyManifest(
                lesson_id=_slugify_dirname(work_dir.name),
                title=work_dir.name,
                entry_file=entry_file,
            ),
            code_redacted=code_redacted,
            stdout=stdout,
            stderr=stderr,
            tutor_engine=tutor_engine,
            test_cases=_read_test_files(work_dir / "tests"),
            student_code=code_text,
            attempt_number=attempt_number,
            function_mismatches=function_mismatches,
        )

    # ── Totals ──────────────────────────────────────────────────
    if pytest_summary:
        total = pytest_summary.tests_total
        failed = pytest_summary.tests_failed
        passed_count = pytest_summary.tests_passed
    else:
        total = len(failures)
        failed = len(failures)
        passed_count = 0

    if quality_gate_failed:
        failed = max(failed, 1)
        passed_count = max(total - failed - (pytest_summary.tests_skipped if pytest_summary else 0), 0)

    # Clean up temp junit file
    try:
        junit_xml_path.unlink(missing_ok=True)
    except OSError:
        pass

    return TestRunReport(
        lesson_id=work_dir.name,
        title=work_dir.name,
        passed=returncode == 0 and not quality_gate_failed,
        returncode=returncode,
        duration_seconds=duration,
        tests_total=total,
        tests_passed=passed_count,
        tests_failed=failed,
        stdout=redact_text(stdout),
        stderr=redact_text(stderr),
        failures=failures,
        code_redacted=code_redacted,
        raw_stdout=stdout,
        raw_stderr=stderr,
        raw_code=code_text,
    )


def _slugify_dirname(name: str) -> str:
    """Convert a directory name to a lesson-id-safe slug."""
    import re
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    return slug.strip("_") or name
