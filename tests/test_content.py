"""Tests for content normalisation and execution-based verification.

The parsers get the most attention here because they sit on a trust boundary:
they decide what a student is shown and what they are graded against, and a
silent misparse turns into a problem that cannot be solved rather than an error
someone notices.
"""

from __future__ import annotations

import pytest

from ducky.content import (
    ContentStatus,
    Defect,
    ProblemSpec,
    TestCase,
    classify_static,
    is_shippable,
    parse_function_name,
    parse_parameters,
    parse_test_case,
)
from ducky.verify import VerificationResult, verify_problem

# ── Signature parsing ────────────────────────────────────────────────


class TestSignatureParsing:
    @pytest.mark.parametrize(
        ("signature", "expected"),
        [
            ("def two_sum(nums, k):", "two_sum"),
            ("two_sum(nums, k)", "two_sum"),
            ("  def  spaced (a, b):", "spaced"),
            ("def with_types(a: int, b: str) -> bool:", "with_types"),
            ("def __init__(self, nums):", "__init__"),
            ("", ""),
            ("not a signature at all", ""),
            ("1234", ""),
        ],
    )
    def test_function_name(self, signature, expected):
        assert parse_function_name(signature) == expected

    @pytest.mark.parametrize(
        ("signature", "expected"),
        [
            ("def two_sum(nums, k):", ["nums", "k"]),
            ("two_sum(nums, k)", ["nums", "k"]),
            ("def typed(a: int, b: str = 'x') -> bool:", ["a", "b"]),
            # `self` is an implementation detail of the method, not an argument
            # a caller passes.
            ("def method(self, a):", ["a"]),
            ("def posonly(a, /, b):", ["a", "b"]),
            ("def star(*args, **kwargs):", []),
        ],
    )
    def test_parameters(self, signature, expected):
        assert parse_parameters(signature) == expected

    def test_malformed_signature_yields_no_parameters_not_an_empty_list(self):
        """Conflating 'unknown' with 'takes no arguments' would misclassify rows."""
        assert parse_parameters("def broken(:") == []
        assert parse_parameters("") == []


# ── Test-case parsing ────────────────────────────────────────────────


class TestTestCaseParsing:
    def test_args_envelope_is_the_dominant_shape(self):
        tc = parse_test_case(0, '{"args": {"n": 3}}', "10")
        assert tc.kwargs == {"n": 3}
        assert tc.args == []
        assert tc.expected == 10
        assert tc.usable

    def test_args_envelope_with_positional_payload(self):
        tc = parse_test_case(0, '{"args": [1, 2, 3]}', "6")
        assert tc.args == [1, 2, 3]
        assert tc.expected == 6

    def test_json_array_is_positional(self):
        tc = parse_test_case(0, "[1,-2,3]", "[1, 3]")
        assert tc.args == [1, -2, 3]
        assert tc.expected == [1, 3]

    def test_comma_separated_literals(self):
        tc = parse_test_case(0, "18, True", "True")
        assert tc.args == [18, True]
        assert tc.expected is True

    def test_comma_split_respects_nesting(self):
        """A naive split(',') corrupts nested lists, which are common here."""
        tc = parse_test_case(0, "[1, 2], [3, 4]", "1")
        assert tc.args == [[1, 2], [3, 4]]

    def test_comma_split_respects_commas_inside_strings(self):
        tc = parse_test_case(0, '"a,b", 2', "1")
        assert tc.args == ["a,b", 2]

    def test_single_scalar(self):
        tc = parse_test_case(0, "100", '"A"')
        assert tc.args == [100]
        assert tc.expected == "A"

    def test_newlines_in_expected_string(self):
        tc = parse_test_case(0, '{"args": {"n": 3}}', '"1\\n2\\n3"')
        assert tc.expected == "1\n2\n3"

    def test_json_object_expected(self):
        tc = parse_test_case(0, "[-5,-10]", '{"positives": [], "count": 0}')
        assert tc.expected == {"positives": [], "count": 0}

    def test_bare_object_without_envelope_is_treated_as_kwargs(self):
        tc = parse_test_case(0, '{"a": 1, "b": 2}', "3")
        assert tc.kwargs == {"a": 1, "b": 2}

    def test_empty_input_is_not_usable(self):
        tc = parse_test_case(0, "", "5")
        assert not tc.usable
        assert "empty" in tc.parse_error

    def test_unparseable_expected_is_not_usable(self):
        tc = parse_test_case(0, '{"args": {"n": 1}}', "<not a literal>")
        assert not tc.usable
        assert "expected_output" in tc.parse_error

    def test_unparseable_json_object_is_reported(self):
        tc = parse_test_case(0, "{not json}", "5")
        assert not tc.usable
        assert tc.parse_error

    def test_envelope_with_wrong_payload_type(self):
        tc = parse_test_case(0, '{"args": 5}', "5")
        assert not tc.usable
        assert "want object or array" in tc.parse_error

    def test_hidden_flag_round_trips(self):
        assert parse_test_case(0, "1", "1", is_hidden=True).is_hidden

    def test_json_true_is_not_confused_with_python(self):
        tc = parse_test_case(0, '{"args": {"flag": true}}', "true")
        assert tc.kwargs == {"flag": True}
        assert tc.expected is True


