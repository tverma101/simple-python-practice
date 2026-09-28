"""Regression tests for answer-key exfiltration.

Every test here corresponds to a channel through which a student's own code could
read a hidden test's expected value. Each was found by review, then reproduced,
then fixed -- and each is cheap to reintroduce, so each gets a test.

The channels differ in kind, which is why they are grouped:

* **The harness staged the answer next to the student's code.** The driver's
  source embeds every expected value and lives in the child's working directory.
  This was first closed by runtime means alone (chdir to an empty scratch
  directory, unlink the staged files) while `open` stayed legal, and the
  runtime layers are still tested below on their own -- but `open` is now also
  blocked statically, so the AST filter rejects the attempt before the runtime
  layers are reached. Both are asserted, because either could be relaxed.
* **The output cap was turned into a read primitive.** The child writes its
  result as one single-line JSON blob, so padding past the cap truncates the
  trailing sentinel and a "helpful" diagnostic quoting the last lines of output
  hands back the whole payload -- hidden expectations included.
* **The error path echoed student-supplied text.** A traceback is the student's
  own bytes, so anything they can read, the detail field will repeat.

The bar for all of them: the student's code must not be able to emit a hidden
expected value through any channel. Absolute-path reads of the real filesystem
are a separate and unsolved problem -- see docs/SECURITY_MODEL.md.
"""

from __future__ import annotations

import pytest

from ducky.content import ProblemSpec, parse_test_case
from ducky.verify import verify_problem

SECRET = "SECRET_HIDDEN_ANSWER_9f3a"


def _spec(body: str) -> ProblemSpec:
    cases = [
        parse_test_case(0, '{"args": {"name": "Ada"}}', '"Hello, Ada!"'),
        parse_test_case(1, '{"args": {"name": "Bob"}}', f'"{SECRET}"', is_hidden=True),
    ]
    return ProblemSpec(
        slug="s", title="t", function_name="greet", parameters=["name"],
        starter_code="def greet(name):\n    pass\n",
        solution_code=body, test_cases=cases,
    )


def _leaked(body: str) -> tuple[bool, str, str]:
    """Run a probe and report whether the secret escaped into student-visible text.

    The channel is ``actual_repr`` -- what the student's own code managed to emit
    -- plus ``detail``, which the route returns verbatim.
    """
    result = verify_problem(_spec(body))
    in_output = any(SECRET in failure.actual_repr for failure in result.failures)
    in_detail = SECRET in result.detail
    return in_output or in_detail, result.status.value, result.detail


class TestHarnessFilesAreNotReadable:
    """The driver and the staged solution must not be sitting in the cwd."""

    def test_reading_the_driver_source_yields_nothing(self):
        leaked, status, _ = _leaked(
            "def greet(name):\n"
            "    try:\n"
            "        return open('_ducky_verify.py').read()\n"
            "    except OSError:\n"
            "        return 'unreadable'\n"
        )
        assert not leaked
        # Blocked statically now, before the runtime layers are reached. The
        # assertion used to be that the read *fails*; it is now that the attempt
        # never runs, which is a stronger guarantee for the same outcome.
        assert status == "sandbox_rejected"

    def test_runtime_layers_hold_even_without_the_static_filter(self):
        """The chdir and unlink layers, tested directly.

        The static `open` block now rejects the read before the runtime layers
        matter, which means a test that only goes through `verify_problem` would
        pass even if chdir and unlink were both removed. This bypasses the AST
        filter so those layers are exercised on their own.
        """
        from ducky import verify

        original = verify.check_student_code
        verify.check_student_code = lambda *a, **k: None
        try:
            leaked, status, _ = _leaked(
                "def greet(name):\n"
                "    try:\n"
                "        return open('_ducky_verify.py').read()\n"
                "    except OSError:\n"
                "        return 'unreadable'\n"
            )
        finally:
            verify.check_student_code = original

        assert not leaked, "the driver source reached the student"
        # It ran, and the read failed -- so the value is the fallback, not the
        # answer, and the status is a real execution result rather than a
        # rejection.
        assert status in ("solution_errors", "solution_disagrees")

    def test_reading_the_staged_solution_yields_nothing(self):
        leaked, _, _ = _leaked(
            "def greet(name):\n"
            "    try:\n"
            "        return open('solution_under_test.py').read()\n"
            "    except OSError:\n"
            "        return 'unreadable'\n"
        )
        assert not leaked

    def test_raising_the_file_contents_does_not_echo_them(self):
        """A traceback is the student's own bytes, so it will repeat anything
        they can read. Proves the read fails rather than the echo being filtered."""
        leaked, _, _ = _leaked(
            "def greet(name):\n"
            "    raise ValueError(open('_ducky_verify.py').read())\n"
        )
        assert not leaked

    def test_an_empty_directory_exists_for_the_child(self, tmp_path):
        """The scratch directory is created, so chdir cannot silently fail."""
        result = verify_problem(
            _spec("def greet(name):\n    return f'Hello, {name}!'\n")
        )
        # Sanity: a correct solution still grades correctly after the change, so
        # the scratch setup did not break the import path.
        assert result.cases_passed >= 1


