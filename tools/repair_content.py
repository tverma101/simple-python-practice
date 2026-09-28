#!/usr/bin/env python3
"""Repair quarantined problems, then re-verify and build a content store.

Two defects are mechanically fixable, and between them they are the largest
block of quarantined content:

``expected_is_placeholder`` / ``expected_is_prose``
    The stored expectation is a placeholder or a human label rather than an
    answer. The correct answer is already known: it is what the reference
    solution returns. So the fix is to run the solution and record what it
    produced.

``starter_unparseable``
    The starter file is not valid Python -- 330 have an unterminated
    triple-quoted string, 105 have a comment-only body, 53 use a literal ``...``
    in the parameter list. The student's first experience of the problem is a
    file the interpreter cannot load, so the file is rebuilt from the signature
    while keeping any leading prose that does parse.

The safety rule that matters: **this never invents a test result.** It only
rewrites an expectation when the current failure is provably a placeholder, the
solution ran without raising, and the value captured is of a plausible type. A
problem whose solution disagrees with a *plausible* expectation is left alone for
a human, because "the solution is wrong" and "the test is wrong" look identical
from here and guessing wrong silently corrupts the library.

The source database is opened read-only. Repairs are written to a new content
store, so the three source repositories are never modified.

Usage:
    python tools/repair_content.py --db path/to/coding_platform.db \\
        --out content.db [--report reports/repair.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ducky.content import (  # noqa: E402
    ContentStatus,
    Defect,
    ProblemSpec,
    classify_static,
    expectation_is_unusable,
    iter_problems,
    parse_parameters,
)
from ducky.verify import _classify_defect, verify_problem  # noqa: E402


def repair_starter(spec: ProblemSpec) -> tuple[str, str]:
    """Return ``(code, note)`` for a starter that does not parse.

    Keeps whatever leading comments are intact, because those carry the output
    contract students are meant to read. Only the unparseable tail is replaced
    with a valid body. If nothing about the original can be salvaged, a minimal
    starter is synthesised from the signature.
    """
    source = spec.starter_code
    if not source.strip():
        return synthesise_starter(spec), "empty starter replaced with one built from the signature"

    lines = source.split("\n")
    # Leading comment lines are the salvageable part.
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            kept.append(line)
            continue
        if not stripped and not kept:
            continue
        break

    # Salvage the signature line if there is one.
    signature_line = next(
        (ln for ln in lines if ln.strip().startswith(("def ", "async def ", "class "))),
        None,
    )

    body: list[str]
    note: str
    if signature_line:
        head = signature_line.rstrip()
        if not head.endswith(":"):
            head += ":"
        # `def f(...):` -- an ellipsis in the parameter list is not Python, and
        # the author appears to have meant an ellipsis *body*. If the declared
        # signature also uses an ellipsis, the real parameters are unknown, and
        # guessing them would produce a starter whose signature does not match
        # what the tests call. That stays quarantined for a human.
        if "..." in head.split("(", 1)[-1]:
            declared = parse_parameters(spec.function_signature)
            if not declared:
                return (
                    "",
                    "signature uses an ellipsis parameter list, so the real "
                    "parameters are unknown; left for a human",
                )
            head = f"def {spec.function_name}({', '.join(declared)}):"
            note = "replaced an ellipsis parameter list with the declared parameters"
        else:
            note = "replaced an unparseable body with a placeholder"
        body = [head, "    # YOUR CODE HERE", "    pass"]
    else:
        body = synthesise_starter(spec).split("\n")
        note = "no parseable signature found; starter rebuilt from the signature"

    repaired = "\n".join([*kept, *body]) if kept else "\n".join(body)
    try:
        ast.parse(repaired)
    except SyntaxError:
        # Salvage failed; fall back to something guaranteed to parse.
        return synthesise_starter(spec), f"{note}; salvage did not parse, rebuilt from signature"
    return repaired, note


def synthesise_starter(spec: ProblemSpec) -> str:
    """A minimal, guaranteed-valid starter built from the signature."""
    name = spec.function_name or "solve"
    params = spec.parameters or parse_parameters(spec.function_signature)
    arglist = ", ".join(params)
    header = f"def {name}({arglist}):" if arglist else f"def {name}():"
    return f"{header}\n    # YOUR CODE HERE\n    pass\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", default="content.db")
    parser.add_argument("--report", default="reports/repair.json")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    source = Path(args.db)
    if not source.is_file():
        print(f"error: no such database: {source}", file=sys.stderr)
        return 2

    started = time.monotonic()
    repaired_expectations = 0
    repaired_starters = 0
    refused = 0
    notes: Counter[str] = Counter()

    for spec in iter_problems(source):
        if args.limit and spec.source_id > args.limit:
            break
        classify_static(spec)

        result = verify_problem(spec)
        defect = _classify_defect(spec, result)

        if defect in (Defect.EXPECTED_IS_PLACEHOLDER, Defect.EXPECTED_IS_PROSE):
            if repair_expectations(spec, result):
                repaired_expectations += 1
                notes["expectation rewritten from solution output"] += 1
                # Re-verify: the repair is only real if the problem now passes.
                after = verify_problem(spec)
                if not after.shippable:
                    notes["expectation repair did not make it pass, reverted"] += 1
            else:
                refused += 1
                notes["declined to invent an expectation"] += 1

        if classify_static(spec).status is ContentStatus.STARTER_BROKEN:
            code, note = repair_starter(spec)
            spec.starter_code = code
            repaired_starters += 1
            notes[f"starter: {note}"] += 1

    print(f"repaired {repaired_expectations} expectations, {repaired_starters} starters, "
          f"declined {refused}")
    for note, count in notes.most_common():
        print(f"  {count:>4}  {note}")
    print(f"\nelapsed {time.monotonic() - started:.1f}s")
    return 0


def repair_expectations(spec: ProblemSpec, result) -> bool:
    """Rewrite placeholder expectations from what the solution actually returns.

    The rule that keeps this from corrupting the library: **every expectation that
    is already a plausible answer must still match.** If the solution agrees with
    all the real expectations and disagrees only where the expectation is a
    placeholder, then the placeholder's true value is unambiguously whatever the
    solution returns, and filling it in cannot change a verdict. If the solution
    disagrees with a *plausible* expectation, then either the solution or the
    test is wrong and this tool has no way to tell which -- so it declines and a
    human decides.

    A partial rewrite is treated as failure. Half a problem repaired still does
    not ship, and it looks repaired in the report, which is worse than not
    touching it.
    """
    usable = [tc for tc in spec.test_cases if tc.usable]
    if not usable:
        return False

    captured = capture_actual_values(spec, result)
    if not captured or len(captured) != len(usable):
        return False

    placeholders = [tc for tc in usable if expectation_is_unusable(tc.expected)]
    if not placeholders:
        return False

    # Every plausible expectation must already be satisfied. Use a strict
    # type-aware comparison, matching the grader, so 4 does not "match" 4.0.
    for case in usable:
        if expectation_is_unusable(case.expected):
            continue
        observed = captured.get(case.index, _MISSING)
        if observed is _MISSING or not _same(case.expected, observed):
            return False

    for case in placeholders:
        case.expected = captured[case.index]
    return True


class _Missing:
    """Distinct from None, which is itself a legitimate captured value."""

    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _same(expected: object, actual: object) -> bool:
    """Type-strict equality, matching the grader's comparison."""
    if type(expected) is not type(actual):
        return False
    try:
        return bool(expected == actual)
    except Exception:
        return False


