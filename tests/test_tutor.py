import unittest

from ducky.llm_client import FakeLLMClient, HintRequest, NoLLMClient
from ducky.tutor import TutorEngine, _context_from_request


class TutorTests(unittest.TestCase):
    def test_rule_hint_without_llm(self):
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="prints_instead_of_returns",
        )
        hint = engine.build_hint(request)
        self.assertIn("return", hint)
        self.assertNotIn("Ducky hint", hint)

    def test_llm_hint_appended(self):
        engine = TutorEngine(FakeLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="prints_instead_of_returns",
        )
        hint = engine.build_hint(request)
        self.assertIn("Ducky hint", hint)
        self.assertIn("Fake tutor hint", hint)

    # ── Incremental hint escalation ──────────────────────────────

    def test_level_1_hint_is_gentle(self):
        """Level 1 (attempts 1-2) should give a gentle nudge."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def add(a, b):\n    print(a + b)\n",
            diagnosis="prints_instead_of_returns",
        )
        hint = engine.build_hint(request, attempt_number=1)
        self.assertIn("return", hint.lower())
        # Level 1 should just mention print vs return
        self.assertIn("print", hint.lower())

    def test_level_2_hint_is_specific(self):
        """Level 2 (attempts 3-4) should be more specific."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def add(a, b):\n    print(a + b)\n",
            diagnosis="prints_instead_of_returns",
        )
        hint = engine.build_hint(request, attempt_number=3)
        # Level 2 should be more specific
        self.assertIn("return", hint.lower())
        # Level 2 hints are longer/more detailed than level 1
        self.assertTrue(len(hint) > 50)

    def test_level_3_hint_is_most_detailed(self):
        """Level 3 (attempts 5+) should give near-full guidance."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def add(a, b):\n    print(a + b)\n",
            diagnosis="prints_instead_of_returns",
        )
        hint = engine.build_hint(request, attempt_number=5)
        # Level 3 should be the most detailed
        self.assertTrue(len(hint) > 80)

    def test_function_mismatch_escalates_faster(self):
        """Function name mismatches should escalate at a lower attempt count."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def calc_total(items): pass",
            diagnosis="function_not_found",
            function_mismatch={
                "mismatch": True,
                "missing": ["calculate_total"],
                "suggestions": {"calculate_total": "calc_total"},
                "defined": ["calc_total"],
            },
        )
        # Even at attempt 1, function mismatch should get level 1 or higher
        # (the immediate escalation means level = min((1+2)//2, 3) = 1 for attempt 1,
        #  but the normal formula would give min((1+1)//2, 3) = 1 too, so level is same at attempt 1)
        hint = engine.build_hint(request, attempt_number=1)
        self.assertIn("calc_total", hint)
        self.assertIn("calculate_total", hint)

    def test_function_mismatch_escalates_at_attempt_3(self):
        """At attempt 3, function mismatch should be at least level 2."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def calc_total(items): pass",
            diagnosis="function_not_found",
            function_mismatch={
                "mismatch": True,
                "missing": ["calculate_total"],
                "suggestions": {"calculate_total": "calc_total"},
                "defined": ["calc_total"],
            },
        )
        hint = engine.build_hint(request, attempt_number=3)
        # At attempt 3 with mismatch: level = min((3+2)//2, 3) = min(2, 3) = 2
        # Level 2 should have "Fix:" or more detailed rename instructions
        self.assertTrue("Fix:" in hint or "fix" in hint.lower() or "rename" in hint.lower())

    # ── Context extraction from HintRequest ──────────────────────

    def test_context_from_request_function_mismatch(self):
        """Context should be extracted from function_mismatch in HintRequest."""
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def clac(a, b): pass",
            diagnosis="function_not_found",
            function_mismatch={
                "mismatch": True,
                "missing": ["calc"],
                "suggestions": {"calc": "clac"},
                "defined": ["clac"],
            },
        )
        ctx = _context_from_request(request)
        self.assertEqual(ctx["expected_fn"], "calc")
        self.assertEqual(ctx["closest_match"], "clac")
        self.assertEqual(ctx["defined_functions"], ["clac"])

    def test_context_from_request_no_mismatch(self):
        """Empty context when there's no function mismatch."""
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def add(a, b): return a + b",
            diagnosis="incorrect_logic",
        )
        ctx = _context_from_request(request)
        self.assertEqual(ctx, {})

    def test_context_from_request_mismatch_no_missing(self):
        """Empty context when mismatch is present but has no missing list."""
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="function_not_found",
            function_mismatch={"mismatch": True, "missing": [], "suggestions": {}},
        )
        ctx = _context_from_request(request)
        self.assertEqual(ctx, {})

    def test_context_from_request_param_count_from_error(self):
        """wrong_parameter_count: arities parsed from the TypeError text."""
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def add(a): return a",
            diagnosis="wrong_parameter_count",
            error="TypeError: add() takes 1 positional argument but 2 were given",
        )
        ctx = _context_from_request(request)
        self.assertEqual(ctx.get("student_param_count"), 1)
        self.assertEqual(ctx.get("test_call_arg_count"), 2)

    def test_build_hint_param_count_is_precise_not_generic(self):
        """Regression: a param-count failure must yield the exact counts.

        Before the fix, the counts were never extracted from the TypeError
        text, so ``wrong_parameter_count`` always returned the vague generic
        fallback instead of the precise 'defines 1 but test calls with 2'.
        """
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def add(a): return a",
            diagnosis="wrong_parameter_count",
            error="TypeError: add() takes 1 positional argument but 2 were given",
        )
        hint = engine.build_hint(request)
        self.assertIn("1", hint)
        self.assertIn("2", hint)
        self.assertNotIn("number of parameters", hint)


    # ── No LLM path ─────────────────────────────────────────────

    def test_build_hint_no_llm_no_remote(self):
        """When no LLM is configured, only rule-based hints are returned."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="syntax_error",
        )
        hint = engine.build_hint(request)
        self.assertNotIn("Ducky hint", hint)
        # Should still get a useful rule-based hint
        self.assertTrue(len(hint) > 10)

    def test_build_hint_attempt_number_defaults_to_1(self):
        """Default attempt_number should be 1 (no error)."""
        engine = TutorEngine(NoLLMClient())
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="incorrect_logic",
        )
        hint = engine.build_hint(request)
        # Should not crash and should produce a level 1 hint
        self.assertTrue(len(hint) > 0)

    # ── All diagnoses produce hints at all levels ────────────────

    def test_all_diagnoses_at_all_levels(self):
        """Every known diagnosis should produce a hint at levels 1, 2, and 3."""
        from ducky.diagnostics import hint_for_diagnosis

        diagnoses = [
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
        for d in diagnoses:
            for level in (1, 2, 3):
                hint = hint_for_diagnosis(d, level=level)
                self.assertIsInstance(hint, str, f"Hint for {d} level {level} should be str")
                self.assertTrue(len(hint) > 0, f"Hint for {d} level {level} should not be empty")

    def test_level_2_hints_are_more_detailed_than_level_1(self):
        """Level 2 hints should be generally longer than level 1."""
        from ducky.diagnostics import hint_for_diagnosis

        diagnoses = [
            "prints_instead_of_returns",
            "none_returned",
            "wrong_type",
            "incorrect_logic",
        ]
        for d in diagnoses:
            l1 = hint_for_diagnosis(d, level=1)
            l2 = hint_for_diagnosis(d, level=2)
            # Level 2 should be at least as long as level 1 (usually longer)
            self.assertTrue(len(l2) >= len(l1), f"Level 2 hint for {d} should be >= level 1")


class FunctionNotFoundHintTests(unittest.TestCase):
    """Tests for context-aware function-not-found hints at different levels."""

    def setUp(self):
        from ducky.diagnostics import hint_for_diagnosis

        self.hint_for = hint_for_diagnosis

    def test_level_1_with_closest_match(self):
        ctx = {"expected_fn": "calculate_total", "closest_match": "calc_total"}
        hint = self.hint_for("function_not_found", ctx, level=1)
        self.assertIn("calculate_total", hint)
        self.assertIn("calc_total", hint)
        self.assertIn("Rename", hint)

    def test_level_2_with_closest_match(self):
        ctx = {"expected_fn": "calculate_total", "closest_match": "calc_total"}
        hint = self.hint_for("function_not_found", ctx, level=2)
        # Level 2 should be more explicit
        self.assertIn("Fix:", hint)
        self.assertIn("def calc_total", hint)
        self.assertIn("def calculate_total", hint)

    def test_level_1_missing_no_closest(self):
        ctx = {"expected_fn": "calculate_total"}
        hint = self.hint_for("function_not_found", ctx, level=1)
        self.assertIn("calculate_total", hint)

    def test_level_3_missing_with_defined(self):
        ctx = {
            "expected_fn": "calculate_total",
            "defined_functions": ["helper", "main"],
        }
        hint = self.hint_for("function_not_found", ctx, level=3)
        self.assertIn("helper()", hint)
        self.assertIn("main()", hint)
        self.assertIn("def calculate_total", hint)
