"""Execute a problem's reference solution against its own test cases.

This is the gate that turns "5,132 problems imported" into "5,092 problems known
to work". Importing content proves only that rows were readable. The only
evidence that a problem is shippable is that its reference solution, run against
its own expected outputs, agrees with them -- because a student who submits the
correct answer must not be marked wrong.

Every execution happens inside the same sandbox the grader uses
(:mod:`ducky.sandbox`), so verification cannot be more dangerous than grading.
That is deliberate: a verifier that runs untrusted code more safely than the
grader would be a strange place to draw the line.

Two output conventions are supported, because the upstream library uses both and
guessing wrong would fail correct solutions:

* **Return-value problems.** The function returns the answer. Compared with
  type-strict equality, so ``80`` does not satisfy ``80.0`` and ``[1, 2]`` does
  not satisfy ``(1, 2)``.
* **Print problems.** The signature is annotated ``-> None`` and the answer comes
  out on stdout. Compared as text.

The two are told apart by what the function actually did, not by the annotation
alone, because a ``-> None`` annotation with a real return value is a mistake in
the *content* and should be reported as one.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .content import ContentStatus, Defect, ProblemSpec
from .runner import _run_command
from .sandbox import (
    SandboxPolicy,
    SandboxViolation,
    build_sandbox_env,
    check_student_code,
    write_bootstrap,
)

__all__ = [
    "VerificationResult",
    "verify_problem",
    "verify_all",
    "VERIFICATION_POOL_SIZE",
]

#: One subprocess per problem, so a pathological solution is bounded by the
#: sandbox rather than by the pool.
VERIFICATION_POOL_SIZE = 8

#: Kept short: these are reference solutions, not student attempts, so they
#: should finish in milliseconds. A solution that needs longer is broken, and we
#: want to find that out quickly across thousands of problems.
_VERIFY_POLICY = SandboxPolicy(
    wall_seconds=10,
    cpu_seconds=6,
    max_output_bytes=256 * 1024,
)


@dataclass
class CaseResult:
    index: int
    passed: bool
    reason: str = ""
    expected_repr: str = ""
    actual_repr: str = ""
    #: True when this case is hidden from students. A hidden case's expected
    #: value must never be serialised into anything a student can read.
    is_hidden: bool = False

    def student_view(self) -> dict[str, Any]:
        """What a student is allowed to see about this case.

        A hidden case that fails must not reveal its expected output. The other
        codebase did leak it: the grader set ``expected`` on every result
        including hidden ones, and the UI printed it unconditionally, so failing
        one of the 23,349 hidden tests handed over the answer. The value is only
        useful for diagnosing the *reference* solution, which is a teacher-side
        concern, so it is redacted here rather than at the view layer where a
        future route could forget.
        """
        data: dict[str, Any] = {
            "index": self.index,
            "passed": self.passed,
            "reason": self.reason,
            "is_hidden": self.is_hidden,
        }
        if self.is_hidden:
            # Enough to act on, not enough to cheat from.
            data["expected_repr"] = "<hidden>"
        else:
            data["expected_repr"] = self.expected_repr
        data["actual_repr"] = self.actual_repr
        return data


@dataclass
class VerificationResult:
    slug: str
    source_id: int = 0
    status: ContentStatus = ContentStatus.UNVERIFIED
    detail: str = ""
    #: What would have to change for this to ship. Orthogonal to ``status``.
    defect: Defect = Defect.NONE
    cases_total: int = 0
    cases_usable: int = 0
    cases_passed: int = 0
    failures: list[CaseResult] = field(default_factory=list)
    #: True when the reference solution could not even be imported.
    solution_broken: bool = False
    #: Test-case indices that are hidden from students. Held per run rather than
    #: in a module global, because verify_all runs a thread pool and shared
    #: mutable state there corrupts concurrent results.
    hidden_indices: frozenset[int] = frozenset()

    @property
    def shippable(self) -> bool:
        return (
            self.status is ContentStatus.VERIFIED
            and self.cases_usable > 0
            and self.cases_passed == self.cases_usable
        )

    def to_dict(self, *, student: bool = False) -> dict[str, Any]:
        """Serialise this result.

        ``student=True`` redacts the expected value of every hidden case. The
        default is the teacher/debug view, which is what the verification report
        needs and what it has always used.
        """
        if student:
            failures = [f.student_view() for f in self.failures[:10]]
        else:
            failures = [
                {
                    "index": f.index,
                    "reason": f.reason,
                    "is_hidden": f.is_hidden,
                    "expected": f.expected_repr[:200],
                    "actual": f.actual_repr[:200],
                }
                for f in self.failures[:10]
            ]
        return {
            "slug": self.slug,
            "source_id": self.source_id,
            "status": self.status.value,
            "defect": self.defect.value,
            "detail": self.detail,
            "cases_total": self.cases_total,
            "cases_usable": self.cases_usable,
            "cases_passed": self.cases_passed,
            "solution_broken": self.solution_broken,
            "shippable": self.shippable,
            "failures": failures,
        }


# ── The child driver ─────────────────────────────────────────────────
#
# Generated rather than imported so that the solution is the only thing in
# sys.path, module-level state cannot leak between cases, and a solution that
# crashes the interpreter takes down a disposable process instead of the verifier.

_DRIVER = '''\
import contextlib
import importlib
import io
import json
import os
import sys
import traceback

from ducky.quiz import _normalize_output

_MODULE = {module!r}
_FUNCTION = {function!r}
_CASES = {cases!r}


def _hide_our_own_files():
    """Remove the answer key from the filesystem before student code runs.

    This script's *source* contains every test case's expected value, and it
    lives in the child's working directory. `open` is deliberately not blocked by
    the static filter -- it is taught -- so `open('_ducky_verify.py').read()`
    would otherwise hand back the complete answer key, hidden cases included.

    Two independent measures, because either alone leaves a gap:
      1. unlink this file. Python has already compiled it, so the running process
         is unaffected, and the path no longer resolves.
      2. chdir to an empty directory, so a relative `open()` -- of this file, of
         the solution, of anything else the harness staged -- finds nothing.

    This is defence in depth, not a boundary. It stops the *casual* read of a
    file that happens to be lying next to the student's code. It does not stop a
    determined submission, which has no `os` but does have `open` and an absolute
    path, and there is no filesystem sandbox on macOS. See SECURITY_MODEL.md.
    """
    try:
        os.chdir({scratch!r})
    except OSError:
        pass
    for name in (__file__, _MODULE + ".py"):
        try:
            os.unlink(name)
        except OSError:
            pass


def _describe(value):
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"


def _compare(expected, actual):
    """Type-strict equality, matching the grader's probe.

    Deliberately stricter than ``==``: ``80 == 80.0`` and ``[1, 2] == (1, 2)`` are
    both True in Python, and a student who returns a float where an int was asked
    for has made a mistake worth surfacing rather than absorbing.
    """
    if type(expected) is not type(actual):
        return False, "type_mismatch"
    if expected != actual:
        return False, "value_mismatch"
    return True, ""


results = []
try:
    module = importlib.import_module(_MODULE)
except BaseException:
    print("DUCKY_VERIFY_START")
    print(json.dumps({{"import_error": traceback.format_exc()}}))
    print("DUCKY_VERIFY_END")
    raise SystemExit(0)

target = getattr(module, _FUNCTION, None)
if not callable(target):
    print("DUCKY_VERIFY_START")
    print(json.dumps({{"missing_function": True}}))
    print("DUCKY_VERIFY_END")
    raise SystemExit(0)

# Only now, with the module imported and the target resolved, is it safe to
# remove the harness files: student code has not run yet.
_hide_our_own_files()

for case in _CASES:
    record = {{"index": case["index"], "args": case["args"], "kwargs": case["kwargs"]}}
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            returned = target(*case["args"], **case["kwargs"])
        printed = buffer.getvalue()
        record["printed"] = printed
        record["returned_repr"] = _describe(returned)
        record["returned_type"] = type(returned).__name__
        if returned is None and printed != "":
            # Print-style problem: the answer is what came out. Compared with
            # Ducky's existing output normalizer rather than a fresh rule, so a
            # problem verified here is graded the same way it will be later --
            # a trailing newline must not decide whether a solution is correct.
            expected = case["expected"]
            if expected is None:
                # The upstream row recorded `null` for a function that prints.
                # The intent is unrecoverable, so this is not a failing test, it
                # is an absent one.
                record["passed"] = False
                record["reason"] = "expected_not_recorded"
                record["expected_repr"] = "None"
                record["actual_repr"] = _describe(printed)
            else:
                ok = _normalize_output(expected) == _normalize_output(printed)
                record["passed"] = ok
                record["expected_repr"] = _describe(expected)
                record["actual_repr"] = _describe(printed)
                if not ok:
                    record["reason"] = "stdout_mismatch"
        else:
            ok, reason = _compare(case["expected"], returned)
            record["passed"] = ok
            record["expected_repr"] = _describe(case["expected"])
            record["actual_repr"] = _describe(returned)
            if not ok:
                record["reason"] = reason
    except BaseException:
        record["passed"] = False
        record["reason"] = "raised"
        record["error"] = traceback.format_exc()[-800:]
        record["expected_repr"] = _describe(case.get("expected"))
        record["actual_repr"] = "<exception>"
    results.append(record)

print("DUCKY_VERIFY_START")
print(json.dumps({{"results": results}}))
print("DUCKY_VERIFY_END")
'''


def _extract_payload(stdout: str) -> dict[str, Any] | None:
    """Pull the JSON out from between the sentinels.

    Sentinels rather than "last line of stdout" because a solution is free to
    print anything at all, including something that looks like our marker.
    """
    start = stdout.find("DUCKY_VERIFY_START")
    end = stdout.rfind("DUCKY_VERIFY_END")
    if start == -1 or end == -1:
        return None
    body = stdout[start + len("DUCKY_VERIFY_START") : end].strip()
    for line in reversed(body.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _top_level_functions(source: str) -> list[str]:
    """Names of functions defined at module level in ``source``.

    Used to tell "the solution is broken" apart from "the solution is fine but
    named differently", which have opposite fixes. Best-effort: source that does
    not parse yields an empty list rather than an exception.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _classify_defect(
    spec: ProblemSpec,
    result: VerificationResult,
) -> Defect:
    """Work out what a human would have to change.

    The important case is the one where a *correct* solution fails. Reporting
    that as a broken solution sends whoever fixes it in the wrong direction, and
    it is common: a placeholder test that feeds a string into a numeric parameter
    makes any correct implementation raise.
    """
    if result.status is ContentStatus.VERIFIED:
        return Defect.NONE
    if result.status is ContentStatus.SANDBOX_REJECTED:
        return Defect.BLOCKED_OPERATION
    if result.status is ContentStatus.NO_CALLABLE and "but the solution defines" in result.detail:
        return Defect.NAME_MISMATCH
    if result.status is ContentStatus.STARTER_BROKEN:
        return Defect.STARTER_UNPARSEABLE
    if result.status is ContentStatus.EXPECTED_NOT_RECORDED:
        return Defect.EXPECTED_IS_PLACEHOLDER
    if result.status in (ContentStatus.UNPARSEABLE, ContentStatus.NO_USABLE_TESTS):
        return Defect.ROW_UNPARSEABLE

    usable = [tc for tc in spec.test_cases if tc.usable]
    if not usable:
        return Defect.ROW_UNPARSEABLE

    # Is the expected value a placeholder, a label, or an echo of an input?
    # Delegates to the one shared judgement so this classifier and the repair
    # tool cannot drift apart -- when they did, the tool declined to fix problems
    # it existed to fix.
    from .content import expectation_defect

    for tc in usable:
        kind = expectation_defect(tc.expected)
        if kind is not None:
            return kind
        for value in list(tc.args) + list(tc.kwargs.values()):
            if isinstance(value, str) and tc.expected == value:
                return Defect.EXPECTED_IS_PLACEHOLDER

    # Does the solution raise while consuming an input of an obviously wrong kind?
    if result.status is ContentStatus.SOLUTION_ERRORS:
        annotations = (spec.function_signature or "").lower()
        numeric = any(
            hint in annotations
            for hint in (": int", ": float", "-> int", "-> float", ": str", "-> str")
        )
        for tc in usable:
            for value in list(tc.args) + list(tc.kwargs.values()):
                if isinstance(value, str) and numeric and not _looks_numeric(value):
                    return Defect.TEST_INPUT_WRONG_TYPE
        if result.solution_broken:
            return Defect.SOLUTION_BROKEN
        return Defect.TEST_INPUT_WRONG_TYPE

    if result.status is ContentStatus.SOLUTION_DISAGREES:
        return Defect.MISMATCH

    return Defect.UNKNOWN


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def verify_problem(
    spec: ProblemSpec,
    *,
    policy: SandboxPolicy = _VERIFY_POLICY,
) -> VerificationResult:
    """Run ``spec``'s reference solution against its own test cases.

    Applies the same static AST allowlist the grader does. This function executes
    arbitrary code, so it must not be a softer path than ``runner.run_pack`` --
    the web grading route calls straight into here, and before this check existed
    that route had no layer 1 at all while the CLI path did.
    """
    result = VerificationResult(slug=spec.slug, source_id=spec.source_id)
    try:
        if spec.solution_code.strip():
            try:
                check_student_code(spec.solution_code, policy=policy)
            except SyntaxError:
                # Not a policy problem. Let execution report it properly so the
                # diagnosis is a syntax error rather than a rejection.
                pass
            except SandboxViolation as violation:
                result.status = ContentStatus.SANDBOX_REJECTED
                result.detail = violation.message
                result.solution_broken = True
                return result
        return _verify_inner(spec, result, policy)
    finally:
        result.defect = _classify_defect(spec, result)