# ── Static triage ────────────────────────────────────────────────────


def _spec(**kwargs) -> ProblemSpec:
    base = dict(
        slug="s",
        title="t",
        function_name="f",
        parameters=["a"],
        starter_code="def f(a):\n    return a\n",
        solution_code="def f(a):\n    return a\n",
        test_cases=[parse_test_case(0, '{"args": {"a": 1}}', "1")],
    )
    base.update(kwargs)
    return ProblemSpec(**base)


class TestStaticTriage:
    def test_clean_problem_is_unverified_not_verified(self):
        """Nothing may claim VERIFIED without executing something."""
        spec = classify_static(_spec())
        assert spec.status is ContentStatus.UNVERIFIED

    def test_missing_solution(self):
        spec = classify_static(_spec(solution_code=""))
        assert spec.status is ContentStatus.NO_SOLUTION

    def test_unparseable_starter(self):
        spec = classify_static(_spec(starter_code="def f(a:\n  return a\n"))
        assert spec.status is ContentStatus.STARTER_BROKEN

    def test_unterminated_triple_quote_starter(self):
        """330 real rows look like this; it must be caught, not tolerated."""
        starter = 'def f(a):\n    """Docstring that never closes\n    return a\n'
        spec = classify_static(_spec(starter_code=starter))
        assert spec.status is ContentStatus.STARTER_BROKEN

    def test_comment_only_body_starter(self):
        spec = classify_static(_spec(starter_code="def f(a):\n    # TODO\n"))
        assert spec.status is ContentStatus.STARTER_BROKEN

    def test_ellipsis_as_parameter_list_starter(self):
        spec = classify_static(_spec(starter_code="def f(...):\n    return 1\n"))
        assert spec.status is ContentStatus.STARTER_BROKEN

    def test_no_usable_tests(self):
        spec = classify_static(
            _spec(test_cases=[parse_test_case(0, "", "x"), parse_test_case(1, "", "y")])
        )
        assert spec.status is ContentStatus.NO_USABLE_TESTS

    def test_no_callable(self):
        spec = classify_static(_spec(function_name="", function_signature="???"))
        assert spec.status is ContentStatus.NO_CALLABLE

    def test_too_many_positional_args_for_declared_signature(self):
        spec = classify_static(
            _spec(parameters=["a"], test_cases=[parse_test_case(0, "[1, 2, 3]", "1")])
        )
        assert spec.status is ContentStatus.SIGNATURE_MISMATCH

    def test_unknown_keyword_is_a_mismatch(self):
        spec = classify_static(
            _spec(parameters=["a"], test_cases=[parse_test_case(0, '{"args": {"zzz": 1}}', "1")])
        )
        assert spec.status is ContentStatus.SIGNATURE_MISMATCH

    def test_kwarg_matching_declared_parameter_is_fine(self):
        spec = classify_static(
            _spec(parameters=["a"], test_cases=[parse_test_case(0, '{"args": {"a": 1}}', "1")])
        )
        assert spec.status is ContentStatus.UNVERIFIED

    def test_unparseable_row_never_ships(self):
        assert is_shippable(ContentStatus.UNPARSEABLE) is False

    def test_only_verified_ships(self):
        assert is_shippable(ContentStatus.VERIFIED) is True
        assert is_shippable(ContentStatus.UNVERIFIED) is False


# ── Execution ────────────────────────────────────────────────────────


def _runnable(code: str, cases: list[TestCase], name: str = "f") -> VerificationResult:
    return verify_problem(
        ProblemSpec(
            slug="s", title="t", function_name=name, parameters=[],
            starter_code=code, solution_code=code, test_cases=cases,
        )
    )


