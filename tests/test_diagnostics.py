"""Tests for ducky.diagnostics — comprehensive edge cases for AI slop patterns.

This test suite targets the most common AI-generated code failures:
  1. Type mismatches (int/float/bool, str/None, nested types)
  2. Diagnosis ordering ambiguities (assertion vs runtime errors)
  3. Hint escalation quality across all diagnosis types
  4. Error parsing with malformed/unicode/exotic error messages
  5. Missing corner cases (empty, None, zero, NaN, negative)
  6. Combined failure modes (error + wrong output simultaneously)
  7. Context-aware hint builders with missing/incomplete context
"""

from __future__ import annotations

import unittest

from ducky.diagnostics import (
    _as_text,
    _build_function_not_found_hint,
    _build_incorrect_logic_hint,
    _build_param_count_hint,
    _parse_attribute_error,
    _parse_param_count_error,
    classify_failure,
    hint_for_diagnosis,
)


class AsTextTests(unittest.TestCase):
    """_as_text helper — the base utility other functions depend on."""

    def test_none_becomes_literal_none(self):
        self.assertEqual(_as_text(None), "None")

    def test_string_stripped(self):
        self.assertEqual(_as_text("  hello  "), "hello")

    def test_number_preserved(self):
        self.assertEqual(_as_text(42), "42")
        self.assertEqual(_as_text(0), "0")
        self.assertEqual(_as_text(-1), "-1")

    def test_float_preserved(self):
        self.assertEqual(_as_text(3.14), "3.14")

    def test_boolean_converted(self):
        self.assertEqual(_as_text(True), "True")
        self.assertEqual(_as_text(False), "False")

    def test_empty_string(self):
        self.assertEqual(_as_text(""), "")
        self.assertEqual(_as_text("  "), "")

    def test_list_represented(self):
        self.assertEqual(_as_text([1, 2, 3]), "[1, 2, 3]")

    def test_dict_represented(self):
        self.assertEqual(_as_text({"a": 1}), "{'a': 1}")


class ParseAttributeErrorTests(unittest.TestCase):
    """_parse_attribute_error edge cases."""

    def test_standard_message(self):
        module, attr = _parse_attribute_error("module 'solution' has no attribute 'calc_grade'")
        self.assertEqual(module, "solution")
        self.assertEqual(attr, "calc_grade")

    def test_double_quotes(self):
        module, attr = _parse_attribute_error('module "solution" has no attribute "calc_grade"')
        self.assertEqual(module, "solution")
        self.assertEqual(attr, "calc_grade")

    def test_no_match_returns_none(self):
        module, attr = _parse_attribute_error("Some random error message")
        self.assertIsNone(module)
        self.assertIsNone(attr)

    def test_empty_string(self):
        module, attr = _parse_attribute_error("")
        self.assertIsNone(module)
        self.assertIsNone(attr)

    def test_unicode_in_names(self):
        module, attr = _parse_attribute_error("module 'réponse' has no attribute 'calcülate'")
        self.assertEqual(module, "réponse")
        self.assertEqual(attr, "calcülate")

    def test_import_error_message(self):
        module, attr = _parse_attribute_error("ImportError: cannot import name 'calc_grade' from 'solution'")
        self.assertEqual(module, "solution")
        self.assertEqual(attr, "calc_grade")

    def test_partially_initialized_import_error(self):
        module, attr = _parse_attribute_error("ImportError: cannot import name 'calc_grade' from partially initialized module 'solution'")
        self.assertEqual(module, "solution")
        self.assertEqual(attr, "calc_grade")