def _verify_inner(
    spec: ProblemSpec,
    result: VerificationResult,
    policy: SandboxPolicy,
) -> VerificationResult:
    usable = [tc for tc in spec.test_cases if tc.usable]
    result.cases_total = len(spec.test_cases)
    result.cases_usable = len(usable)
    result.hidden_indices = frozenset(tc.index for tc in usable if tc.is_hidden)

    if not usable:
        result.status = ContentStatus.NO_USABLE_TESTS
        result.detail = "no usable test cases to execute"
        return result
    if not spec.solution_code.strip():
        result.status = ContentStatus.NO_SOLUTION
        result.detail = "no reference solution"
        return result
    if not spec.function_name:
        result.status = ContentStatus.NO_CALLABLE
        result.detail = "no callable name in signature"
        return result

    # Expected values must survive the trip to JSON. Anything unserialisable here
    # would have crashed the child, so it is filtered with a reason rather than
    # trusted.
    cases: list[dict[str, Any]] = []
    skipped: list[str] = []
    for tc in usable:
        try:
            json.dumps({"args": tc.args, "kwargs": tc.kwargs, "expected": tc.expected})
        except (TypeError, ValueError) as exc:
            skipped.append(f"case {tc.index}: unserialisable ({exc})")
            continue
        cases.append({"index": tc.index, "args": tc.args, "kwargs": tc.kwargs, "expected": tc.expected})

    if not cases:
        result.status = ContentStatus.NO_USABLE_TESTS
        result.detail = skipped[0] if skipped else "all cases unserialisable"
        return result

    with tempfile.TemporaryDirectory(prefix="ducky_verify_") as tmp:
        work = Path(tmp)
        module_name = "solution_under_test"
        (work / f"{module_name}.py").write_text(spec.solution_code, encoding="utf-8")
        driver = work / "_ducky_verify.py"
        # An empty directory for the child to chdir into, so a relative open()
        # of a staged file finds nothing.
        scratch = work / "empty"
        scratch.mkdir(parents=True, exist_ok=True)
        driver.write_text(
            _DRIVER.format(
                module=module_name,
                function=spec.function_name,
                cases=cases,
                scratch=str(scratch),
            ),
            encoding="utf-8",
        )
        bootstrap = write_bootstrap(work / ".sandbox", policy)
        env = build_sandbox_env(work)

        try:
            stdout, stderr, returncode = _run_command(
                [sys.executable, str(bootstrap), sys.executable, str(driver)],
                cwd=work,
                env=env,
                timeout=policy.wall_seconds,
                max_output_bytes=policy.max_output_bytes,
            )
        except Exception as exc:  # timeout, OSError, anything the harness raises
            result.status = ContentStatus.SOLUTION_ERRORS
            result.detail = f"verification run failed: {type(exc).__name__}: {exc}"
            return result

    payload = _extract_payload(stdout)
    if payload is None:
        # No diagnostics from the child may reach `detail`.
        #
        # The child writes its whole result as ONE single-line JSON blob, so when
        # a submission prints more than the output cap, the trailing sentinel is
        # cut off and extraction fails. The obvious "helpful" thing -- quote the
        # last lines of output to explain why -- hands back 256 KB of a single
        # line containing every test case's `expected_repr`, hidden ones
        # included. A student could trigger it with nothing but
        # `print('P' * 262100)` and read the answer key.
        #
        # `detail` bypasses CaseResult.student_view(), so the redaction that
        # protects the normal path does not apply here. Say what happened and
        # nothing more.
        result.solution_broken = True
        result.status = ContentStatus.SOLUTION_ERRORS
        overflowed = "DuckyOutputLimitError" in (stderr or "") or "DuckyOutputLimitError" in (stdout or "")
        if overflowed:
            result.detail = (
                "The run produced more output than the grader can capture, so no "
                "result was recorded. Check for a loop that prints on every pass."
            )
        else:
            result.detail = (
                "The run ended without reporting a result. It may have exited early, "
                "been stopped for running too long, or printed more than the grader "
                "allows."
            )
        return result

    if payload.get("import_error"):
        result.solution_broken = True
        result.status = ContentStatus.SOLUTION_ERRORS
        last = [ln for ln in str(payload["import_error"]).strip().splitlines() if ln.strip()]
        result.detail = f"solution does not import: {last[-1] if last else 'unknown'}"
        return result

    if payload.get("missing_function"):
        result.solution_broken = True
        # The solution may be perfectly good under a different name. Distinguish
        # that from a genuinely broken solution, because the fix is completely
        # different: rename, versus rewrite.
        defined = _top_level_functions(spec.solution_code)
        if len(defined) == 1 and defined[0] != spec.function_name:
            result.status = ContentStatus.NO_CALLABLE
            result.detail = (
                f"signature declares {spec.function_name!r} but the solution "
                f"defines {defined[0]!r}"
            )
        elif defined:
            result.status = ContentStatus.NO_CALLABLE
            result.detail = (
                f"solution defines {defined} but not {spec.function_name!r}"
            )
        else:
            result.status = ContentStatus.NO_CALLABLE
            result.detail = f"solution defines no top-level function named {spec.function_name!r}"
        return result

    failures: list[CaseResult] = []
    for record in payload.get("results", []):
        if record.get("passed"):
            result.cases_passed += 1
        else:
            failures.append(
                CaseResult(
                    index=record.get("index", -1),
                    passed=False,
                    reason=record.get("reason", "unknown"),
                    expected_repr=record.get("expected_repr", ""),
                    actual_repr=record.get("returned_repr") or record.get("actual_repr", ""),
                    is_hidden=record.get("index") in result.hidden_indices,
                )
            )
    result.failures = failures

    if skipped:
        result.detail = f"skipped {len(skipped)} unserialisable case(s): {skipped[0]}"
        result.status = ContentStatus.NO_USABLE_TESTS
        return result

    if not failures:
        result.status = ContentStatus.VERIFIED
        result.detail = f"{result.cases_passed}/{result.cases_usable} cases pass"
        return result

    unrecorded = [f for f in failures if f.reason == "expected_not_recorded"]
    if unrecorded and len(unrecorded) == len(failures):
        result.status = ContentStatus.EXPECTED_NOT_RECORDED
        result.detail = (
            f"{len(failures)} case(s) record no expected output for a function that "
            "prints; the answer was never captured upstream"
        )
        return result

    raised = [f for f in failures if f.reason == "raised"]
    if len(raised) == len(failures):
        result.status = ContentStatus.SOLUTION_ERRORS
        result.detail = f"reference solution raised on all {len(failures)} case(s)"
    else:
        result.status = ContentStatus.SOLUTION_DISAGREES
        result.detail = (
            f"{len(failures)}/{result.cases_usable} case(s) disagree with the reference "
            f"solution (first: case {failures[0].index}, {failures[0].reason})"
        )
    return result


def verify_all(
    specs: list[ProblemSpec],
    *,
    pool_size: int = VERIFICATION_POOL_SIZE,
    progress: Any = None,
) -> list[VerificationResult]:
    """Verify many problems concurrently.

    Concurrency is across problems, never within one: each problem owns a single
    sandboxed subprocess, so the pool cannot be used to multiply a single
    problem's blast radius.
    """
    results: list[VerificationResult] = []
    if not specs:
        return results
    with ThreadPoolExecutor(max_workers=max(1, pool_size)) as pool:
        futures = {pool.submit(verify_problem, spec): spec for spec in specs}
        for done, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if progress is not None:
                progress(done, len(specs))
    results.sort(key=lambda r: r.source_id)
    return results
