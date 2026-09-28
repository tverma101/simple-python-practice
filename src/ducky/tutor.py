from __future__ import annotations

import logging

from .diagnostics import _parse_param_count_error, hint_for_diagnosis
from .llm_client import HintRequest, NoLLMClient

logger = logging.getLogger(__name__)


def _context_from_request(request: HintRequest) -> dict:
    """Extract a hint context dict from a HintRequest."""
    context: dict = {}

    # Function name mismatch context
    mismatch = request.function_mismatch
    if mismatch and mismatch.get("mismatch"):
        missing = mismatch.get("missing", [])
        suggestions = mismatch.get("suggestions", {})
        defined = mismatch.get("defined", [])
        if missing:
            context["expected_fn"] = missing[0]
            context["defined_functions"] = defined
            if missing[0] in suggestions:
                context["closest_match"] = suggestions[missing[0]]

    # Expected/actual values for specific hint tailoring
    if request.expected is not None or request.actual is not None:
        try:
            context["expected"] = repr(request.expected)
        except Exception:
            context["expected"] = str(request.expected) if request.expected is not None else None
        try:
            context["actual"] = repr(request.actual)
        except Exception:
            context["actual"] = str(request.actual) if request.actual is not None else None

    # Parameter-count mismatch: the TypeError text carries the exact arities
    # ("takes 1 positional argument but 2 were given"). classifies as
    # wrong_parameter_count, and the hint builder needs these counts — extract
    # them here so the student gets a precise hint instead of the vague generic.
    if request.diagnosis == "wrong_parameter_count":
        src = request.error or request.raw_output or ""
        student_params, test_call_args = _parse_param_count_error(src)
        if student_params is not None and test_call_args is not None:
            context["student_param_count"] = student_params
            context["test_call_arg_count"] = test_call_args

    return context


class TutorEngine:
    def __init__(self, llm_client=None, remote_client=None) -> None:
        self.llm_client = llm_client or NoLLMClient()
        self.remote_client = remote_client

    def build_hint(self, request: HintRequest, attempt_number: int = 1) -> str:
        """Build a hint for a test failure, escalating with attempt count.

        *attempt_number* controls hint detail:
        - 1–2: Gentle nudge
        - 3–4: Specific pointer
        - 5+: Near full guidance

        Function name mismatches are escalated immediately regardless of attempt.
        """
        # Determine escalation level
        # Function name mismatches get escalated immediately
        fn_mismatch = request.function_mismatch
        if fn_mismatch and fn_mismatch.get("mismatch"):
            # Immediate escalation for function name issues
            level = min((attempt_number + 2) // 2, 3)
        else:
            level = min((attempt_number + 1) // 2, 3)

        context = _context_from_request(request)
        rule_hint = hint_for_diagnosis(request.diagnosis, context, level=level)

        llm_hint = None

        if self.remote_client:
            try:
                llm_hint = self.remote_client.request_hint(request.to_dict())
            except Exception as exc:
                logger.debug("Remote hint request failed: %s", exc)

        if not llm_hint and not isinstance(self.llm_client, NoLLMClient):
            llm_hint = self.llm_client.generate_hint(request)

        if llm_hint:
            return f"{rule_hint}\n\nDucky hint:\n{llm_hint.strip()}"
        return rule_hint
