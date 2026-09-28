"""Regression coverage for the student-facing explanations found in sandbox runs."""

from __future__ import annotations

import unittest

from ducky.diagnostics import classify_failure, hint_for_diagnosis


class ThirtyScenarioExplanationTests(unittest.TestCase):
    """Keep common beginner failures specific enough to act on."""

    CASES = [
        ("syntax", {"raw_output": "SyntaxError: invalid syntax"}, "syntax_error"),
        ("indentation", {"error": "IndentationError: unexpected indent"}, "syntax_error"),
        ("module", {"error": "ModuleNotFoundError: No module named 'missing'"}, "import_error"),
        ("function", {"error": "AttributeError: module 'solution' has no attribute 'f'"}, "function_not_found"),
        ("arity", {"error": "TypeError: f() takes 1 positional argument but 2 were given"}, "wrong_parameter_count"),
        ("missing arity", {"error": "TypeError: f() missing 1 required positional argument: 'x'"}, "wrong_parameter_count"),
        ("none", {"expected": 2, "actual": None}, "none_returned"),
        ("print", {"expected": 2, "actual": None, "stdout": "2\n"}, "prints_instead_of_returns"),
        ("zero", {"error": "ZeroDivisionError: division by zero"}, "division_by_zero"),
        ("index", {"error": "IndexError: list index out of range"}, "index_out_of_range"),
        ("key", {"error": "KeyError: 'name'"}, "missing_key"),
        ("name", {"error": "NameError: name 'missing' is not defined"}, "undefined_variable"),
        ("type operation", {"error": "TypeError: unsupported operand type(s) for +"}, "type_error"),
        ("int float", {"expected": 80, "actual": 80.0}, "wrong_type"),
        ("str int", {"expected": 42, "actual": "42"}, "wrong_type"),
        ("bool int", {"expected": 1, "actual": True}, "wrong_type"),
        ("leading spaces", {"expected": "hello", "actual": " hello"}, "extra_spaces"),
        ("internal spaces", {"expected": "hello world", "actual": "hello  world"}, "extra_spaces"),
        ("case", {"expected": "Hello", "actual": "hello"}, "case_mismatch"),
        ("list order", {"expected": [1, 2], "actual": [2, 1]}, "list_order_wrong"),
        ("collection type", {"expected": [1, 2], "actual": (1, 2)}, "collection_type_mismatch"),
        ("dict value", {"expected": {"a": 1}, "actual": {"a": 2}}, "container_value_mismatch"),
        ("empty none", {"expected": "", "actual": None}, "none_returned"),
        ("negative boundary", {"expected": True, "actual": False}, "boolean_logic"),
        ("zero boundary", {"expected": True, "actual": False}, "boolean_logic"),
        ("float precision", {"expected": 0.3, "actual": 0.30000000000000004}, "numeric_precision"),
        ("empty input", {"error": "IndexError: list index out of range"}, "index_out_of_range"),
        ("keyword arity", {"error": "TypeError: f() missing 1 required positional argument: 'x'"}, "wrong_parameter_count"),
        ("timeout", {"error": "DuckyTimeoutError: diagnostic case exceeded 1 seconds"}, "timeout"),
        ("branch logic", {"expected": "no", "actual": "nope"}, "incorrect_logic"),
        ("student assert", {"error": "AssertionError: x must be positive", "actual": None}, "assertion_failed"),
        ("none attribute", {"error": "AttributeError: 'NoneType' object has no attribute 'name'"}, "attribute_error"),
        ("value format", {"error": "ValueError: invalid literal for int()"}, "value_error"),
        ("overflow", {"error": "OverflowError: math range error"}, "overflow_error"),
        ("recursion", {"error": "RecursionError: maximum recursion depth exceeded"}, "recursion_error"),
        ("iterator exhausted", {"error": "StopIteration"}, "stop_iteration"),
        ("missing file", {"error": "FileNotFoundError: [Errno 2] No such file"}, "file_not_found"),
        ("permission", {"error": "PermissionError: [Errno 13] Permission denied"}, "permission_error"),
        ("set return", {"actual": "{1, 2}", "expected": [1, 2], "actual_jsonable": False}, "unserializable_return"),
        ("bytes return", {"actual": "b'hi'", "expected": "hi", "actual_jsonable": False}, "unserializable_return"),
        ("generator return", {"actual": "<generator object f>", "expected": [1], "actual_jsonable": False}, "unserializable_return"),
        ("attribute object", {"error": "AttributeError: 'int' object has no attribute 'name'"}, "attribute_error"),
        ("invalid date", {"error": "ValueError: month must be in 1..12"}, "value_error"),
        ("recursive factorial", {"error": "RecursionError: maximum recursion depth exceeded in comparison"}, "recursion_error"),
        ("large exponent", {"error": "OverflowError: numerical result out of range"}, "overflow_error"),
        ("open permission", {"error": "PermissionError: operation not permitted"}, "permission_error"),
        ("next exhausted", {"error": "StopIteration: no more values"}, "stop_iteration"),
        ("assert branch", {"error": "AssertionError: expected positive input", "actual": None}, "assertion_failed"),
        ("wrong mapping value", {"expected": {"a": 1}, "actual": {"a": 2}}, "container_value_mismatch"),
        ("rounding boundary", {"expected": 1.0, "actual": 1.0000000001}, "numeric_precision"),
    ]

    def test_all_thirty_scenarios_have_named_diagnoses(self):
        self.assertEqual(len(self.CASES), 50)
        for name, values, expected_diagnosis in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(classify_failure(**values), expected_diagnosis)

    def test_new_diagnoses_do_not_fall_back_to_generic_logic_copy(self):
        specific = {
            "type_error",
            "boolean_logic",
            "numeric_precision",
            "container_value_mismatch",
            "collection_type_mismatch",
            "timeout",
            "assertion_failed",
            "attribute_error",
            "file_not_found",
            "overflow_error",
            "permission_error",
            "recursion_error",
            "stop_iteration",
            "unserializable_return",
            "value_error",
        }
        for diagnosis in specific:
            with self.subTest(diagnosis=diagnosis):
                hint = hint_for_diagnosis(diagnosis, level=1)
                self.assertNotIn("Check what your function returns", hint)
                self.assertGreater(len(hint), 40)

    def test_none_hint_names_expected_value(self):
        hint = hint_for_diagnosis("none_returned", {"expected": "2"}, level=1)
        self.assertIn("2", hint)
        self.assertIn("return", hint.lower())

    def test_contextual_logic_hint_is_not_the_old_vague_message(self):
        hint = hint_for_diagnosis("incorrect_logic", {"expected": "no", "actual": "nope"}, level=1)
        self.assertIn("no", hint)
        self.assertIn("nope", hint)
        self.assertNotIn("Check what your function returns", hint)
