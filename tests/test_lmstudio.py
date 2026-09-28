from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from ducky.llm_client import (
    FailureContext,
    FakeLLMClient,
    HintRequest,
    LMStudioClient,
    NoLLMClient,
    build_hint_prompt,
)


class HintRequestTests(unittest.TestCase):
    def test_to_dict_and_back(self):
        req = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="pass",
            diagnosis="syntax_error",
            failure_name="test_foo",
            expected=42,
            actual=None,
            stdout="hello",
            error="boom",
            concept_tags=["tag1"],
            failure_breakdown=[
                FailureContext(name="f1", diagnosis="d1", expected=1, actual=2),
            ],
        )
        d = req.to_dict()
        self.assertEqual(d["lesson_id"], "test")
        self.assertEqual(len(d["failure_breakdown"]), 1)

        req2 = HintRequest.from_dict(d)
        self.assertEqual(req2.lesson_id, "test")
        self.assertEqual(req2.failure_breakdown[0].name, "f1")

    def test_from_dict_minimal(self):
        req = HintRequest.from_dict({})
        self.assertEqual(req.lesson_id, "")
        self.assertEqual(req.failure_breakdown, [])


class BuildHintPromptTests(unittest.TestCase):
    def test_prompt_contains_key_sections(self):
        req = HintRequest(
            lesson_id="l1",
            title="My Lesson",
            code_snippet="def foo(): pass",
            diagnosis="missing_function",
        )
        prompt = build_hint_prompt(req)
        self.assertIn("Lesson: My Lesson", prompt)
        self.assertIn("Diagnosis: missing_function", prompt)
        self.assertIn("STUDENT'S CURRENT CODE", prompt)
        self.assertIn("def foo(): pass", prompt)
        self.assertIn("YOUR HINT", prompt)
        self.assertIn("Ducky", prompt)

    def test_prompt_with_concept_tags(self):
        req = HintRequest(
            lesson_id="l1",
            title="L",
            code_snippet="",
            diagnosis="d",
            concept_tags=["loops", "lists"],
        )
        prompt = build_hint_prompt(req)
        self.assertIn("loops, lists", prompt)

    def test_prompt_with_failure_breakdown(self):
        req = HintRequest(
            lesson_id="l1",
            title="L",
            code_snippet="",
            diagnosis="d",
            failure_breakdown=[
                FailureContext(name="f1", diagnosis="d1"),
                FailureContext(name="f2", diagnosis="d2"),
            ],
        )
        prompt = build_hint_prompt(req)
        self.assertIn("Test 1: f1", prompt)
        self.assertIn("Test 2: f2", prompt)

    def test_prompt_with_instructions(self):
        req = HintRequest(
            lesson_id="l1",
            title="L",
            code_snippet="pass",
            diagnosis="d",
            instructions="Write a function that returns 42",
        )
        prompt = build_hint_prompt(req)
        self.assertIn("LESSON INSTRUCTIONS", prompt)
        self.assertIn("Write a function that returns 42", prompt)

    def test_prompt_with_test_cases(self):
        req = HintRequest(
            lesson_id="l1",
            title="L",
            code_snippet="pass",
            diagnosis="d",
            test_cases="def test_foo(): assert foo() == 42",
        )
        prompt = build_hint_prompt(req)
        self.assertIn("TEST CASES", prompt)
        self.assertIn("def test_foo()", prompt)

    def test_prompt_with_student_code(self):
        req = HintRequest(
            lesson_id="l1",
            title="L",
            code_snippet="old snippet",
            diagnosis="d",
            student_code="def foo(): return 0",
        )
        prompt = build_hint_prompt(req)
        self.assertIn("def foo(): return 0", prompt)
        self.assertNotIn("old snippet", prompt)

    def test_prompt_with_answer_key(self):
        req = HintRequest(
            lesson_id="l1",
            title="L",
            code_snippet="pass",
            diagnosis="d",
            answer_key="def foo(): return 42",
        )
        prompt = build_hint_prompt(req)
        self.assertIn("ANSWER KEY", prompt)
        self.assertIn("do NOT paste it", prompt)


class NoLLMClientTests(unittest.TestCase):
    def test_returns_none(self):
        client = NoLLMClient()
        req = HintRequest(lesson_id="", title="", code_snippet="", diagnosis="")
        self.assertIsNone(client.generate_hint(req))


class FakeLLMClientTests(unittest.TestCase):
    def test_returns_fake_hint(self):
        client = FakeLLMClient()
        req = HintRequest(lesson_id="", title="", code_snippet="", diagnosis="syntax_error")
        hint = client.generate_hint(req)
        self.assertIn("syntax_error", hint)


class LMStudioClientTests(unittest.TestCase):
    @patch("ducky.llm_client.urllib.request.urlopen")
    def test_generate_hint_success(self, mock_urlopen):
        response_data = {
            "choices": [{"message": {"content": "Check your return statement"}}],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = LMStudioClient(model="test-model", base_url="http://localhost:1234")
        req = HintRequest(lesson_id="l1", title="L", code_snippet="pass", diagnosis="d")
        hint = client.generate_hint(req)
        self.assertEqual(hint, "Check your return statement")

    @patch("ducky.llm_client.urllib.request.urlopen")
    def test_generate_hint_empty_choices(self, mock_urlopen):
        response_data = {"choices": []}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = LMStudioClient()
        req = HintRequest(lesson_id="", title="", code_snippet="", diagnosis="")
        hint = client.generate_hint(req)
        self.assertIsNone(hint)

    @patch("ducky.llm_client.urllib.request.urlopen", side_effect=Exception("connection refused"))
    def test_generate_hint_connection_error(self, mock_urlopen):
        client = LMStudioClient()
        req = HintRequest(lesson_id="", title="", code_snippet="", diagnosis="")
        hint = client.generate_hint(req)
        self.assertIsNone(hint)