class ClassifyFailureTests(unittest.TestCase):
    """Comprehensive classify_failure tests targeting AI slop patterns."""

    # ── Type Mismatch Edge Cases (AI slop pattern #1) ──────────────

    def test_int_vs_float_equal(self):
        """int 80 vs float 80.0 — AI often produces wrong type."""
        result = classify_failure(expected=80, actual=80.0)
        self.assertEqual(result, "wrong_type")

    def test_float_vs_int_equal(self):
        result = classify_failure(expected=80.0, actual=80)
        self.assertEqual(result, "wrong_type")

    def test_bool_vs_int_true(self):
        """True/False vs 1/0 — classic AI slop type confusion."""
        result = classify_failure(expected=1, actual=True)
        self.assertEqual(result, "wrong_type")

    def test_bool_vs_int_false(self):
        result = classify_failure(expected=0, actual=False)
        self.assertEqual(result, "wrong_type")

    def test_str_vs_int_equal_value(self):
        result = classify_failure(expected="0", actual=0)
        self.assertEqual(result, "wrong_type")

    def test_int_vs_str_equal_value(self):
        result = classify_failure(expected=42, actual="42")
        self.assertEqual(result, "wrong_type")

    def test_none_vs_empty_string(self):
        """None vs '' — AI slop pattern: returning None instead of ''."""
        result = classify_failure(expected="", actual=None)
        self.assertEqual(result, "none_returned")

    def test_none_vs_zero(self):
        result = classify_failure(expected=0, actual=None)
        self.assertEqual(result, "none_returned")

    def test_float_nan_comparison(self):
        """NaN != NaN — AI slop: floating point edge cases."""
        result = classify_failure(expected=float("nan"), actual=float("nan"))
        # NaN != NaN, so this should be a logic error
        self.assertIn(result, ("incorrect_logic", "none_returned", "wrong_type"))

    def test_negative_zero(self):
        """-0.0 vs 0.0 — subtle float edge case."""
        result = classify_failure(expected=0.0, actual=-0.0)
        # In Python -0.0 == 0.0, so this should pass type check and then logic
        self.assertIn(result, ("incorrect_logic",))

    def test_large_float_vs_int(self):
        """Large float/int values — potential precision issues."""
        result = classify_failure(expected=10000000000, actual=10000000000.0)
        self.assertEqual(result, "wrong_type")

    # ── Diagnosis Ordering (AI slop pattern: cascading checks) ─────

    def test_assertion_error_in_error_not_runtime(self):
        """AssertionError in error field should NOT be classified as runtime."""
        result = classify_failure(error="AssertionError: expected 5 got 3")
        self.assertNotEqual(result, "runtime_error")
        self.assertEqual(result, "assertion_failed")

    def test_assert_in_error_variable_not_assertion(self):
        """'assert' in error could be a variable name, not an actual assertion error.
        This tests the fragility of the 'assert ' check in classify_failure."""
        result = classify_failure(error='NameError: name "assert_valid" is not defined')
        # Should NOT be classified as assertion failure, should be undefined_variable
        self.assertEqual(result, "undefined_variable")

    def test_assertion_in_non_assertion_error(self):
        """Error message with 'assert' as prefix of a variable name — should NOT be assertion."""
        result = classify_failure(error="NameError: name 'assert_valid' is not defined")
        self.assertEqual(result, "undefined_variable")

    def test_typeerror_missing_required_keyword(self):
        """'missing' and 'required' and 'argument' in TypeError — should be wrong_parameter_count."""
        result = classify_failure(error="TypeError: my_func() missing 1 required positional argument: 'b'")
        self.assertEqual(result, "wrong_parameter_count")

    def test_typeerror_takes_positional_args(self):
        result = classify_failure(error="TypeError: my_func() takes 1 positional argument but 2 were given")
        self.assertEqual(result, "wrong_parameter_count")

    def test_typeerror_general_is_not_param_count(self):
        """General TypeError not about arguments should not be wrong_parameter_count."""
        result = classify_failure(error="TypeError: unsupported operand type(s) for +: 'int' and 'str'")
        self.assertNotEqual(result, "wrong_parameter_count")
        self.assertEqual(result, "type_error")

    def test_none_returned_vs_prints(self):
        """Both None and stdout — prints wins."""
        result = classify_failure(expected="hello", actual=None, stdout="hello\n")
        self.assertEqual(result, "prints_instead_of_returns")

    def test_none_returned_no_stdout(self):
        result = classify_failure(expected=5, actual=None, stdout="")
        self.assertEqual(result, "none_returned")

    def test_both_none_no_stdout(self):
        result = classify_failure(expected=None, actual=None, stdout="")
        self.assertEqual(result, "none_returned")

    def test_both_none_with_stdout(self):
        """Both expected and actual are None but stdout has output — should be prints."""
        result = classify_failure(expected=None, actual=None, stdout="42\n")
        # expected is None so _as_text(expected) == stdout.strip() won't match
        self.assertIn(result, ("none_returned", "prints_instead_of_returns"))

    # ── Error Text Parsing Edge Cases ──────────────────────────────

    def test_syntax_error_in_combined(self):
        """SyntaxError detected even if passed through raw_output."""
        result = classify_failure(raw_output="line 1\n  SyntaxError: invalid syntax")
        self.assertEqual(result, "syntax_error")

    def test_indentation_error(self):
        result = classify_failure(error="IndentationError: unexpected indent")
        self.assertEqual(result, "syntax_error")

    def test_import_error_cannot_import_name(self):
        result = classify_failure(error="ImportError: cannot import name 'add_numbers' from 'solution'")
        self.assertEqual(result, "function_not_found")

    def test_import_error_module_not_found(self):
        result = classify_failure(error="ModuleNotFoundError: No module named 'numpy'")
        self.assertEqual(result, "import_error")

    def test_general_import_error(self):
        result = classify_failure(error="ImportError: No module named 'requests'")
        self.assertEqual(result, "import_error")

    def test_attribute_error_no_attribute(self):
        result = classify_failure(error="AttributeError: module 'solution' has no attribute 'my_func'")
        self.assertEqual(result, "function_not_found")

    def test_attribute_error_general(self):
        """AttributeError about NoneType should be runtime, not function_not_found."""
        result = classify_failure(error="AttributeError: 'NoneType' object has no attribute 'append'")
        self.assertEqual(result, "attribute_error")

    def test_zerodivision_detected(self):
        result = classify_failure(error="ZeroDivisionError: division by zero")
        self.assertEqual(result, "division_by_zero")

    def test_index_error_detected(self):
        result = classify_failure(error="IndexError: list index out of range")
        self.assertEqual(result, "index_out_of_range")

    def test_key_error_detected(self):
        result = classify_failure(error="KeyError: 'missing_key'")
        self.assertEqual(result, "missing_key")

    def test_name_error_detected(self):
        result = classify_failure(error="NameError: name 'undefined_var' is not defined")
        self.assertEqual(result, "undefined_variable")

    def test_unrecognized_runtime_error(self):
        result = classify_failure(error="ValueError: invalid literal for int()")
        self.assertEqual(result, "value_error")

    def test_multiple_errors_combined(self):
        """Error should take precedence over wrong return."""
        result = classify_failure(
            expected=5,
            actual=3,
            error="ZeroDivisionError: division by zero",
        )
        self.assertEqual(result, "division_by_zero")

    def test_no_error_no_values(self):
        """No error, expected=None, actual=None — fall through to none_returned."""
        result = classify_failure(error="")
        self.assertEqual(result, "none_returned")

    def test_none_error_does_not_crash(self):
        """None error used to crash with TypeError — regression test."""
        # Should not crash
        result = classify_failure(error=None)
        self.assertIsInstance(result, str)

    # ── String Comparison Edge Cases ───────────────────────────────

    def test_whitespace_around_words(self):
        """Extra spaces inside string — different from leading/trailing."""
        result = classify_failure(expected="hello world", actual="hello  world")
        self.assertEqual(result, "extra_spaces")

    def test_trailing_whitespace(self):
        result = classify_failure(expected="hello", actual="hello   ")
        self.assertEqual(result, "extra_spaces")

    def test_leading_whitespace(self):
        result = classify_failure(expected="hello", actual="  hello")
        self.assertEqual(result, "extra_spaces")

    def test_no_extra_spaces_identical(self):
        """Identical strings with same spacing should not be extra_spaces."""
        result = classify_failure(expected="hello world", actual="hello world")
        self.assertEqual(result, "incorrect_logic")

    def test_case_mismatch_full(self):
        result = classify_failure(expected="HELLO", actual="hello")
        self.assertEqual(result, "case_mismatch")

    def test_case_mismatch_title(self):
        result = classify_failure(expected="Hello World", actual="hello world")
        self.assertEqual(result, "case_mismatch")

    def test_whitespace_and_case_both_different(self):
        """Both whitespace and case differ — either diagnosis is acceptable."""
        result = classify_failure(expected="Hello", actual="  hello  ")
        self.assertIn(result, ("extra_spaces", "case_mismatch", "incorrect_logic"))

    # ── List Comparison Edge Cases ─────────────────────────────────

    def test_list_order_wrong_simple(self):
        result = classify_failure(expected=[1, 2, 3], actual=[3, 2, 1])
        self.assertEqual(result, "list_order_wrong")

    def test_list_order_same_order(self):
        result = classify_failure(expected=[1, 2, 3], actual=[1, 2, 3])
        self.assertEqual(result, "incorrect_logic")

    def test_list_order_with_strings(self):
        result = classify_failure(expected=["a", "b"], actual=["b", "a"])
        self.assertEqual(result, "list_order_wrong")

    def test_list_order_wrong_with_duplicates(self):
        """Duplicates make order detection harder."""
        result = classify_failure(expected=[1, 1, 2], actual=[1, 2, 1])
        self.assertEqual(result, "list_order_wrong")

    def test_empty_lists(self):
        result = classify_failure(expected=[], actual=[])
        self.assertEqual(result, "incorrect_logic")

    def test_list_one_element(self):
        """Single-element lists — sorted comparison is trivial."""
        result = classify_failure(expected=[1], actual=[1])
        self.assertEqual(result, "incorrect_logic")

    def test_list_not_order_mismatch_different_elements(self):
        result = classify_failure(expected=[1, 2, 3], actual=[4, 5, 6])
        self.assertEqual(result, "incorrect_logic")

    def test_nested_lists_same_elements_different_order(self):
        """Nested lists with different order of stringified elements should be list_order_wrong."""
        result = classify_failure(expected=[[1, 2], [3]], actual=[[3], [1, 2]])
        self.assertEqual(result, "list_order_wrong")

    # ── Edge Cases with raw_output ─────────────────────────────────

    def test_syntax_in_raw_output_not_error(self):
        result = classify_failure(raw_output="SyntaxError: bad syntax", error="")
        self.assertEqual(result, "syntax_error")

    def test_raw_output_without_error(self):
        result = classify_failure(expected=5, actual=3, raw_output="some output")
        self.assertEqual(result, "incorrect_logic")

    # ── AI Slop: Plausible But Wrong (Pattern #1) ──────────────────

    def test_off_by_one_diagnosed_as_incorrect_logic(self):
        """Off-by-one errors should never be misdiagnosed as type issues."""
        result = classify_failure(expected=10, actual=9)
        self.assertEqual(result, "incorrect_logic")

    def test_sign_flip_diagnosed_correctly(self):
        result = classify_failure(expected=5, actual=-5)
        self.assertEqual(result, "incorrect_logic")

    def test_wrong_constant(self):
        result = classify_failure(expected=3.14159, actual=3.14)
        self.assertEqual(result, "incorrect_logic")

    # ── Boolean logic edge cases ───────────────────────────────────

    def test_bool_expected_actual_int(self):
        """Expected True/False but got 1/0 — subtle type mismatch."""
        result = classify_failure(expected=True, actual=1)
        self.assertEqual(result, "wrong_type")

    def test_bool_false_expected_actual_0(self):
        result = classify_failure(expected=False, actual=0)
        self.assertEqual(result, "wrong_type")

    # ── Zero/Empty/None edge cases ─────────────────────────────────

    def test_zero_expected_actual_zero(self):
        result = classify_failure(expected=0, actual=0)
        self.assertEqual(result, "incorrect_logic")

    def test_zero_expected_actual_none(self):
        result = classify_failure(expected=0, actual=None)
        self.assertEqual(result, "none_returned")

    def test_none_returned_with_logged_output(self):
        """Returning None when expected a value, but stdout has logged info (not the answer)."""
        result = classify_failure(
            expected=42,
            actual=None,
            stdout="DEBUG: starting computation\n",
        )
        self.assertEqual(result, "none_returned")

    def test_none_returned_without_any_output(self):
        result = classify_failure(expected=42, actual=None)
        self.assertEqual(result, "none_returned")

    # ── String edge cases ──────────────────────────────────────────

    def test_empty_string_vs_none(self):
        """Returning None instead of '' — common bug."""
        result = classify_failure(expected="", actual=None)
        self.assertEqual(result, "none_returned")

    def test_whitespace_only_string(self):
        """Whitespace string vs expected value."""
        result = classify_failure(expected="hello", actual="   ")
        self.assertEqual(result, "incorrect_logic")

    def test_multiline_strings(self):
        result = classify_failure(
            expected="line1\nline2",
            actual="line1\nline2\n",
        )
        self.assertEqual(result, "extra_spaces")

    def test_newline_differences(self):
        result = classify_failure(
            expected="hello\nworld",
            actual="hello\nworld\n",
        )
        self.assertEqual(result, "extra_spaces")

    # ── Execution path: error + actual values mixed ────────────────

    def test_combined_error_and_actual_values(self):
        """Both error and actual values provided — error should take priority."""
        result = classify_failure(
            expected=5,
            actual=3,
            error="IndexError: list index out of range",
        )
        self.assertEqual(result, "index_out_of_range")

    def test_unrelated_error_but_correct_values(self):
        """Error unrelated but values match — still reports error type."""
        result = classify_failure(
            expected=5,
            actual=5,
            error="RuntimeError: something went wrong",
        )
        self.assertEqual(result, "runtime_error")


