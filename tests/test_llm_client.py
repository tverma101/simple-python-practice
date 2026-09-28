import unittest

from ducky.llm_client import FailureContext, HintRequest, build_hint_prompt


class OllamaPromptTests(unittest.TestCase):
    def test_complex_failure_context_is_in_prompt(self):
        request = HintRequest(
            lesson_id="lesson.complex_01",
            title="Normalize Scores",
            code_snippet="def normalize_scores(scores):\n    return scores\n",
            diagnosis="runtime_error",
            failure_name="test_normalize_scores_rejects_bad_input",
            expected={"count": 3, "total": 8, "average": 2.6666666667},
            actual=None,
            stdout="debug: starting\ntrace: processing list",
            error="ValueError: invalid score",
            raw_output="Traceback (most recent call last):\n  ValueError: invalid score",
            concept_tags=["lists", "validation"],
            failure_breakdown=[
                FailureContext(
                    name="test_normalize_scores_rejects_bad_input",
                    diagnosis="runtime_error",
                    expected={"count": 3, "total": 8, "average": 2.6666666667},
                    actual=None,
                    stdout="debug: starting\ntrace: processing list",
                    error="ValueError: invalid score",
                ),
                FailureContext(
                    name="test_normalize_scores_keeps_length",
                    diagnosis="wrong_value",
                    expected={"count": 4, "total": 10, "average": 2.5},
                    actual={"count": 3, "total": 10, "average": 3.3333333333},
                    stdout="",
                    error="AssertionError: counts should stay aligned",
                ),
            ],
        )

        prompt = build_hint_prompt(request)

        self.assertIn("lesson.complex_01", prompt)
        self.assertIn("Diagnosis: runtime_error", prompt)
        self.assertIn("lists, validation", prompt)
        self.assertIn("test_normalize_scores_rejects_bad_input", prompt)
        self.assertIn("Test 1: test_normalize_scores_rejects_bad_input", prompt)
        self.assertIn("Test 2: test_normalize_scores_keeps_length", prompt)
        self.assertIn("trace: processing list", prompt)
        self.assertIn("ValueError: invalid score", prompt)
        self.assertIn("STUDENT'S CURRENT CODE", prompt)
        self.assertIn("YOUR HINT", prompt)
        self.assertIn("Do NOT write the corrected code", prompt)

    def test_attempt_number_in_prompt(self):
        """Higher attempt numbers should cause more urgent escalation in the prompt."""
        request = HintRequest(
            lesson_id="test.1",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="incorrect_logic",
            attempt_number=5,
        )
        prompt = build_hint_prompt(request)
        self.assertIn("Attempt #5", prompt)
        self.assertIn("tried multiple times", prompt.lower())

    def test_early_attempt_prompt_is_gentle(self):
        """Early attempts should have gentle escalation."""
        request = HintRequest(
            lesson_id="test.1",
            title="Test",
            code_snippet="def f(): pass",
            diagnosis="incorrect_logic",
            attempt_number=1,
        )
        prompt = build_hint_prompt(request)
        self.assertIn("Attempt #1", prompt)
        self.assertIn("gentle nudge", prompt.lower())

    def test_function_mismatch_in_prompt(self):
        """Function name mismatch info should appear in the prompt."""
        request = HintRequest(
            lesson_id="test.1",
            title="Test",
            code_snippet="def calc_grade(s): pass",
            diagnosis="function_not_found",
            function_mismatch={
                "mismatch": True,
                "missing": ["calculate_grade"],
                "suggestions": {"calculate_grade": "calc_grade"},
                "defined": ["calc_grade"],
            },
        )
        prompt = build_hint_prompt(request)
        self.assertIn("FUNCTION NAME MISMATCH", prompt)
        self.assertIn("calculate_grade", prompt)
        self.assertIn("calc_grade", prompt)

    def test_function_mismatch_not_in_prompt_when_no_mismatch(self):
        """No function mismatch section when mismatch dict is empty."""
        request = HintRequest(
            lesson_id="test.1",
            title="Test",
            code_snippet="def add(a, b): return a + b",
            diagnosis="incorrect_logic",
            function_mismatch={"mismatch": False, "missing": [], "suggestions": {}},
        )
        prompt = build_hint_prompt(request)
        self.assertNotIn("FUNCTION NAME MISMATCH", prompt)
