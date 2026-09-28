"""Focused regressions for student quiz history, retry ordering, and exits."""

from __future__ import annotations

import tkinter as tk

import pytest

import ducky.gui.student_desktop as student_desktop
from ducky.gui.student_desktop import QuizCatalogItem, StudentDesktopApp
from ducky.quiz import Quiz, QuizQuestion, QuizSession


def _quiz() -> Quiz:
    return Quiz(
        quiz_id="history-navigation",
        lesson_id="practice.history-navigation",
        title="History navigation test",
        questions=[
            QuizQuestion(
                id="q1",
                type="multiple_choice",
                prompt="First question",
                options=["A", "B"],
                correct="A",
                explanation="A is correct.",
            ),
            QuizQuestion(
                id="q2",
                type="multiple_choice",
                prompt="Second question",
                options=["A", "B"],
                correct="B",
                explanation="B is correct.",
            ),
        ],
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk root unavailable: {exc}")
    root.withdraw()
    monkeypatch.setattr(StudentDesktopApp, "refresh_data", lambda self: None)
    monkeypatch.setattr(StudentDesktopApp, "show_home", lambda self: None)
    instance = StudentDesktopApp(root)
    item = QuizCatalogItem(
        quiz=_quiz(),
        lesson={"lesson_id": "practice.history-navigation", "is_general_quiz": True},
        teacher_assigned=False,
        status="Not started",
        attempts=0,
        best=0,
        draft=False,
    )
    instance._active_quiz = item
    instance._quiz_session = QuizSession(item.quiz.questions)
    instance._quiz_deadline = ""
    instance._render_quiz_question()
    root.update_idletasks()
    yield instance, item
    if instance._quiz_timer_job is not None:
        try:
            root.after_cancel(instance._quiz_timer_job)
        except tk.TclError:
            pass
    root.destroy()


def _widget_text(widget: tk.Misc) -> str:
    values: list[str] = []
    for child in widget.winfo_children():
        try:
            value = child.cget("text")
        except tk.TclError:
            value = ""
        if value:
            values.append(str(value))
        values.append(_widget_text(child))
    return " ".join(value for value in values if value)


def _find_tree(widget: tk.Misc) -> tk.Misc | None:
    if isinstance(widget, tk.ttk.Treeview):
        return widget
    for child in widget.winfo_children():
        found = _find_tree(child)
        if found is not None:
            return found
    return None


def test_history_view_is_visible_and_lists_attempt_score(app, monkeypatch):
    instance, item = app

    class FakeStore:
        def get_attempts(self, quiz_id, fingerprint):
            assert quiz_id == item.quiz.quiz_id
            assert fingerprint
            return [{"passed": True, "correct": 2, "total": 2, "taken_at": "2026-08-12T12:34:56"}]

        def close(self):
            pass

    monkeypatch.setattr(student_desktop, "QuizResultsStore", FakeStore)
    instance._show_quiz_history(item)
    instance.root.update_idletasks()

    visible = _widget_text(instance.body)
    assert "History | History navigation test" in visible
    history_tree = _find_tree(instance.body)
    assert history_tree is not None
    assert isinstance(history_tree, tk.ttk.Treeview)
    assert history_tree.item(history_tree.get_children()[0], "values") == (
        "Passed",
        "2/2 (100%)",
        "2026-08-12 12:34:56",
    )


def test_retry_starts_with_questions_missed_on_latest_attempt(app, monkeypatch):
    instance, item = app

    class FakeStore:
        def get_attempts(self, quiz_id, fingerprint):
            return [{
                "answers": {"q1": "A", "q2": "A"},
                "per_question": [
                    {"id": "q1", "correct": True},
                    {"id": "q2", "correct": False},
                ],
            }]

        def get_draft(self, quiz_id, fingerprint):
            return None

        def close(self):
            pass

    rendered: list[list[str]] = []
    monkeypatch.setattr(student_desktop, "QuizResultsStore", FakeStore)
    monkeypatch.setattr(instance, "_render_quiz_question", lambda: rendered.append([q.id for q in instance._quiz_session.questions]))
    instance._start_quiz(item)

    assert rendered == [["q2", "q1"]]
    assert instance._quiz_attempt_number == 2


def test_sidebar_navigation_saves_answered_draft_before_leaving(app, monkeypatch):
    instance, _item = app
    saved: list[dict[str, str]] = []
    destination: list[bool] = []

    class FakeStore:
        def save_draft(self, *args, **kwargs):
            saved.append(dict(args[6]))

        def close(self):
            pass

    monkeypatch.setattr(student_desktop, "QuizResultsStore", FakeStore)

    instance._answer_var.set("A")
    # The test fixture does not need the full window shell; mark the rendered
    # question panel as active so the sidebar guard exercises its save path.
    assert instance._quiz_question_frame is not None
    instance._navigate_from_quiz(lambda: destination.append(True))

    assert saved == [{"q1": "A"}]
    assert destination == [True]
    assert instance._quiz_session is None