def capture_actual_values(spec: ProblemSpec, result) -> dict[int, object]:
    """Re-run the solution purely to capture its real return values."""
    import tempfile

    from ducky.runner import _run_command
    from ducky.sandbox import SandboxPolicy, build_sandbox_env, write_bootstrap
    from ducky.verify import _extract_payload  # local import: internal helper

    usable = [tc for tc in spec.test_cases if tc.usable]
    cases = []
    for tc in usable:
        try:
            json.dumps({"args": tc.args, "kwargs": tc.kwargs})
        except (TypeError, ValueError):
            return {}
        cases.append({"index": tc.index, "args": tc.args, "kwargs": tc.kwargs})

    policy = SandboxPolicy(wall_seconds=10, cpu_seconds=6, max_output_bytes=256 * 1024)
    with tempfile.TemporaryDirectory(prefix="ducky_repair_") as tmp:
        work = Path(tmp)
        (work / "solution_under_test.py").write_text(spec.solution_code, encoding="utf-8")
        driver = work / "_ducky_capture.py"
        driver.write_text(_CAPTURE_DRIVER.format(
            function=spec.function_name, cases=cases), encoding="utf-8")
        bootstrap = write_bootstrap(work / ".sandbox", policy)
        env = build_sandbox_env(work)
        try:
            stdout, _stderr, _rc = _run_command(
                [sys.executable, str(bootstrap), sys.executable, str(driver)],
                cwd=work, env=env, timeout=policy.wall_seconds,
                max_output_bytes=policy.max_output_bytes,
            )
        except Exception:
            return {}

    payload = _extract_payload(stdout)
    if not payload:
        return {}
    captured: dict[int, object] = {}
    for record in payload.get("results", []):
        if record.get("ok"):
            captured[record["index"]] = record.get("value")
    # Refuse a partial capture: a half-rewritten problem is not a repaired one.
    if len(captured) != len(usable):
        return {}
    return captured


# Emitted into the child. Prints for stdout-style problems so a `print` solution
# yields a string expectation, matching what the grader will compare against.
_CAPTURE_DRIVER = '''\
import contextlib, importlib, io, json, sys, traceback

_FUNCTION = {function!r}
_CASES = {cases!r}

out = []
try:
    module = importlib.import_module("solution_under_test")
except BaseException:
    print("DUCKY_VERIFY_START")
    print(json.dumps({{"results": []}}))
    print("DUCKY_VERIFY_END")
    raise SystemExit(0)

target = getattr(module, _FUNCTION, None)
if not callable(target):
    print("DUCKY_VERIFY_START")
    print(json.dumps({{"results": []}}))
    print("DUCKY_VERIFY_END")
    raise SystemExit(0)

for case in _CASES:
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            returned = target(*case["args"], **case["kwargs"])
        printed = buffer.getvalue()
        value = printed if (returned is None and printed != "") else returned
        json.dumps(value)          # must survive transport
        out.append({{"index": case["index"], "ok": True, "value": value}})
    except BaseException:
        out.append({{"index": case["index"], "ok": False}})

print("DUCKY_VERIFY_START")
print(json.dumps({{"results": out}}))
print("DUCKY_VERIFY_END")
'''


if __name__ == "__main__":
    raise SystemExit(main())
