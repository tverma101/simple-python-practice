from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _format_value(value: Any, limit: int = 180) -> str:
    try:
        text = repr(value)
    except Exception:
        text = str(value)
    return _clip(text, limit)


@dataclass
class FailureContext:
    name: str
    diagnosis: str
    expected: Any = None
    actual: Any = None
    stdout: str = ""
    error: str = ""


@dataclass
class HintRequest:
    lesson_id: str
    title: str
    code_snippet: str
    diagnosis: str
    failure_name: str = ""
    expected: Any = None
    actual: Any = None
    stdout: str = ""
    error: str = ""
    raw_output: str = ""
    concept_tags: list[str] = field(default_factory=list)
    failure_breakdown: list[FailureContext] = field(default_factory=list)
    instructions: str = ""
    test_cases: str = ""
    student_code: str = ""
    answer_key: str = ""
    attempt_number: int = 1
    function_mismatch: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "title": self.title,
            "code_snippet": self.code_snippet,
            "diagnosis": self.diagnosis,
            "failure_name": self.failure_name,
            "expected": self.expected,
            "actual": self.actual,
            "stdout": self.stdout,
            "error": self.error,
            "raw_output": self.raw_output,
            "concept_tags": self.concept_tags,
            "failure_breakdown": [
                {
                    "name": f.name,
                    "diagnosis": f.diagnosis,
                    "expected": f.expected,
                    "actual": f.actual,
                    "stdout": f.stdout,
                    "error": f.error,
                }
                for f in self.failure_breakdown
            ],
            "instructions": self.instructions,
            "test_cases": self.test_cases,
            "student_code": self.student_code,
            "answer_key": self.answer_key,
            "attempt_number": self.attempt_number,
            "function_mismatch": self.function_mismatch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HintRequest:
        breakdown = []
        for f in data.get("failure_breakdown", []):
            breakdown.append(
                FailureContext(
                    name=f.get("name", ""),
                    diagnosis=f.get("diagnosis", ""),
                    expected=f.get("expected"),
                    actual=f.get("actual"),
                    stdout=f.get("stdout", ""),
                    error=f.get("error", ""),
                )
            )
        return cls(
            lesson_id=data.get("lesson_id", ""),
            title=data.get("title", ""),
            code_snippet=data.get("code_snippet", ""),
            diagnosis=data.get("diagnosis", ""),
            failure_name=data.get("failure_name", ""),
            expected=data.get("expected"),
            actual=data.get("actual"),
            stdout=data.get("stdout", ""),
            error=data.get("error", ""),
            raw_output=data.get("raw_output", ""),
            concept_tags=data.get("concept_tags", []),
            failure_breakdown=breakdown,
            instructions=data.get("instructions", ""),
            test_cases=data.get("test_cases", ""),
            student_code=data.get("student_code", ""),
            answer_key=data.get("answer_key", ""),
            attempt_number=data.get("attempt_number", 1),
            function_mismatch=data.get("function_mismatch"),
        )


def build_hint_prompt(request: HintRequest) -> str:
    # ── Escalation guidance based on attempt count ──────────────────
    if request.attempt_number <= 2:
        escalation = (
            "This is the student's early attempt. Give a gentle nudge — "
            "point to the general area of the problem but don't spell out the fix."
        )
    elif request.attempt_number <= 4:
        escalation = (
            "This is a mid-level attempt. Be more specific — name the issue "
            "and suggest what to check, but still let the student figure out the exact fix."
        )
    else:
        escalation = (
            "The student has tried multiple times. Give detailed guidance "
            "with a concrete example of the fix, but still don't write the "
            "complete corrected code for them."
        )

    sections = [
        "You are Ducky, a beginner-friendly Python tutor.",
        "",
        "YOUR JOB: Help the student get closer to the correct answer by nudging them in the right direction.",
        "DO NOT give the full solution. Instead, point out the specific mistake or missing piece.",
        "Reference the test cases below to explain WHAT the code should do, then guide them to fix it.",
        "Be concrete: mention the specific function, line, or logic that needs changing.",
        "",
        f"ESCALATION LEVEL: {escalation}",
        f"Attempt #{request.attempt_number}",
        "",
    ]

    # ── Function name mismatch (critical — call out immediately) ────
    mismatch = request.function_mismatch
    if mismatch and mismatch.get("mismatch"):
        missing = mismatch.get("missing", [])
        suggestions = mismatch.get("suggestions", {})
        defined = mismatch.get("defined", [])
        if missing and suggestions:
            sections.append("=== FUNCTION NAME MISMATCH (tell the student about this!) ===")
            for miss in missing:
                if miss in suggestions:
                    sections.append(
                        f"The test calls ``{miss}()`` but the student defined "
                        f"``{suggestions[miss]}()``. Tell them to rename "
                        f"``{suggestions[miss]}`` → ``{miss}``."
                    )
                else:
                    sections.append(
                        f"The test expects ``{miss}()`` which is missing. "
                        f"Student defined: {', '.join(defined) if defined else 'none'}."
                    )
            sections.append("")

    # ── Lesson instructions ────────────────────────────────────────
    sections.append("=== LESSON INSTRUCTIONS ===")

    if request.instructions:
        sections.append(_clip(request.instructions, 2000))
    else:
        sections.append(f"Lesson: {request.title} (ID: {request.lesson_id})")

    sections.extend(["", "=== TEST CASES (what the code must pass) ==="])

    if request.test_cases:
        sections.append(_clip(request.test_cases, 2000))
    elif request.failure_breakdown:
        for index, failure in enumerate(request.failure_breakdown[:5], 1):
            sections.append(f"  Test {index}: {failure.name}")
            sections.append(f"    Expected: {_format_value(failure.expected)}")
            sections.append(f"    Got:      {_format_value(failure.actual)}")
    else:
        sections.append(f"Expected: {_format_value(request.expected)}")
        sections.append(f"Actual:   {_format_value(request.actual)}")

    if request.answer_key:
        sections.extend(["", "=== ANSWER KEY (DO NOT reveal directly) ==="])
        sections.append("Use this to understand the correct approach, but do NOT paste it.")
        sections.append(_clip(request.answer_key, 1500))

    sections.extend(
        [
            "",
            "=== STUDENT'S CURRENT CODE ===",
            _clip(request.student_code or request.code_snippet, 2000),
            "",
            "=== DIAGNOSIS ===",
            f"Failure: {request.failure_name}" if request.failure_name else "",
            f"Diagnosis: {request.diagnosis}",
        ]
    )

    if request.concept_tags:
        sections.append(f"Concepts: {', '.join(request.concept_tags)}")

    if request.stdout:
        sections.extend(["", "=== STUDENT STDOUT ===", _clip(request.stdout, 500)])
    if request.error:
        sections.extend(["", "=== STUDENT ERROR ===", _clip(request.error, 600)])

    if request.raw_output:
        sections.extend(["", "=== FULL TEST OUTPUT ===", _clip(request.raw_output, 800)])

    sections.extend(
        [
            "",
            "=== YOUR HINT ===",
            "Write 1-3 sentences that:",
            "1. Point to the specific bug or missing piece in the student's code",
            "2. Reference what the test expects vs what the code does",
            "3. Give the student a concrete next step (e.g., 'check your return value', 'what does the function return when...')",
            "Do NOT write the corrected code. Do NOT paste the answer key. Nudge only.",
        ]
    )

    return "\n".join(section for section in sections if section is not None).strip() + "\n"