class TestVerification:
    def test_correct_solution_verifies(self):
        cases = [parse_test_case(0, '{"args": {"a": 2}}', "4"),
                 parse_test_case(1, '{"args": {"a": 5}}', "10")]
        result = _runnable("def f(a):\n    return a * 2\n", cases)
        assert result.status is ContentStatus.VERIFIED
        assert result.cases_passed == 2
        assert result.shippable
        assert result.defect is Defect.NONE

    def test_wrong_solution_does_not_verify(self):
        cases = [parse_test_case(0, '{"args": {"a": 2}}', "4")]
        result = _runnable("def f(a):\n    return a * 3\n", cases)
        assert result.status is ContentStatus.SOLUTION_DISAGREES
        assert not result.shippable

    def test_type_mismatch_is_not_equality(self):
        """80 == 80.0 in Python; a student returning the wrong type made a mistake."""
        cases = [parse_test_case(0, '{"args": {"a": 2}}', "4.0")]
        result = _runnable("def f(a):\n    return 4\n", cases)
        assert result.status is ContentStatus.SOLUTION_DISAGREES
        assert result.failures[0].reason == "type_mismatch"

    def test_int_and_list_substitution_rejected(self):
        cases = [parse_test_case(0, '{"args": {"a": 1}}', "[1, 2]")]
        result = _runnable("def f(a):\n    return (1, 2)\n", cases)
        assert result.failures[0].reason == "type_mismatch"

    def test_print_problem_compared_on_stdout(self):
        cases = [parse_test_case(0, '{"args": {"n": 3}}', '"1\\n2\\n3"')]
        result = _runnable("def f(n):\n    for i in range(1, n + 1):\n        print(i)\n", cases)
        assert result.status is ContentStatus.VERIFIED

    def test_trailing_newline_does_not_fail_a_print_problem(self):
        """print() adds one; the expected value never does. This is not a failure."""
        cases = [parse_test_case(0, '{"args": {"n": 2}}', '"1\\n2"')]
        result = _runnable("def f(n):\n    for i in range(1, n + 1):\n        print(i)\n", cases)
        assert result.status is ContentStatus.VERIFIED

    def test_null_expected_on_a_print_problem_is_an_absent_test(self):
        cases = [parse_test_case(0, '{"args": {"n": 2}}', "null")]
        result = _runnable("def f(n):\n    for i in range(1, n + 1):\n        print(i)\n", cases)
        assert result.status is ContentStatus.EXPECTED_NOT_RECORDED
        assert result.defect is Defect.EXPECTED_IS_PLACEHOLDER

    def test_solution_that_does_not_import(self):
        cases = [parse_test_case(0, '{"args": {"a": 1}}', "1")]
        result = _runnable("def f(a)\n    return a\n", cases)
        assert result.solution_broken
        assert not result.shippable

    def test_solution_defining_the_wrong_function(self):
        cases = [parse_test_case(0, '{"args": {"a": 1}}', "1")]
        result = _runnable("def other(a):\n    return a\n", cases)
        assert result.status is ContentStatus.NO_CALLABLE

    def test_raising_solution_is_reported(self):
        cases = [parse_test_case(0, '{"args": {"a": 1}}', "1")]
        result = _runnable("def f(a):\n    return a / 0\n", cases)
        assert result.status is ContentStatus.SOLUTION_ERRORS
        assert result.failures[0].reason == "raised"

    def test_infinite_loop_is_bounded_and_reported(self):
        cases = [parse_test_case(0, '{"args": {"a": 1}}', "1")]
        result = _runnable("def f(a):\n    while True:\n        pass\n", cases)
        assert not result.shippable
        assert result.status in (ContentStatus.SOLUTION_ERRORS, ContentStatus.SOLUTION_DISAGREES)

    def test_prose_expected_is_classified_as_prose(self):
        """`expected: "Zero"` is a label, not an answer -- name it as such."""
        cases = [parse_test_case(0, '{"args": {"n": 0}}', '"Zero"')]
        result = _runnable("def f(n):\n    return 0\n", cases)
        assert result.defect is Defect.EXPECTED_IS_PROSE

    def test_echoed_input_that_the_solution_returns_is_a_legitimate_pass(self):
        """Placeholder detection must not fire on a correct answer."""
        cases = [parse_test_case(0, '{"args": {"a": "test"}}', '"test"')]
        result = _runnable("def f(a):\n    return a\n", cases)
        assert result.status is ContentStatus.VERIFIED

    def test_echoed_input_on_a_failing_case_is_a_placeholder(self):
        cases = [parse_test_case(0, '{"args": {"a": "test"}}', '"test"')]
        result = _runnable("def f(a):\n    return 1\n", cases)
        assert not result.shippable
        assert result.defect is Defect.EXPECTED_IS_PLACEHOLDER

    def test_string_where_number_expected_is_test_data_not_a_broken_solution(self):
        """The single most important classification: 640 rows depend on it."""
        cases = [parse_test_case(0, '{"args": {"num": "test"}}', '"test"')]
        result = _runnable(
            "def f(num: int):\n    return abs(num)\n",
            cases,
            name="f",
        )
        assert result.defect in (Defect.EXPECTED_IS_PLACEHOLDER, Defect.TEST_INPUT_WRONG_TYPE)
        assert result.defect is not Defect.SOLUTION_BROKEN

    def test_all_cases_failing_to_verify_means_not_shippable(self):
        result = VerificationResult(slug="s", status=ContentStatus.VERIFIED, cases_usable=3, cases_passed=2)
        assert not result.shippable

    def test_result_serialises(self):
        import json

        result = _runnable("def f(a):\n    return a\n", [parse_test_case(0, '{"args": {"a": 1}}', "1")])
        json.dumps(result.to_dict())


