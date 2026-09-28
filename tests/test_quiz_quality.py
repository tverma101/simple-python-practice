"""Regression checks for the quality of the bundled practice quiz bank."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ducky.quiz import grade_question, load_general_quizzes, parse_quiz_dict, validate_quiz_dict

QUIZ_ROOT = Path(__file__).parents[1] / "quizzes"


def _normalize_choice(value: object) -> str:
    """Match the quiz grader's case/whitespace normalization."""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _quiz_files() -> list[Path]:
    return sorted(QUIZ_ROOT.glob("*.json"))


@pytest.fixture(scope="module")
def quiz_data() -> list[tuple[Path, dict]]:
    files = _quiz_files()
    assert files, f"No quiz JSON files found in {QUIZ_ROOT}"
    return [(path, json.loads(path.read_text(encoding="utf-8"))) for path in files]


def test_every_bundled_quiz_passes_schema_validation(quiz_data):
    for path, data in quiz_data:
        ok, errors, _warnings = validate_quiz_dict(data)
        assert ok, f"{path.name}: {errors}"


def test_every_question_has_a_concept_and_useful_explanation(quiz_data):
    for path, data in quiz_data:
        for question in data["questions"]:
            location = f"{path.name}/{question.get('id', '<missing id>')}"
            concept = question.get("concept", question.get("concept_tag", ""))
            explanation = question.get("explanation", "")
            assert isinstance(concept, str) and concept.strip(), (
                f"{location} needs a real concept label"
            )
            assert isinstance(explanation, str) and len(explanation.strip()) >= 45, (
                f"{location} needs explanatory text of at least 45 characters"
            )


def test_multiple_choice_answers_are_unambiguous(quiz_data):
    for path, data in quiz_data:
        for question in data["questions"]:
            if question["type"] not in {"multiple_choice", "mcq"}:
                continue
            location = f"{path.name}/{question.get('id', '<missing id>')}"
            options = question.get("options", [])
            normalized = [_normalize_choice(option) for option in options]
            assert all(normalized), f"{location} has an empty option"
            assert len(normalized) == len(set(normalized)), (
                f"{location} has duplicate options after normalization"
            )
            correct = _normalize_choice(question.get("correct", ""))
            assert sum(option == correct for option in normalized) == 1, (
                f"{location} correct answer must match exactly one option after normalization"
            )


def test_predict_output_code_is_valid_python(quiz_data):
    for path, data in quiz_data:
        for question in data["questions"]:
            if question["type"] != "predict_output":
                continue
            location = f"{path.name}/{question.get('id', '<missing id>')}"
            try:
                ast.parse(question["code"], filename=location, mode="exec")
            except (SyntaxError, TypeError) as exc:
                pytest.fail(f"{location} has invalid Python in code: {exc}")


def test_predict_output_answers_match_actual_python_execution(quiz_data, tmp_path):
    """Catch stale expected output while keeping snippets isolated and bounded."""
    for path, data in quiz_data:
        quiz = parse_quiz_dict(data)
        for question in quiz.questions:
            if question.type != "predict_output":
                continue
            completed = subprocess.run(
                [sys.executable, "-c", question.code],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if completed.returncode == 0:
                actual = completed.stdout
            else:
                lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
                actual = lines[-1].split(":", 1)[0] if lines else f"exit {completed.returncode}"
            correct, detail = grade_question(question, actual)
            assert correct, f"{path.name}/{question.id}: expected {question.correct!r}, got {actual!r} ({detail})"


def test_parsed_catalog_contains_each_bundled_quiz_once(quiz_data):
    catalog = load_general_quizzes()
    expected_ids = {data["quiz_id"] for _path, data in quiz_data}
    parsed_ids = [quiz.quiz_id for quiz in catalog]

    assert expected_ids <= set(parsed_ids)
    assert len(parsed_ids) == len(set(parsed_ids))
    assert len(parsed_ids) == len(expected_ids)
    for path, data in quiz_data:
        parsed = next(quiz for quiz in catalog if quiz.quiz_id == data["quiz_id"])
        assert len(parsed.questions) == len(data["questions"]), path.name
        assert all(question.concept.strip() for question in parsed.questions)
        assert all(question.explanation.strip() for question in parsed.questions)
