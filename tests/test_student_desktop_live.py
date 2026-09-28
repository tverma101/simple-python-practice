"""Focused live Tk coverage for the student quiz interaction."""

from __future__ import annotations

import tkinter as tk

import pytest

from ducky.gui.student_desktop import QuizCatalogItem, StudentDesktopApp
from ducky.quiz import Quiz, QuizQuestion, QuizSession


def _widget_text(widget: tk.Misc) -> str:
    """Return visible text from a Tk subtree, including nested feedback cards."""
    parts: list[str] = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            parts.append(str(text))
        parts.append(_widget_text(child))
    return " ".join(part for part in parts if part)


@pytest.fixture
def live_app(monkeypatch: pytest.MonkeyPatch):
    """Create the real desktop app, skipping only when Tk cannot create."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk root unavailable: {exc}")
    root.withdraw()

    # Startup catalog loading is unrelated to this interaction contract and
    # can touch the user's local quiz database. The quiz renderer remains real.
    monkeypatch.setattr(StudentDesktopApp, "refresh_data", lambda self: None)
    monkeypatch.setattr(StudentDesktopApp, "show_home", lambda self: None)
    monkeypatch.setattr(StudentDesktopApp, "_save_quiz_draft", lambda self: None)
    app = StudentDesktopApp(root)
    app._active_quiz = QuizCatalogItem(
        quiz=Quiz(
            quiz_id="live-test",
            lesson_id="lesson",
            title="Live test quiz",
            questions=[
                QuizQuestion(
                    id="q1",
                    type="multiple_choice",
                    prompt="Which value is returned?",
                    options=["2", "3", "4"],
                    correct="3",
                    explanation="The function adds one to 2, so it returns 3.",
                ),
                QuizQuestion(
                    id="q2",
                    type="true_false",
                    prompt="A list is mutable.",
                    correct="true",
                    explanation="Lists can be changed after creation.",
                ),
            ],
        ),
        lesson={"lesson_id": "lesson", "is_general_quiz": True},
        teacher_assigned=False,
        status="Not started",
        attempts=0,
        best=0,
        draft=False,
    )
    app._quiz_session = QuizSession(app._active_quiz.quiz.questions)
    app._quiz_deadline = ""
    app._render_quiz_question()
    root.update_idletasks()
    yield app
    if app._quiz_timer_job is not None:
        try:
            root.after_cancel(app._quiz_timer_job)
        except tk.TclError:
            pass
    root.destroy()


def test_wrong_answer_shows_correct_answer_and_explanation(live_app):
    app = live_app

    app._answer_var.set("2")
    assert app._check_current_answer() is True
    app.root.update_idletasks()

    feedback = _widget_text(app._quiz_feedback_frame)
    assert "Not quite yet" in feedback
    assert "Correct answer" in feedback
    assert "3" in feedback
    assert "The function adds one to 2, so it returns 3." in feedback
    packed = app._quiz_question_frame.pack_slaves()
    assert packed.index(app._quiz_feedback_frame) < packed.index(app._quiz_button_row)


def test_revising_answer_clears_stale_live_feedback(live_app):
    app = live_app

    app._answer_var.set("2")
    app._check_current_answer()
    assert app._current_feedback() is not None

    app._answer_var.set("3")
    app.root.update_idletasks()
    assert app._current_feedback() is None
    assert _widget_text(app._quiz_feedback_frame) == ""


def test_question_map_jump_preserves_answer_and_saves_draft(live_app, monkeypatch):
    app = live_app
    saves: list[dict[str, str]] = []
    monkeypatch.setattr(app, "_save_quiz_draft", lambda: saves.append(dict(app._quiz_session.answers)))

    app._answer_var.set("3")
    app._jump_to_question(1)
    app.root.update_idletasks()

    assert app._quiz_session.position == 1
    assert app._quiz_session.answers == {"q1": "3"}
    assert saves == [{"q1": "3"}]

    app._jump_to_question(0)
    app.root.update_idletasks()
    assert app._quiz_session.position == 0
    assert app._answer_var.get() == "3"


def test_check_requires_choice_before_navigation_or_feedback(live_app):
    app = live_app

    assert app._check_current_answer() is False
    assert app._quiz_session.answers == {}
    assert app._current_feedback() is None
    assert app._status_var.get() == "Choose an answer before checking."
