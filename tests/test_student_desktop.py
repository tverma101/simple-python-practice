"""Headless contracts for the windowed student application."""

from __future__ import annotations

from pathlib import Path

from ducky.gui.student_desktop import _question_kind, _quiz_focus, _time_label, load_quiz_catalog
from ducky.quiz import Quiz, QuizQuestion


def test_student_desktop_catalog_exposes_the_whole_practice_bank(tmp_path: Path):
    rows = load_quiz_catalog(lessons=[], db_path=tmp_path / "quiz-results.db")

    assert len(rows) >= 15
    assert sum(len(row.quiz.questions) for row in rows) >= 155
    assert sum(1 for row in rows if row.quiz.time_limit_seconds == 600) >= 14
    assert all(row.status in {"Not started", "Resume", "Passed", "Needs review"} for row in rows)


def test_student_desktop_time_labels_are_readable():
    assert _time_label(0) == "No time limit"
    assert _time_label(600) == "10 min"
    assert _time_label(75) == "1m 15s"


def test_student_quiz_labels_explain_the_task_without_icon_decorations():
    questions = [
        QuizQuestion(id="q1", type="predict_output", prompt="p", correct="1", concept="loops"),
        QuizQuestion(id="q2", type="multiple_choice", prompt="p", correct="a", options=["a", "b"], concept="loops"),
        QuizQuestion(id="q3", type="true_false", prompt="p", correct="true", concept="functions"),
    ]
    quiz = Quiz(quiz_id="quiz", lesson_id="lesson", title="Quiz", questions=questions)

    assert _question_kind(questions[0]) == "Trace the code"
    assert _question_kind(questions[1]) == "Choose the best explanation"
    assert _question_kind(questions[2]) == "Check the claim"
    assert _quiz_focus(quiz) == ["loops", "functions"]