class TestNameMismatch:
    """The signature and the solution drifting apart is a distinct, cheap fix."""

    def test_solution_under_a_different_name_is_reported_as_name_mismatch(self):
        cases = [parse_test_case(0, '{"args": {"a": "x"}}', "True")]
        result = _runnable("def other_name(a):\n    return a.isupper()\n", cases)
        assert result.status is ContentStatus.NO_CALLABLE
        assert "but the solution defines" in result.detail
        assert result.defect is Defect.NAME_MISMATCH

    def test_solution_defining_nothing_recognisable(self):
        cases = [parse_test_case(0, '{"args": {"a": 1}}', "1")]
        result = _runnable("x = 1 + 1\n", cases)
        assert result.status is ContentStatus.NO_CALLABLE
        assert result.defect is Defect.SOLUTION_BROKEN or result.defect is Defect.UNKNOWN


class TestHiddenTestRedaction:
    """A failed hidden test must never reveal its expected output.

    Regression test for a real leak found by audit: the other codebase set
    ``expected`` on every grader result including hidden ones, and the UI printed
    it unconditionally. With 23,349 hidden tests in that library, failing any one
    of them handed the student the answer. Enforced at the data layer rather than
    the view, because a future route that forgets to redact is exactly how it
    happened there.
    """

    def _spec(self, hidden_index: int = 1):
        cases = [
            parse_test_case(0, '{"args": {"a": 2}}', "4"),
            parse_test_case(1, '{"args": {"a": 3}}', "99", is_hidden=True),
        ]
        return ProblemSpec(
            slug="s", title="t", function_name="f", parameters=["a"],
            starter_code="def f(a):\n    return a\n",
            solution_code="def f(a):\n    return a\n",
            test_cases=cases,
        )

    def test_hidden_flag_survives_into_the_result(self):
        result = verify_problem(self._spec())
        hidden = [f for f in result.failures if f.is_hidden]
        assert len(hidden) == 1
        assert hidden[0].index == 1

    def test_student_view_redacts_a_hidden_expectation(self):
        result = verify_problem(self._spec())
        payload = result.to_dict(student=True)
        leaked = [f for f in payload["failures"] if f.get("expected") == "99"]
        assert not leaked, "hidden expected value reached the student payload"
        hidden = [f for f in payload["failures"] if f["is_hidden"]]
        assert hidden and hidden[0]["expected_repr"] == "<hidden>"

    def test_student_view_keeps_a_visible_expectation(self):
        """Redaction must not cost the student the feedback they need."""
        result = verify_problem(
            ProblemSpec(
                slug="s", title="t", function_name="f", parameters=["a"],
                starter_code="def f(a):\n    return a\n",
                solution_code="def f(a):\n    return a\n",
                test_cases=[parse_test_case(0, '{"args": {"a": 2}}', "4")],
            )
        )
        payload = result.to_dict(student=True)
        assert payload["failures"]
        assert payload["failures"][0]["expected_repr"] == "4"
        assert payload["failures"][0]["is_hidden"] is False

    def test_teacher_view_keeps_everything(self):
        """The verification report needs the real value to be useful."""
        result = verify_problem(self._spec())
        payload = result.to_dict()
        assert any(f.get("expected") == "99" for f in payload["failures"])

    def test_hidden_set_is_per_run_not_shared(self):
        """verify_all uses a thread pool; shared state there corrupts results."""
        a = verify_problem(self._spec(hidden_index=1))
        b = ProblemSpec(
            slug="s", title="t", function_name="f", parameters=["a"],
            starter_code="def f(a):\n    return a\n",
            solution_code="def f(a):\n    return a\n",
            test_cases=[parse_test_case(0, '{"args": {"a": 2}}', "4")],
        )
        result_b = verify_problem(b)
        assert 1 not in result_b.hidden_indices
        assert 1 in a.hidden_indices