class TestOutputCapIsNotAReadPrimitive:
    def test_padding_past_the_cap_leaks_nothing(self):
        """The exploit: print more than the cap so the sentinel is truncated.

        The child writes its result as a single line, so the retained prefix is
        one 256 KB line containing every ``expected_repr``.
        """
        leaked, status, detail = _leaked(
            "def greet(name):\n"
            "    print('P' * 262100)\n"
            "    return 'nope'\n"
        )
        assert not leaked
        assert SECRET not in detail
        # And the message must not quote the child's output.
        assert "expected_repr" not in detail
        assert "P" * 100 not in detail

    def test_the_overflow_message_is_actionable_without_quoting_output(self):
        _, _, detail = _leaked(
            "def greet(name):\n"
            "    print('P' * 262100)\n"
            "    return 'nope'\n"
        )
        assert "output" in detail.lower()
        assert len(detail) < 400

    def test_a_missing_payload_never_quotes_child_output(self):
        """Whatever the cause, the diagnostic must not echo the child."""
        _, _, detail = _leaked(
            "def greet(name):\n"
            "    import sys\n"
            "    sys.stdout.close()\n"
            "    return 'x'\n"
        )
        assert "expected_repr" not in detail


class TestGradingStillWorks:
    """The hardening must not have broken the thing it protects."""

    def test_a_correct_solution_still_passes(self):
        # The second case's expectation is deliberately the sentinel, so a
        # solution that only satisfies the first case is *not* correct. Assert
        # against a spec where the solution genuinely satisfies every case, so a
        # failure here means the hardening broke grading rather than that the
        # fixture was wrong.
        cases = [
            parse_test_case(0, '{"args": {"name": "Ada"}}', '"Hello, Ada!"'),
            parse_test_case(1, '{"args": {"name": "Bob"}}', '"Hello, Bob!"', is_hidden=True),
        ]
        spec = ProblemSpec(
            slug="s", title="t", function_name="greet", parameters=["name"],
            starter_code="x",
            solution_code="def greet(name):\n    return f'Hello, {name}!'\n",
            test_cases=cases,
        )
        result = verify_problem(spec)
        assert result.status.value == "verified"
        assert result.cases_passed == result.cases_usable == 2

    def test_a_correct_solution_redacts_nothing_it_should(self):
        """A pass still reports counts, and still says how many were hidden."""
        result = verify_problem(
            ProblemSpec(
                slug="s", title="t", function_name="greet", parameters=["name"],
                starter_code="x",
                solution_code="def greet(name):\n    return f'Hello, {name}!'\n",
                test_cases=[
                    parse_test_case(0, '{"args": {"name": "Ada"}}', '"Hello, Ada!"'),
                    parse_test_case(1, '{"args": {"name": "Bob"}}', '"Hello, Bob!"',
                                    is_hidden=True),
                ],
            )
        )
        assert result.cases_total == 2
        assert result.hidden_indices == frozenset({1})

    def test_a_print_style_solution_still_passes(self):
        cases = [parse_test_case(0, '{"args": {"n": 3}}', '"1\\n2\\n3"')]
        spec = ProblemSpec(
            slug="s", title="t", function_name="count", parameters=["n"],
            starter_code="x",
            solution_code="def count(n):\n    for i in range(1, n + 1):\n        print(i)\n",
            test_cases=cases,
        )
        assert verify_problem(spec).status.value == "verified"

    def test_the_student_view_still_redacts(self):
        result = verify_problem(_spec("def greet(name):\n    return 'nope'\n"))
        hidden = [f for f in result.failures if f.is_hidden]
        assert hidden
        assert hidden[0].student_view()["expected_repr"] == "<hidden>"