class HintForDiagnosisTests(unittest.TestCase):
    """Comprehensive hint tests — targeting hint quality for AI slop detection."""

    ALL_DIAGNOSES = [
        "syntax_error",
        "function_not_found",
        "import_error",
        "wrong_parameter_count",
        "division_by_zero",
        "index_out_of_range",
        "missing_key",
        "undefined_variable",
        "runtime_error",
        "prints_instead_of_returns",
        "none_returned",
        "wrong_type",
        "extra_spaces",
        "case_mismatch",
        "list_order_wrong",
        "incorrect_logic",
    ]

    def test_hints_for_all_diagnoses_at_level_1(self):
        diagnoses = self.ALL_DIAGNOSES
        for d in diagnoses:
            hint = hint_for_diagnosis(d)
            self.assertIsInstance(hint, str, f"Hint for {d} should be a string")
            self.assertTrue(len(hint) > 10, f"Hint for {d} should be meaningful (got {len(hint)} chars)")

    def test_hints_for_all_diagnoses_at_level_2(self):
        diagnoses = self.ALL_DIAGNOSES
        for d in diagnoses:
            hint = hint_for_diagnosis(d, level=2)
            self.assertIsInstance(hint, str, f"Level 2 hint for {d} should be a string")
            self.assertTrue(len(hint) > 20, f"Level 2 hint for {d} should be more detailed (got {len(hint)} chars)")

    def test_hints_for_all_diagnoses_at_level_3(self):
        diagnoses = self.ALL_DIAGNOSES
        for d in diagnoses:
            hint = hint_for_diagnosis(d, level=3)
            self.assertIsInstance(hint, str, f"Level 3 hint for {d} should be a string")
            self.assertTrue(len(hint) > 40, f"Level 3 hint for {d} should be very detailed (got {len(hint)} chars)")

    def test_hint_level_escalation(self):
        """Level 3 should be longer/more detailed than level 1."""
        for d in ["syntax_error", "none_returned", "wrong_type", "incorrect_logic"]:
            hint1 = hint_for_diagnosis(d, level=1)
            hint3 = hint_for_diagnosis(d, level=3)
            self.assertGreater(len(hint3), len(hint1),
                               f"Level 3 hint for {d} should be longer than level 1")

    def test_unknown_diagnosis_falls_back(self):
        hint = hint_for_diagnosis("nonexistent_diagnosis")
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)

    def test_unknown_level_falls_back_to_level_1(self):
        hint = hint_for_diagnosis("syntax_error", level=999)
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)