SYSTEM_PROMPT = (
    "You are Ducky, a beginner-friendly Python tutor. "
    "Your job is to nudge students toward the correct answer by pointing out specific mistakes "
    "in their code. Never give the full solution. Reference test cases to explain what the code "
    "should do, then guide them to fix it. Be concrete and specific."
)


class NoLLMClient:
    def generate_hint(self, request: HintRequest) -> str | None:
        return None


class FakeLLMClient:
    def generate_hint(self, request: HintRequest) -> str:
        return f"Fake tutor hint for {request.diagnosis}."


class OllamaClient:
    def __init__(self, model: str = "qwen2.5-coder:1.5b", base_url: str = "http://localhost:11434") -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")

    def list_models(self) -> list[dict[str, str]]:
        req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    size_bytes = m.get("size", 0)
                    size_gb = f"{size_bytes / (1024**3):.1f}GB" if size_bytes else ""
                    models.append({"key": name, "name": name, "size_gb": size_gb})
                return models
        except Exception:
            return []

    def generate_hint(self, request: HintRequest) -> str | None:
        prompt = build_hint_prompt(request)
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 120},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response")
        except Exception:
            return None


class GeminiClient:
    """Google Gemini API client (cloud, uses API key).

    Works with a free Gemini API key from Google AI Studio:
    https://aistudio.google.com/apikey
    """

    GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-2.5-flash",
    ) -> None:
        self.api_key = api_key
        self.model = model

    def generate_hint(self, request: HintRequest) -> str | None:
        if not self.api_key:
            return None

        user_prompt = build_hint_prompt(request)
        payload = json.dumps(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": f"{SYSTEM_PROMPT}\n\n{user_prompt}"}],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 256,
                    "topP": 0.95,
                },
            }
        ).encode("utf-8")

        url = self.GEMINI_URL.format(model=self.model)
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if parts:
                        return parts[0].get("text")
                return None
        except Exception:
            return None


class OpenAIClient:
    """OpenAI API client (cloud, uses API key).

    Works with any OpenAI-compatible API:
    - OpenAI: https://api.openai.com/v1
    - Any OpenAI-compatible endpoint (custom base_url)
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def generate_hint(self, request: HintRequest) -> str | None:
        if not self.api_key:
            return None

        user_prompt = build_hint_prompt(request)
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 256,
            }
        ).encode("utf-8")

        url = f"{self.base_url}/chat/completions"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content")
                return None
        except Exception:
            return None


class LMStudioClient:
    def __init__(self, model: str = "default", base_url: str = "http://localhost:1234", max_tokens: int = 2500) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens

    def list_models(self) -> list[dict[str, str]]:
        """Fetch available models from LM Studio server."""
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/models",
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = []
                for m in data.get("models", []):
                    if m.get("type") == "llm":
                        key = m.get("key", "")
                        display = m.get("display_name", key)
                        loaded = len(m.get("loaded_instances", []))
                        size_gb = m.get("size_bytes", 0) / (1024**3)
                        models.append(
                            {
                                "key": key,
                                "name": display,
                                "loaded": loaded > 0,
                                "size_gb": f"{size_gb:.1f}GB",
                            }
                        )
                return models
        except Exception:
            return []

    def load_model(self, model_key: str, context_length: int = 4096) -> dict:
        """Load a model with specific context length."""
        payload = json.dumps(
            {
                "model": model_key,
                "context_length": context_length,
                "flash_attention": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/models/load",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"error": str(e)}

    def generate_hint(self, request: HintRequest) -> str | None:
        user_prompt = build_hint_prompt(request)
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": self.max_tokens,
                "stream": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content")
                return None
        except Exception:
            return None