class HintBuilderFunctionNotFoundTests(unittest.TestCase):
    """Context-aware hint builder for function_not_found."""

    def test_with_expected_and_closest(self):
        context = {"expected_fn": "calculate_grade", "closest_match": "calc_grade"}
        hint = _build_function_not_found_hint(context, level=1)
        self.assertIn("calculate_grade", hint)
        self.assertIn("calc_grade", hint)
        self.assertIn("rename", hint.lower())

    def test_with_expected_but_no_closest(self):
        context = {"expected_fn": "calculate_grade"}
        hint = _build_function_not_found_hint(context, level=1)
        self.assertIn("calculate_grade", hint)

    def test_with_all_context_level_3(self):
        context = {"expected_fn": "calculate_grade", "defined_functions": ["add", "sub"]}
        hint = _build_function_not_found_hint(context, level=3)
        self.assertIn("calculate_grade", hint)
        self.assertIn("add", hint)
        self.assertIn("sub", hint)

    def test_with_no_context(self):
        hint = _build_function_not_found_hint({}, level=1)
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)

    def test_level_2_vs_level_1_detail(self):
        context = {"expected_fn": "foo", "closest_match": "fo"}
        hint1 = _build_function_not_found_hint(context, level=1)
        hint2 = _build_function_not_found_hint(context, level=2)
        self.assertNotEqual(hint1, hint2)
        self.assertTrue(len(hint2) >= len(hint1))

    def test_bare_empty_context(self):
        hint = _build_function_not_found_hint({})
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)

    def test_with_defined_functions_but_no_expected(self):
        context = {"defined_functions": ["a", "b"]}
        hint = _build_function_not_found_hint(context, level=3)
        self.assertIsInstance(hint, str)


class HintBuilderIncorrectLogicTests(unittest.TestCase):
    """Context-aware hint builder for incorrect_logic."""

    def test_with_expected_and_actual(self):
        context = {"expected": 10, "actual": 5}
        hint = _build_incorrect_logic_hint(context, level=1)
        self.assertIn("10", hint)
        self.assertIn("5", hint)

    def test_with_expected_and_actual_level_2(self):
        context = {"expected": "hello", "actual": "world"}
        hint = _build_incorrect_logic_hint(context, level=2)
        self.assertIn("hello", hint)
        self.assertIn("world", hint)
        # Level 2 should be more actionable
        self.assertIn("condition", hint.lower())

    def test_without_context(self):
        hint = _build_incorrect_logic_hint({}, level=1)
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)

    def test_level_2_without_context(self):
        hint = _build_incorrect_logic_hint({}, level=2)
        self.assertIsInstance(hint, str)
        self.assertIn("test input", hint.lower())

    def test_with_none_expected_falls_back(self):
        """When expected is None, builder falls back to generic hint — values not included."""
        context = {"expected": None, "actual": 42}
        hint = _build_incorrect_logic_hint(context, level=1)
        self.assertNotIn("42", hint)
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)
        self.assertIn("wrong answer", hint.lower())


class HintBuilderParamCountTests(unittest.TestCase):
    """Context-aware hint builder for wrong_parameter_count."""

    def test_with_both_counts_level_1(self):
        """Level 1 param hint points to the counts without calling out extra/missing."""
        context = {"student_param_count": 3, "test_call_arg_count": 2}
        hint = _build_param_count_hint(context, level=1)
        self.assertIn("3", hint)
        self.assertIn("2", hint)

    def test_fewer_params_level_1(self):
        context = {"student_param_count": 1, "test_call_arg_count": 3}
        hint = _build_param_count_hint(context, level=1)
        self.assertIn("1", hint)
        self.assertIn("3", hint)

    def test_level_2_with_counts_extra(self):
        context = {"student_param_count": 3, "test_call_arg_count": 2}
        hint = _build_param_count_hint(context, level=2)
        self.assertIn("extra", hint.lower())

    def test_level_2_with_counts_missing(self):
        context = {"student_param_count": 1, "test_call_arg_count": 3}
        hint = _build_param_count_hint(context, level=2)
        self.assertIn("missing", hint.lower())

    def test_level_2_with_counts(self):
        context = {"student_param_count": 2, "test_call_arg_count": 4}
        hint = _build_param_count_hint(context, level=2)
        self.assertIn("def my_func", hint)

    def test_without_context(self):
        hint = _build_param_count_hint({}, level=1)
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)
        self.assertIn("number of parameters", hint)

    def test_with_equal_counts_no_hint_about_diff(self):
        context = {"student_param_count": 2, "test_call_arg_count": 2}
        hint = _build_param_count_hint(context, level=1)
        # Should still give useful guidance
        self.assertIsInstance(hint, str)
        self.assertTrue(len(hint) > 0)


class ParseParamCountErrorTests(unittest.TestCase):
    """The TypeError text carries exact arities; parse them for precise hints."""

    def test_takes_one_but_two(self):
        student, call = _parse_param_count_error(
            "TypeError: my_func() takes 1 positional argument but 2 were given"
        )
        self.assertEqual((student, call), (1, 2))

    def test_takes_two_but_one_singular(self):
        student, call = _parse_param_count_error(
            "TypeError: f() takes 2 positional arguments but 1 was given"
        )
        self.assertEqual((student, call), (2, 1))

    def test_no_match_returns_none(self):
        student, call = _parse_param_count_error("TypeError: unsupported operand type")
        self.assertEqual((student, call), (None, None))

    def test_missing_required_falls_back(self):
        # The "missing N required" message only names how many are missing, not
        # the function's total arity, so it cannot yield precise counts.
        student, call = _parse_param_count_error(
            "TypeError: f() missing 1 required positional argument: 'x'"
        )
        self.assertEqual((student, call), (None, None))


class ClassifyFailureRegressionTests(unittest.TestCase):
    """Regression tests for bugs found in previous diagnostics.

    These test cases document real bugs that were fixed and should not reappear.
    """

    def test_pytest_assertion_failure_is_not_runtime_error(self):
        """Regression: assert failure should not be classified as runtime."""
        result = classify_failure(error="assert 1 == 2")
        self.assertEqual(result, "assertion_failed")

    def test_basic_correct_path(self):
        """Happy path: correct code should not trigger classify_failure."""
        result = classify_failure(expected=5, actual=5, error="")
        self.assertEqual(result, "incorrect_logic")

    def test_print_instead_of_return_detected(self):
        result = classify_failure(expected=5, actual=None, stdout="5\n")
        self.assertEqual(result, "prints_instead_of_returns")

    def test_wrong_type_basic(self):
        result = classify_failure(expected=5, actual="5")
        self.assertEqual(result, "wrong_type")

    def test_syntax_error_basic(self):
        result = classify_failure(error="SyntaxError: invalid syntax")
        self.assertEqual(result, "syntax_error")

    def test_missing_function_basic(self):
        result = classify_failure(error="AttributeError: module 'x' has no attribute 'f'")
        self.assertEqual(result, "function_not_found")

    def test_indentation_is_syntax(self):
        result = classify_failure(error="IndentationError: unexpected indent")
        self.assertEqual(result, "syntax_error")


if __name__ == "__main__":
    unittest.main()
