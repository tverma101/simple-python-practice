"""Windowed student application.

The original ``ducky-gui`` entry point was a terminal menu with coloured
output.  That is useful for diagnostics, but it makes browsing a quiz bank
feel like operating a command prompt.  This module is the student-facing
desktop shell: it owns navigation and presentation while reusing the existing
offline quiz, workspace, runner, sync, and persistence services.

Tkinter is deliberately used here instead of adding a runtime dependency.
The module imports Tk lazily so the student package can still be inspected and
tested on headless machines.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import load_config, save_config
from ..quiz import (
    Quiz,
    QuizDraft,
    QuizResult,
    QuizResultsStore,
    QuizSession,
    grade_quiz,
    latest_missed_question_ids,
    load_general_quizzes,
    load_quiz_from_pack,
    ordered_questions,
    quiz_deadline_at,
    quiz_fingerprint,
    quiz_seconds_remaining,
)
from ..runner import TestRunReport, run_from_dir
from ..student_client import StudentClient, update_packs_from_server
from ..workspace import (
    get_lesson_instructions,
    get_progress,
    get_student_assignments_dir,
    list_available_lessons,
    mark_test_result,
    start_lesson,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuizCatalogItem:
    """A quiz plus the student-facing state needed by the browser."""

    quiz: Quiz
    lesson: dict[str, Any]
    teacher_assigned: bool
    status: str
    attempts: int
    best: int
    draft: bool


def _quiz_status(stats: dict[str, Any], draft: QuizDraft | None) -> str:
    if draft is not None:
        return "Resume"
    if not stats.get("attempts"):
        return "Not started"
    if stats.get("last_passed"):
        return "Passed"
    return "Needs review"


def load_quiz_catalog(
    lessons: list[dict[str, Any]] | None = None,
    *,
    db_path: Path | str | None = None,
) -> list[QuizCatalogItem]:
    """Load teacher and bundled quizzes into one stable student catalog.

    This is intentionally headless and is the contract used by the desktop
    browser. A malformed pack is skipped without hiding the healthy bundled
    practice bank.
    """

    lessons = lessons if lessons is not None else list_available_lessons()
    store = QuizResultsStore(db_path=db_path)
    rows: list[QuizCatalogItem] = []
    teacher_ids: set[str] = set()
    try:
        for lesson in lessons:
            if not lesson.get("has_quizzes"):
                continue
            try:
                pack_file = lesson.get("pack_file")
                quiz = load_quiz_from_pack(pack_file) if pack_file else None
                if quiz is None:
                    continue
                teacher_ids.add(quiz.quiz_id)
                fingerprint = quiz_fingerprint(quiz)
                stats = store.get_stats(quiz.quiz_id, fingerprint)
                draft = store.get_draft(quiz.quiz_id, fingerprint)
                rows.append(
                    QuizCatalogItem(
                        quiz=quiz,
                        lesson=dict(lesson),
                        teacher_assigned=True,
                        status=_quiz_status(stats, draft),
                        attempts=int(stats.get("attempts", 0) or 0),
                        best=int(stats.get("best", 0) or 0),
                        draft=draft is not None,
                    )
                )
            except Exception:
                logger.info("Skipping unusable teacher quiz", exc_info=True)

        for quiz in load_general_quizzes():
            if quiz.quiz_id in teacher_ids:
                continue
            try:
                fingerprint = quiz_fingerprint(quiz)
                stats = store.get_stats(quiz.quiz_id, fingerprint)
                draft = store.get_draft(quiz.quiz_id, fingerprint)
                lesson = {
                    "lesson_id": quiz.lesson_id or f"practice.{quiz.quiz_id}",
                    "title": quiz.title,
                    "status": "not_started",
                    "workspace_dir": None,
                    "pack_file": None,
                    "is_general_quiz": True,
                }
                rows.append(
                    QuizCatalogItem(
                        quiz=quiz,
                        lesson=lesson,
                        teacher_assigned=False,
                        status=_quiz_status(stats, draft),
                        attempts=int(stats.get("attempts", 0) or 0),
                        best=int(stats.get("best", 0) or 0),
                        draft=draft is not None,
                    )
                )
            except Exception:
                logger.info("Skipping unusable practice quiz %s", quiz.quiz_id, exc_info=True)
    finally:
        store.close()

    # Keep teacher assignments first, then make the practice bank predictable.
    rows.sort(key=lambda row: (not row.teacher_assigned, row.quiz.title.casefold()))
    return rows


def _time_label(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if not seconds:
        return "No time limit"
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}m {remainder}s"
    if minutes:
        return f"{minutes} min"
    return f"{remainder}s"


def _question_kind(question: Any) -> str:
    """Return a plain-language label for the work a question asks for."""
    question_type = getattr(question, "type", "")
    if question_type == "predict_output":
        return "Trace the code"
    if question_type == "true_false":
        return "Check the claim"
    if question_type == "multiple_choice":
        return "Choose the best explanation"
    return "Work the problem"


def _quiz_focus(quiz: Quiz, *, limit: int = 4) -> list[str]:
    """Return stable, de-duplicated skill labels for a quiz summary."""
    concepts: list[str] = []
    for question in quiz.questions:
        concept = str(question.concept or "").strip()
        if concept and concept.casefold() not in {item.casefold() for item in concepts}:
            concepts.append(concept)
    return concepts[:limit]


class StudentDesktopApp:
    """A simple, keyboard-friendly desktop shell for the student build."""

    BG = "#f4f7fb"
    PANEL = "#ffffff"
    NAV = "#10233f"
    NAV_MUTED = "#8fa4c2"
    TEXT = "#1d2a3a"
    MUTED = "#65758b"
    ACCENT = "#1f7a8c"
    ACCENT_DARK = "#155e6b"
    SUCCESS = "#19764a"
    WARNING = "#a05a13"
    DANGER = "#a33a3a"
    BORDER = "#dbe3ee"

    def __init__(self, root: Any | None = None, *, startup_update: dict[str, Any] | None = None):
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as exc:  # pragma: no cover - platform packaging guard
            raise RuntimeError("Ducky Student needs Python Tk support to open its window") from exc

        self.tk = tk
        self.ttk = ttk
        self.root = root or tk.Tk()
        self._owns_root = root is None
        self.root.title("Ducky Student")
        self.root.geometry("1180x780")
        self.root.minsize(900, 620)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._configure_styles()
        self._catalog: list[QuizCatalogItem] = []
        self._lessons: list[dict[str, Any]] = []
        self._selected_quiz: QuizCatalogItem | None = None
        self._selected_lesson: dict[str, Any] | None = None
        self._active_quiz: QuizCatalogItem | None = None
        self._quiz_session: QuizSession | None = None
        self._quiz_fingerprint = ""
        self._quiz_attempt_number = 1
        self._quiz_deadline = ""
        self._quiz_timer_job: Any | None = None
        self._answer_var: Any | None = None
        self._answer_text: Any | None = None
        self._quiz_question_frame: Any | None = None
        self._quiz_feedback: dict[str, dict[str, Any]] = {}
        self._quiz_feedback_answers: dict[str, str] = {}
        self._question_map_buttons: dict[str, Any] = {}
        self._quiz_feedback_frame: Any | None = None
        self._quiz_action_button: Any | None = None
        self._quiz_button_row: Any | None = None
        self._quiz_map_status: Any | None = None
        self._status_var = tk.StringVar(value="Ready")
        self._update_var = tk.StringVar(value="Checking for student updates...")
        self._startup_update = startup_update or {}
        self._build_shell()
        self.refresh_data()
        self.show_home()

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure("Ducky.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Ducky.TLabel", background=self.BG, foreground=self.TEXT, font=("Avenir Next", 11))
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED, font=("Avenir Next", 10))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Avenir Next", 25, "bold"))
        style.configure("Subtitle.TLabel", background=self.BG, foreground=self.MUTED, font=("Avenir Next", 12))
        style.configure("PanelTitle.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Avenir Next", 15, "bold"))
        style.configure("PanelText.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Avenir Next", 11))
        style.configure("PanelMuted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Avenir Next", 10))
        style.configure("Eyebrow.Panel.TLabel", background=self.PANEL, foreground=self.ACCENT, font=("Avenir Next", 9, "bold"))
        style.configure("Accent.TButton", foreground="white", background=self.ACCENT, padding=(14, 8), font=("Avenir Next", 11, "bold"))
        style.map("Accent.TButton", background=[("active", self.ACCENT_DARK)])
        style.configure("Quiet.TButton", foreground=self.TEXT, background=self.PANEL, padding=(12, 7), font=("Avenir Next", 10))
        style.configure("Ducky.TEntry", padding=7)
        style.configure("Ducky.Treeview", rowheight=34, font=("Avenir Next", 10))
        style.configure("Ducky.Treeview.Heading", font=("Avenir Next", 10, "bold"))
        style.configure("Ducky.Horizontal.TProgressbar", troughcolor="#e4ebf3", background=self.ACCENT)

    def _build_shell(self) -> None:
        tk = self.tk
        self.sidebar = tk.Frame(self.root, bg=self.NAV, width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.main = tk.Frame(self.root, bg=self.BG)
        self.main.pack(side="right", fill="both", expand=True)

        brand = tk.Frame(self.sidebar, bg=self.NAV)
        brand.pack(fill="x", padx=22, pady=(26, 30))
        tk.Label(brand, text="DUCKY", bg=self.NAV, fg="white", font=("Avenir Next", 22, "bold")).pack(anchor="w")
        tk.Label(brand, text="Student workspace", bg=self.NAV, fg=self.NAV_MUTED, font=("Avenir Next", 10)).pack(anchor="w", pady=(3, 0))

        self._nav_buttons: dict[str, Any] = {}
        for key, label, command in (
            ("home", "Home", lambda: self._navigate_from_quiz(self.show_home)),
            ("quizzes", "Quiz center", lambda: self._navigate_from_quiz(self.show_quizzes)),
            ("lessons", "Lessons", lambda: self._navigate_from_quiz(self.show_lessons)),
            ("progress", "My progress", lambda: self._navigate_from_quiz(self.show_progress)),
            ("classroom", "Classroom", lambda: self._navigate_from_quiz(self.show_classroom)),
        ):
            self._nav_buttons[key] = self._make_nav_button(label, command)

        spacer = tk.Frame(self.sidebar, bg=self.NAV)
        spacer.pack(fill="both", expand=True)
        self._make_nav_button("Settings", lambda: self._navigate_from_quiz(self.show_settings)).pack(side="bottom", fill="x", padx=14, pady=(0, 8))
        tk.Label(self.sidebar, text=f"v{__version__}", bg=self.NAV, fg=self.NAV_MUTED, font=("Avenir Next", 9)).pack(side="bottom", anchor="w", padx=24, pady=(0, 18))

        top = tk.Frame(self.main, bg=self.PANEL, height=58, highlightbackground=self.BORDER, highlightthickness=1)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, textvariable=self._update_var, bg=self.PANEL, fg=self.MUTED, font=("Avenir Next", 10)).pack(side="left", padx=24)
        tk.Label(top, textvariable=self._status_var, bg=self.PANEL, fg=self.MUTED, font=("Avenir Next", 10)).pack(side="right", padx=24)

        self.body = tk.Frame(self.main, bg=self.BG)
        self.body.pack(fill="both", expand=True, padx=30, pady=26)

        update = self._startup_update
        check = update.get("check", update)
        if update.get("updated"):
            self._update_var.set(f"Updated to v{update.get('version', '?')} | restart completed")
        elif check.get("error"):
            self._update_var.set("Cloudflare update check unavailable | working offline")
        elif check:
            self._update_var.set(f"Student app up to date | v{check.get('latest_version', __version__)}")

    def _make_nav_button(self, label: str, command: Callable[[], None]) -> Any:
        button = self.tk.Button(
            self.sidebar,
            text=label,
            command=command,
            anchor="w",
            relief="flat",
            borderwidth=0,
            bg=self.NAV,
            fg="white",
            activebackground="#1d3d67",
            activeforeground="white",
            font=("Avenir Next", 11),
            padx=22,
            pady=11,
            cursor="hand2",
        )
        button.pack(fill="x", padx=14, pady=2)
        return button

    def _set_active_nav(self, key: str) -> None:
        for name, button in self._nav_buttons.items():
            button.configure(bg="#1d3d67" if name == key else self.NAV)

    def _clear_body(self) -> None:
        if self._quiz_timer_job is not None:
            try:
                self.root.after_cancel(self._quiz_timer_job)
            except self.tk.TclError:
                pass
            self._quiz_timer_job = None
        for child in self.body.winfo_children():
            child.destroy()
        self._quiz_feedback_frame = None
        self._quiz_action_button = None
        self._quiz_button_row = None
        self._question_map_buttons = {}
        self._quiz_map_status = None

    def _navigate_from_quiz(self, command: Callable[[], None]) -> None:
        """Save an in-progress quiz before a sidebar navigation change."""
        if self._quiz_session is not None and self._quiz_question_frame is not None:
            try:
                self._save_quiz_draft()
                self._status_var.set("Quiz draft saved on this Mac.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not save quiz draft before navigation: %s", exc)
                self._status_var.set("Quiz left safely; the latest answer could not be saved.")
            self._quiz_session = None
            self._quiz_question_frame = None
            self._quiz_feedback.clear()
            self._quiz_feedback_answers.clear()
        command()

    def _title(self, title: str, subtitle: str = "") -> Any:
        header = self.ttk.Frame(self.body, style="Ducky.TFrame")
        header.pack(fill="x", pady=(0, 20))
        self.ttk.Label(header, text=title, style="Title.TLabel").pack(anchor="w")
        if subtitle:
            self.ttk.Label(header, text=subtitle, style="Subtitle.TLabel", wraplength=850).pack(anchor="w", pady=(5, 0))
        return header

    def _panel(self, parent: Any, *, padding: int = 18) -> Any:
        panel = self.ttk.Frame(parent, style="Panel.TFrame", padding=padding)
        panel.configure(borderwidth=1, relief="solid")
        return panel

    def _button_row(self, parent: Any) -> Any:
        row = self.ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=(18, 0))
        return row

    def _run_on_worker(self, work: Callable[[], Any], done: Callable[[Any, Exception | None], None]) -> None:
        def runner() -> None:
            try:
                value = work()
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda error=exc: done(None, error))
            else:
                self.root.after(0, lambda: done(value, None))

        threading.Thread(target=runner, daemon=True).start()

    def refresh_data(self) -> None:
        try:
            self._lessons = list_available_lessons()
            self._catalog = load_quiz_catalog(self._lessons)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load student data")
            self._lessons = []
            self._catalog = []
            self._status_var.set(f"Could not load local data: {exc}")

    def _sync(self) -> None:
        self._status_var.set("Syncing teacher updates...")

        def work() -> dict[str, Any]:
            client = StudentClient.from_config()
            result: dict[str, Any] = {"downloaded": 0, "updated": 0, "queued": 0, "errors": []}
            if not client.teacher_url or not client.token:
                result["message"] = "No teacher connection is configured"
                return result
            packs = client.list_packs()
            result.update(update_packs_from_server(client, packs))
            queue = client.flush_queue()
            result["queued"] = queue.get("remaining", 0)
            result["queue_sent"] = queue.get("sent", 0)
            result["queue_errors"] = queue.get("errors", [])
            return result

        def done(value: dict[str, Any] | None, error: Exception | None) -> None:
            if error:
                self._status_var.set("Offline | local work is safe")
                self._show_inline_message(f"Sync unavailable: {error}", kind="warning")
                return
            self.refresh_data()
            downloaded = int((value or {}).get("downloaded", 0) or 0)
            sent = int((value or {}).get("queue_sent", 0) or 0)
            self._status_var.set(f"Synced | {downloaded} lesson update(s), {sent} queued result(s) sent")
            self._show_inline_message("Teacher updates and queued work are up to date.", kind="success")
            self.show_home()

        self._run_on_worker(work, done)

    def _show_inline_message(self, message: str, *, kind: str = "info") -> None:
        color = self.SUCCESS if kind == "success" else self.WARNING if kind == "warning" else self.ACCENT
        self._status_var.set(message)
        self._last_message = (message, color)

    def show_home(self) -> None:
        self._set_active_nav("home")
        self._clear_body()
        config = load_config()
        name = str(config.get("student_name", "")).strip() or "there"
        self._title(f"Welcome back, {name}", "Pick up where you left off. Your quizzes, code, and progress stay on this Mac.")

        current_lesson = next(
            (
                lesson
                for lesson in self._lessons
                if str(lesson.get("status", "")).casefold() not in {"completed", "passed"}
            ),
            self._lessons[0] if self._lessons else None,
        )
        assigned_count = sum(1 for item in self._catalog if item.teacher_assigned)
        timed_count = sum(1 for item in self._catalog if item.quiz.time_limit_seconds > 0)

        upper = self.ttk.Frame(self.body, style="Ducky.TFrame")
        upper.pack(fill="x", pady=(0, 18))
        continue_panel = self._panel(upper, padding=24)
        continue_panel.pack(side="left", fill="both", expand=True, padx=(0, 14))
        self.ttk.Label(continue_panel, text="CONTINUE LEARNING", style="Eyebrow.Panel.TLabel").pack(anchor="w")
        lesson_title = current_lesson.get("title", "Choose a lesson") if current_lesson else "No lesson is open"
        self.ttk.Label(continue_panel, text=lesson_title, style="PanelTitle.TLabel", wraplength=560).pack(anchor="w", pady=(8, 0))
        if current_lesson:
            lesson_status = str(current_lesson.get("status", "not started")).replace("_", " ").title()
            detail = f"{lesson_status}. Open the assignment, edit your starter file, or run its tests."
        else:
            detail = "Sync with your teacher to receive an assignment, or start with a practice quiz."
        self.ttk.Label(continue_panel, text=detail, style="PanelMuted.TLabel", wraplength=560).pack(anchor="w", pady=(8, 18))
        self.ttk.Button(continue_panel, text="Open lessons", command=self.show_lessons, style="Accent.TButton").pack(anchor="w")

        quiz_panel = self._panel(upper, padding=24)
        quiz_panel.pack(side="right", fill="both", padx=(0, 0))
        self.ttk.Label(quiz_panel, text="QUIZ CENTER", style="Eyebrow.Panel.TLabel").pack(anchor="w")
        self.ttk.Label(quiz_panel, text=f"{len(self._catalog)} ready", style="PanelTitle.TLabel").pack(anchor="w", pady=(8, 0))
        self.ttk.Label(quiz_panel, text=f"{assigned_count} teacher-assigned | {timed_count} timed", style="PanelMuted.TLabel").pack(anchor="w", pady=(6, 18))
        self.ttk.Button(quiz_panel, text="Browse quizzes", command=self.show_quizzes, style="Quiet.TButton").pack(anchor="w")

        lower = self.ttk.Frame(self.body, style="Ducky.TFrame")
        lower.pack(fill="both", expand=True)
        progress_panel = self._panel(lower, padding=22)
        progress_panel.pack(side="left", fill="both", expand=True, padx=(0, 14))
        self.ttk.Label(progress_panel, text="Progress at a glance", style="Eyebrow.Panel.TLabel").pack(anchor="w")
        self.ttk.Label(progress_panel, text="Your course work", style="PanelTitle.TLabel").pack(anchor="w", pady=(8, 0))
        progress = get_progress()
        total = int(progress.get("total", 0) or 0)
        completed = int(progress.get("completed", 0) or 0)
        self.ttk.Label(progress_panel, text=f"{completed} of {total} lessons completed", style="PanelText.TLabel").pack(anchor="w", pady=(18, 8))
        bar = self.ttk.Progressbar(progress_panel, maximum=max(total, 1), value=completed, style="Ducky.Horizontal.TProgressbar")
        bar.pack(fill="x")
        self.ttk.Label(progress_panel, text="Assignments stay local. Sync only when you want the teacher's latest work or need to send a result.", style="PanelMuted.TLabel", wraplength=470).pack(anchor="w", pady=(16, 0))

        recent_panel = self._panel(lower, padding=22)
        recent_panel.pack(side="right", fill="both", expand=True)
        self.ttk.Label(recent_panel, text="QUIZ PULSE", style="Eyebrow.Panel.TLabel").pack(anchor="w")
        self.ttk.Label(recent_panel, text="Pick one skill to practice", style="PanelTitle.TLabel").pack(anchor="w", pady=(8, 0))
        preview = self._catalog[:3]
        if not preview:
            self.ttk.Label(recent_panel, text="No quizzes are installed yet. Sync with your teacher or reinstall the student app.", style="PanelMuted.TLabel", wraplength=390).pack(anchor="w", pady=(18, 0))
        else:
            for item in preview:
                row = self.ttk.Frame(recent_panel, style="Panel.TFrame")
                row.pack(fill="x", pady=(12, 0))
                self.ttk.Label(row, text=item.quiz.title, style="PanelText.TLabel", wraplength=260).pack(side="left", anchor="w")
                self.ttk.Label(row, text=item.status, style="PanelMuted.TLabel").pack(side="right")

        sync = self.ttk.Frame(self.body, style="Ducky.TFrame")
        sync.pack(fill="x", pady=(16, 0))
        self.ttk.Label(sync, text="Need new work?", style="Ducky.TLabel").pack(side="left")
        self.ttk.Label(sync, text="Teacher updates are checked before this window opens and can be refreshed here.", style="Muted.TLabel").pack(side="left", padx=(10, 0))
        self.ttk.Button(sync, text="Sync classroom", command=self._sync, style="Quiet.TButton").pack(side="right")

    def show_quizzes(self) -> None:
        self._set_active_nav("quizzes")
        self.refresh_data()
        self._clear_body()
        self._title("Quiz center", "Choose a skill, see what needs attention, and start with a clear next step.")

        library_note = self.ttk.Frame(self.body, style="Ducky.TFrame")
        library_note.pack(fill="x", pady=(0, 14))
        self.ttk.Label(library_note, text="FOCUSED PRACTICE", style="Muted.TLabel").pack(side="left")
        self.ttk.Label(library_note, text="Short questions use code reading and common beginner mistakes. There are no trick prompts.", style="Muted.TLabel").pack(side="left", padx=(12, 0))

        toolbar = self.ttk.Frame(self.body, style="Ducky.TFrame")
        toolbar.pack(fill="x", pady=(0, 14))
        search_var = self.tk.StringVar()
        filter_var = self.tk.StringVar(value="All quizzes")
        self.ttk.Label(toolbar, text="Search", style="Ducky.TLabel").pack(side="left")
        search = self.ttk.Entry(toolbar, textvariable=search_var, width=34, style="Ducky.TEntry")
        search.pack(side="left", padx=(8, 14))
        filter_box = self.ttk.Combobox(toolbar, textvariable=filter_var, state="readonly", width=18, values=("All quizzes", "Not started", "Resume", "Passed", "Needs review", "Teacher quizzes", "Practice quizzes"))
        filter_box.pack(side="left")
        self.ttk.Button(toolbar, text="Sync now", command=self._sync, style="Quiet.TButton").pack(side="right")

        split = self.ttk.Panedwindow(self.body, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = self._panel(split, padding=12)
        right = self._panel(split, padding=22)
        split.add(left, weight=3)
        split.add(right, weight=4)

        tree = self.ttk.Treeview(left, columns=("status", "meta"), show="tree headings", style="Ducky.Treeview", selectmode="browse")
        tree.heading("#0", text="Quiz")
        tree.heading("status", text="Status")
        tree.heading("meta", text="Details")
        tree.column("#0", width=245, anchor="w")
        tree.column("status", width=105, anchor="w")
        tree.column("meta", width=170, anchor="w")
        scroll = self.ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._quiz_tree = tree
        self._quiz_tree_items: dict[str, QuizCatalogItem] = {}
        self._quiz_detail = right

        def refill(*_args: Any) -> None:
            query = search_var.get().strip().casefold()
            selected_filter = filter_var.get()
            for child in tree.get_children():
                tree.delete(child)
            self._quiz_tree_items.clear()
            first = ""
            for index, item in enumerate(self._catalog):
                searchable = f"{item.quiz.title} {item.quiz.description} {item.quiz.lesson_id}".casefold()
                if query and query not in searchable:
                    continue
                if selected_filter in {"Not started", "Resume", "Passed", "Needs review"} and item.status != selected_filter:
                    continue
                if selected_filter == "Teacher quizzes" and not item.teacher_assigned:
                    continue
                if selected_filter == "Practice quizzes" and item.teacher_assigned:
                    continue
                iid = f"quiz-{index}"
                meta = f"{len(item.quiz.questions)} questions | {_time_label(item.quiz.time_limit_seconds)}"
                tree.insert("", "end", iid=iid, text=item.quiz.title, values=(item.status, meta))
                self._quiz_tree_items[iid] = item
                first = first or iid
            if first:
                tree.selection_set(first)
                tree.focus(first)
                self._render_quiz_detail(self._quiz_tree_items[first])
            else:
                self._render_empty_detail("No quizzes match that search", "Try a different topic or sync with your teacher.")

        search_var.trace_add("write", refill)
        filter_var.trace_add("write", refill)
        filter_box.bind("<<ComboboxSelected>>", refill)
        tree.bind("<<TreeviewSelect>>", lambda _event: self._select_quiz_tree())
        refill()

    def _select_quiz_tree(self) -> None:
        selection = self._quiz_tree.selection()
        if selection:
            item = self._quiz_tree_items.get(selection[0])
            if item:
                self._render_quiz_detail(item)

    def _render_empty_detail(self, title: str, message: str) -> None:
        for child in self._quiz_detail.winfo_children():
            child.destroy()
        self.ttk.Label(self._quiz_detail, text=title, style="PanelTitle.TLabel").pack(anchor="w")
        self.ttk.Label(self._quiz_detail, text=message, style="PanelMuted.TLabel", wraplength=430).pack(anchor="w", pady=(12, 0))

    def _render_quiz_detail(self, item: QuizCatalogItem) -> None:
        self._selected_quiz = item
        for child in self._quiz_detail.winfo_children():
            child.destroy()
        quiz = item.quiz
        tag = "Teacher assignment" if item.teacher_assigned else "Practice quiz"
        self.ttk.Label(self._quiz_detail, text=tag.upper(), style="Eyebrow.Panel.TLabel").pack(anchor="w")
        self.ttk.Label(self._quiz_detail, text=quiz.title, style="PanelTitle.TLabel", wraplength=500).pack(anchor="w", pady=(7, 0))
        self.ttk.Label(self._quiz_detail, text=quiz.description or "A focused Python practice session.", style="PanelText.TLabel", wraplength=500).pack(anchor="w", pady=(12, 0))

        facts = self.ttk.Frame(self._quiz_detail, style="Panel.TFrame")
        facts.pack(fill="x", pady=(22, 0))
        for label, value in (
            ("Questions", str(len(quiz.questions))),
            ("Time", _time_label(quiz.time_limit_seconds)),
            ("Status", item.status),
            ("Best", f"{item.best}/{len(quiz.questions)}" if item.attempts else "Not attempted"),
        ):
            fact = self.ttk.Frame(facts, style="Panel.TFrame")
            fact.pack(side="left", fill="x", expand=True)
            self.ttk.Label(fact, text=value, style="PanelTitle.TLabel").pack(anchor="w")
            self.ttk.Label(fact, text=label, style="PanelMuted.TLabel").pack(anchor="w", pady=(2, 0))

        focus = _quiz_focus(quiz)
        focus_text = ", ".join(focus) if focus else "the ideas in this lesson"
        self.ttk.Label(self._quiz_detail, text="SKILL FOCUS", style="Eyebrow.Panel.TLabel").pack(anchor="w", pady=(22, 0))
        self.ttk.Label(self._quiz_detail, text=focus_text, style="PanelText.TLabel", wraplength=500).pack(anchor="w", pady=(6, 0))
        self.ttk.Label(self._quiz_detail, text="Each question names the concept it checks. If you miss one, the result screen shows the expected answer and why it works.", style="PanelMuted.TLabel", wraplength=500).pack(anchor="w", pady=(8, 0))

        if quiz.source:
            self.ttk.Label(self._quiz_detail, text=f"Source: {quiz.source}", style="PanelMuted.TLabel", wraplength=500).pack(anchor="w", pady=(18, 0))
        if item.teacher_assigned:
            self.ttk.Label(self._quiz_detail, text="Your teacher assigned this quiz. Results can be sent when you submit.", style="PanelMuted.TLabel", wraplength=500).pack(anchor="w", pady=(12, 0))
        else:
            self.ttk.Label(self._quiz_detail, text="Practice results stay local unless you choose to connect them to a classroom.", style="PanelMuted.TLabel", wraplength=500).pack(anchor="w", pady=(12, 0))

        buttons = self._button_row(self._quiz_detail)
        self.ttk.Button(buttons, text="Resume quiz" if item.draft else "Start quiz", command=lambda: self._start_quiz(item), style="Accent.TButton").pack(side="left")
        self.ttk.Button(buttons, text="View history", command=lambda: self._show_quiz_history(item), style="Quiet.TButton").pack(side="left", padx=(10, 0))

    def _ensure_lesson_for_quiz(self, item: QuizCatalogItem) -> bool:
        lesson = item.lesson
        if lesson.get("is_general_quiz"):
            return True
        workspace = lesson.get("workspace_dir")
        if workspace and Path(workspace).is_dir():
            return True
        pack_file = lesson.get("pack_file")
        if not pack_file or not Path(pack_file).is_file():
            self._show_inline_message("This teacher quiz is waiting for its lesson pack.", kind="warning")
            return False
        try:
            lesson_dir = start_lesson(Path(pack_file), destination_root=get_student_assignments_dir())
            lesson["workspace_dir"] = lesson_dir
            self.refresh_data()
            return True
        except Exception as exc:  # noqa: BLE001
            self._show_inline_message(f"Could not prepare the lesson: {exc}", kind="warning")
            return False

    def _start_quiz(self, item: QuizCatalogItem) -> None:
        if not self._ensure_lesson_for_quiz(item):
            return
        self._active_quiz = item
        self._quiz_feedback = {}
        self._quiz_feedback_answers = {}
        quiz = item.quiz
        self._quiz_fingerprint = quiz_fingerprint(quiz)
        store = QuizResultsStore()
        try:
            attempts = store.get_attempts(quiz.quiz_id, self._quiz_fingerprint)
            draft = store.get_draft(quiz.quiz_id, self._quiz_fingerprint)
        finally:
            store.close()

        order_by_id = {question.id: question for question in quiz.questions}
        if draft and draft.question_order and all(question_id in order_by_id for question_id in draft.question_order):
            questions = [order_by_id[question_id] for question_id in draft.question_order]
            self._quiz_attempt_number = draft.attempt_number
            self._quiz_deadline = draft.deadline_at or quiz_deadline_at(quiz)
            self._quiz_session = QuizSession(questions=questions, position=min(draft.position, len(questions) - 1), answers=dict(draft.answers))
        else:
            self._quiz_attempt_number = len(attempts) + 1
            questions = ordered_questions(
                quiz,
                attempt_number=self._quiz_attempt_number,
                missed_ids=latest_missed_question_ids(quiz, attempts),
            )
            self._quiz_deadline = quiz_deadline_at(quiz)
            self._quiz_session = QuizSession(questions=questions)
        self._render_quiz_question()

    def _read_current_answer(self) -> str:
        if self._answer_text is not None:
            return self._answer_text.get("1.0", "end-1c")
        if self._answer_var is not None:
            return str(self._answer_var.get())
        return ""

    @staticmethod
    def _answer_is_missing(question: Any, answer: str) -> bool:
        # An empty prediction is meaningful: it can represent a program that
        # prints nothing. Choices and true/false questions need a selection.
        return question.type in {"multiple_choice", "true_false"} and not answer.strip()

    def _current_feedback(self) -> dict[str, Any] | None:
        if not self._quiz_session:
            return None
        question = self._quiz_session.current
        feedback = self._quiz_feedback.get(question.id)
        if feedback is None:
            return None
        if self._quiz_feedback_answers.get(question.id) != self._read_current_answer():
            return None
        return feedback

    def _answer_changed(self, *_args: Any) -> None:
        """Clear stale feedback when a student revises an answer."""
        if not self._quiz_session:
            return
        question = self._quiz_session.current
        current = self._read_current_answer()
        if self._quiz_feedback_answers.get(question.id) == current:
            return
        self._quiz_feedback.pop(question.id, None)
        self._quiz_feedback_answers.pop(question.id, None)
        self._render_live_feedback()
        self._update_quiz_action()
        self._refresh_question_map()

    def _render_question_map(self) -> None:
        if not self._quiz_session:
            return
        session = self._quiz_session
        map_panel = self._panel(self.body, padding=12)
        map_panel.pack(fill="x", pady=(0, 14))
        header = self.ttk.Frame(map_panel, style="Panel.TFrame")
        header.pack(fill="x")
        self.ttk.Label(header, text="QUESTION MAP", style="Eyebrow.Panel.TLabel").pack(side="left")
        self._quiz_map_status = self.ttk.Label(header, text="", style="PanelMuted.TLabel")
        self._quiz_map_status.pack(side="right")

        buttons = self.tk.Frame(map_panel, bg=self.PANEL)
        buttons.pack(fill="x", pady=(9, 0))
        self._question_map_buttons = {}
        for index, question in enumerate(session.questions):
            button = self.tk.Button(
                buttons,
                text=str(index + 1),
                command=lambda position=index: self._jump_to_question(position),
                relief="flat",
                borderwidth=1,
                width=3,
                padx=2,
                pady=4,
                font=("Avenir Next", 10, "bold"),
                cursor="hand2",
            )
            button.grid(row=index // 10, column=index % 10, padx=(0, 6), pady=(0, 5), sticky="w")
            self._question_map_buttons[question.id] = button

        legend = self.tk.Label(
            map_panel,
            text="Blue = current    Green = correct    Red = review    White = not checked",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Avenir Next", 9),
            anchor="w",
        )
        legend.pack(anchor="w", pady=(2, 0))
        self._refresh_question_map()

    def _refresh_question_map(self) -> None:
        if not self._quiz_session:
            return
        session = self._quiz_session
        for index, question in enumerate(session.questions):
            button = self._question_map_buttons.get(question.id)
            if button is None:
                continue
            feedback = self._quiz_feedback.get(question.id)
            if index == session.position:
                background, foreground = self.ACCENT, "white"
            elif feedback and feedback.get("correct"):
                background, foreground = "#dff3e8", self.SUCCESS
            elif feedback:
                background, foreground = "#fde7e4", self.DANGER
            elif question.id in session.answers:
                background, foreground = "#e7eef7", self.TEXT
            else:
                background, foreground = self.PANEL, self.TEXT
            button.configure(bg=background, fg=foreground, activebackground=background, activeforeground=foreground)
        if self._quiz_map_status is not None:
            checked = len(self._quiz_feedback)
            self._quiz_map_status.configure(
                text=f"{checked} checked  |  {session.answered_count}/{len(session.questions)} answered"
            )

    def _jump_to_question(self, position: int) -> None:
        if not self._quiz_session or not 0 <= position < len(self._quiz_session.questions):
            return
        if position == self._quiz_session.position:
            return
        self._capture_answer()
        try:
            self._save_quiz_draft()
        except Exception:  # noqa: BLE001
            logger.info("Could not save quiz draft while moving between questions", exc_info=True)
        self._quiz_session.position = position
        self._render_quiz_question()

    def _render_quiz_question(self) -> None:
        self._clear_body()
        if not self._active_quiz or not self._quiz_session:
            self.show_quizzes()
            return
        quiz = self._active_quiz.quiz
        session = self._quiz_session
        question = session.current
        header = self._title(
            quiz.title,
            f"Question {session.position + 1} of {len(session.questions)} | {session.answered_count} answered",
        )
        self.ttk.Button(header, text="Save and exit", command=self._save_quiz_and_exit, style="Quiet.TButton").pack(
            side="right"
        )

        top = self.ttk.Frame(self.body, style="Ducky.TFrame")
        top.pack(fill="x", pady=(0, 14))
        self.ttk.Progressbar(top, maximum=len(session.questions), value=session.position + 1, style="Ducky.Horizontal.TProgressbar").pack(side="left", fill="x", expand=True)
        timer = self.ttk.Label(top, text="", style="Ducky.TLabel", width=18, anchor="e")
        timer.pack(side="right", padx=(18, 0))
        self._quiz_timer_label = timer

        self._render_question_map()

        panel = self._panel(self.body, padding=24)
        panel.pack(fill="both", expand=True)
        self.ttk.Label(panel, text=_question_kind(question).upper(), style="Eyebrow.Panel.TLabel").pack(anchor="w")
        self.ttk.Label(panel, text=question.prompt, style="PanelTitle.TLabel", wraplength=820).pack(anchor="w")
        if question.concept:
            self.ttk.Label(panel, text=f"Skill focus: {question.concept}", style="PanelMuted.TLabel").pack(anchor="w", pady=(8, 0))
            self.ttk.Label(panel, text=f"This checks whether you can use {question.concept} in context.", style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))
        if question.code:
            self.ttk.Label(panel, text="Code to trace", style="PanelMuted.TLabel").pack(anchor="w", pady=(18, 0))
            code = self.tk.Text(panel, height=min(9, max(4, question.code.count("\n") + 2)), wrap="none", bg="#172235", fg="#edf5ff", insertbackground="white", relief="flat", padx=14, pady=12, font=("Menlo", 11))
            code.insert("1.0", question.code)
            code.configure(state="disabled")
            code.pack(fill="x", pady=(7, 0))

        self._quiz_question_frame = panel
        answer = session.answers.get(question.id, "")
        if question.type == "multiple_choice":
            self._answer_var = self.tk.StringVar(value=answer)
            choices = self.ttk.Frame(panel, style="Panel.TFrame")
            choices.pack(fill="x", pady=(20, 0))
            for index, option in enumerate(question.options):
                self.ttk.Radiobutton(choices, text=f"{chr(65 + index)}.  {option}", value=option, variable=self._answer_var).pack(anchor="w", pady=6)
            self._answer_var.trace_add("write", self._answer_changed)
            self._answer_text = None
        elif question.type == "true_false":
            self._answer_var = self.tk.StringVar(value=answer)
            choices = self.ttk.Frame(panel, style="Panel.TFrame")
            choices.pack(fill="x", pady=(20, 0))
            for option in ("true", "false"):
                self.ttk.Radiobutton(choices, text=option.title(), value=option, variable=self._answer_var).pack(side="left", padx=(0, 24))
            self._answer_var.trace_add("write", self._answer_changed)
            self._answer_text = None
        else:
            self._answer_var = None
            self.ttk.Label(panel, text="Type the output exactly as it would appear, then check your answer.", style="PanelMuted.TLabel").pack(anchor="w", pady=(20, 0))
            self._answer_text = self.tk.Text(panel, height=7, wrap="word", font=("Menlo", 11), padx=10, pady=10)
            self._answer_text.insert("1.0", answer)
            self._answer_text.pack(fill="x", pady=(7, 0))
            self._answer_text.bind("<KeyRelease>", self._answer_changed)

        self._quiz_feedback_frame = self.tk.Frame(panel, bg=self.PANEL)
        buttons = self._button_row(panel)
        self._quiz_button_row = buttons
        self.ttk.Button(buttons, text="Save and exit", command=self._save_quiz_and_exit, style="Quiet.TButton").pack(side="left")
        if session.position > 0:
            self.ttk.Button(buttons, text="Back", command=self._quiz_back, style="Quiet.TButton").pack(side="left", padx=(10, 0))
        self._quiz_action_button = self.ttk.Button(buttons, text="Check answer", command=self._quiz_next, style="Accent.TButton")
        self._quiz_action_button.pack(side="right")
        self._render_live_feedback()
        self._update_quiz_action()
        self._refresh_question_map()
        self._tick_quiz_timer()

    def _capture_answer(self) -> None:
        if not self._quiz_session:
            return
        question = self._quiz_session.current
        if self._answer_text is not None:
            value = self._answer_text.get("1.0", "end-1c")
        elif self._answer_var is not None:
            value = self._answer_var.get()
        else:
            value = ""
        self._quiz_session.answers[question.id] = value

    def _check_current_answer(self) -> bool:
        if not self._active_quiz or not self._quiz_session:
            return False
        question = self._quiz_session.current
        answer = self._read_current_answer()
        if self._answer_is_missing(question, answer):
            self._status_var.set("Choose an answer before checking.")
            return False
        self._quiz_session.answers[question.id] = answer
        result = grade_quiz(self._active_quiz.quiz, {question.id: answer}, questions=[question])
        feedback = result.per_question[0]
        self._quiz_feedback[question.id] = feedback
        self._quiz_feedback_answers[question.id] = answer
        self._render_live_feedback()
        self._update_quiz_action()
        self._refresh_question_map()
        try:
            self._save_quiz_draft()
        except Exception:  # noqa: BLE001
            logger.info("Could not save quiz draft after checking an answer", exc_info=True)
        self._status_var.set("Answer checked. Read the explanation, then continue.")
        return True

    def _render_live_feedback(self) -> None:
        frame = self._quiz_feedback_frame
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        feedback = self._current_feedback()
        if feedback is None:
            frame.pack_forget()
            return

        correct = bool(feedback.get("correct"))
        accent = self.SUCCESS if correct else self.DANGER
        background = "#effaf3" if correct else "#fff3f1"
        card = self.tk.Frame(frame, bg=background, highlightbackground=accent, highlightthickness=1, padx=16, pady=12)
        card.pack(fill="x")
        self.tk.Label(
            card,
            text="Correct — nice work" if correct else "Not quite yet",
            bg=background,
            fg=accent,
            font=("Avenir Next", 12, "bold"),
            anchor="w",
        ).pack(anchor="w")

        def add_copy(label: str, value: str, *, muted: bool = False) -> None:
            row = self.tk.Frame(card, bg=background)
            row.pack(fill="x", pady=(7, 0))
            self.tk.Label(row, text=label, bg=background, fg=self.MUTED, font=("Avenir Next", 9, "bold"), anchor="nw").pack(side="left", padx=(0, 10))
            self.tk.Label(
                row,
                text=value,
                bg=background,
                fg=self.MUTED if muted else self.TEXT,
                font=("Menlo", 10) if "answer" in label.casefold() else ("Avenir Next", 10),
                justify="left",
                anchor="w",
                wraplength=760,
            ).pack(side="left", fill="x", expand=True)

        if not correct:
            selected = str(feedback.get("selected") or "(blank)")
            correct_answer = str(feedback.get("correct_answer") or "(no answer provided)")
            add_copy("Your answer", selected, muted=True)
            add_copy("Correct answer", correct_answer)
            detail = str(feedback.get("detail") or "").strip()
            if detail and detail != f"Correct answer: {correct_answer}":
                add_copy("Grader note", detail, muted=True)
        explanation = str(feedback.get("explanation") or "").strip()
        if explanation:
            add_copy("Why this works", explanation)
        if self._quiz_button_row is not None:
            frame.pack(fill="x", pady=(16, 0), before=self._quiz_button_row)
        else:
            frame.pack(fill="x", pady=(16, 0))

    def _update_quiz_action(self) -> None:
        if self._quiz_action_button is None or not self._quiz_session:
            return
        feedback = self._current_feedback()
        if feedback is None:
            label = "Check answer"
        elif self._quiz_session.position == len(self._quiz_session.questions) - 1:
            label = "Finish quiz"
        else:
            label = "Next question"
        self._quiz_action_button.configure(text=label)

    def _save_quiz_draft(self) -> None:
        if not self._active_quiz or not self._quiz_session:
            return
        self._capture_answer()
        store = QuizResultsStore()
        try:
            store.save_draft(
                self._active_quiz.quiz.quiz_id,
                self._active_quiz.lesson.get("lesson_id", ""),
                self._quiz_fingerprint,
                self._quiz_attempt_number,
                self._quiz_session.position,
                [question.id for question in self._quiz_session.questions],
                self._quiz_session.answers,
                deadline_at=self._quiz_deadline,
            )
        finally:
            store.close()

    def _save_quiz_and_exit(self) -> None:
        try:
            self._save_quiz_draft()
            self._show_inline_message("Draft saved on this Mac.", kind="success")
        except Exception as exc:  # noqa: BLE001
            self._show_inline_message(f"Could not save draft: {exc}", kind="warning")
        self._quiz_session = None
        self._quiz_question_frame = None
        self._quiz_feedback.clear()
        self._quiz_feedback_answers.clear()
        self.show_quizzes()

    def _quiz_back(self) -> None:
        if self._quiz_session and self._quiz_session.position > 0:
            self._capture_answer()
            self._quiz_session.position -= 1
            self._render_quiz_question()

    def _quiz_next(self) -> None:
        if not self._quiz_session:
            return
        self._capture_answer()
        if self._current_feedback() is None:
            self._check_current_answer()
            return
        if self._quiz_session.position >= len(self._quiz_session.questions) - 1:
            unanswered = [
                index
                for index, question in enumerate(self._quiz_session.questions)
                if question.id not in self._quiz_feedback
            ]
            if unanswered:
                self._status_var.set("Check every question before finishing. Opening the first one still to review.")
                self._quiz_session.position = unanswered[0]
                self._render_quiz_question()
                return
            self._submit_quiz()
        else:
            self._quiz_session.position += 1
            self._render_quiz_question()

    def _tick_quiz_timer(self) -> None:
        if not self._active_quiz or not self._quiz_session:
            return
        remaining = quiz_seconds_remaining(self._quiz_deadline)
        if remaining is None:
            self._quiz_timer_label.configure(text="Untimed")
        else:
            minutes, seconds = divmod(remaining, 60)
            self._quiz_timer_label.configure(text=f"Time left  {minutes:02d}:{seconds:02d}", foreground=self.DANGER if remaining < 60 else self.TEXT)
            if remaining <= 0:
                self._capture_answer()
                self._submit_quiz(timed_out=True)
                return
        self._quiz_timer_job = self.root.after(500, self._tick_quiz_timer)

    def _submit_quiz(self, *, timed_out: bool = False) -> None:
        if not self._active_quiz or not self._quiz_session:
            return
        self._capture_answer()
        quiz = self._active_quiz.quiz
        result = grade_quiz(quiz, self._quiz_session.answers, questions=self._quiz_session.questions)
        store = QuizResultsStore()
        try:
            store.record(quiz.quiz_id, self._active_quiz.lesson.get("lesson_id", ""), result, self._quiz_session.answers, self._quiz_fingerprint)
            store.clear_draft(quiz.quiz_id, self._quiz_fingerprint)
        finally:
            store.close()
        self._quiz_session = None
        self._quiz_question_frame = None
        self._quiz_feedback.clear()
        self._quiz_feedback_answers.clear()
        self._send_quiz_result(result)
        self._show_quiz_result(result, timed_out=timed_out)

    def _send_quiz_result(self, result: QuizResult) -> None:
        item = self._active_quiz
        if item is None or not item.teacher_assigned:
            return

        def work() -> dict[str, Any]:
            client = StudentClient.from_config()
            if not client.teacher_url or not client.token:
                return {"local_only": True}
            return client.submit_quiz_result(
                item.quiz.quiz_id,
                item.lesson.get("lesson_id", ""),
                {"correct": result.correct, "total": result.total, "score_pct": result.score_pct, "passed": result.passed},
                queue_on_failure=True,
            )

        def done(value: dict[str, Any] | None, error: Exception | None) -> None:
            if error:
                self._show_inline_message(f"Result saved locally; teacher sync will retry: {error}", kind="warning")
            elif value and value.get("queued"):
                self._show_inline_message("Result saved locally and queued for the teacher.", kind="warning")
            elif value and not value.get("local_only"):
                self._show_inline_message("Result sent to your teacher.", kind="success")

        self._run_on_worker(work, done)

    def _show_quiz_result(self, result: QuizResult, *, timed_out: bool = False) -> None:
        self._clear_body()
        title = "Time is up" if timed_out else ("Quiz passed" if result.passed else "Good attempt - review and retry")
        header = self._title(title, f"You got {result.correct} of {result.total} questions correct ({result.score_pct}%).")
        self.ttk.Button(header, text="Back to quiz center", command=self.show_quizzes, style="Quiet.TButton").pack(side="right")
        panel = self._panel(self.body, padding=24)
        panel.pack(fill="both", expand=True)
        color = self.SUCCESS if result.passed else self.WARNING
        summary = self.ttk.Frame(panel, style="Panel.TFrame")
        summary.pack(fill="x")
        self.ttk.Label(summary, text=f"{result.score_pct}%", foreground=color, background=self.PANEL, font=("Avenir Next", 38, "bold")).pack(side="left")
        summary_copy = self.ttk.Frame(summary, style="Panel.TFrame")
        summary_copy.pack(side="left", padx=(18, 0), pady=(3, 0))
        self.ttk.Label(summary_copy, text="Passed" if result.passed else "Keep practicing", style="PanelTitle.TLabel").pack(anchor="w")
        self.ttk.Label(summary_copy, text=f"{result.correct} correct  |  {result.total - result.correct} to review", style="PanelMuted.TLabel").pack(anchor="w", pady=(3, 0))
        self.ttk.Label(panel, text="Review your answers", style="PanelTitle.TLabel").pack(anchor="w", pady=(20, 3))
        self.ttk.Label(panel, text="Every card shows what the grader saw and why the answer works.", style="PanelMuted.TLabel").pack(anchor="w")

        review_shell = self.ttk.Frame(panel, style="Panel.TFrame")
        review_shell.pack(fill="both", expand=True, pady=(12, 0))
        canvas = self.tk.Canvas(review_shell, bg=self.PANEL, highlightthickness=0)
        scrollbar = self.ttk.Scrollbar(review_shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        review = self.tk.Frame(canvas, bg=self.PANEL)
        window = canvas.create_window((0, 0), window=review, anchor="nw")
        review.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))

        questions = self._active_quiz.quiz.questions if self._active_quiz else []
        by_id = {question.id: question for question in questions}
        for index, detail in enumerate(result.per_question):
            question = by_id.get(detail.get("id"))
            correct = bool(detail.get("correct"))
            accent = self.SUCCESS if correct else self.DANGER
            background = "#effaf3" if correct else "#fff3f1"
            card = self.tk.Frame(review, bg=background, highlightbackground=accent, highlightthickness=1, padx=16, pady=12)
            card.pack(fill="x", pady=(0, 10))
            self.tk.Label(card, text=f"Question {index + 1}  |  {'Correct' if correct else 'Review'}", bg=background, fg=accent, font=("Avenir Next", 11, "bold"), anchor="w").pack(anchor="w")
            if question:
                self.tk.Label(card, text=question.prompt, bg=background, fg=self.TEXT, font=("Avenir Next", 11), justify="left", anchor="w", wraplength=760).pack(anchor="w", fill="x", pady=(8, 0))

            # `card` and `background` are bound as defaults rather than closed
            # over. The helper is only ever called within this iteration today,
            # so closing over them happens to work -- but the moment anyone defers
            # a call (a `command=` binding, say) every card would render with the
            # final iteration's values. Untyped on purpose: `tk` is imported
            # lazily in this module, so a `tk.Frame` annotation would name a
            # module-level name that does not exist.
            def add_review_copy(
                label: str,
                value: str,
                *,
                muted: bool = False,
                _card=card,
                _background=background,
            ) -> None:
                row = self.tk.Frame(_card, bg=_background)
                row.pack(fill="x", pady=(7, 0))
                self.tk.Label(row, text=label, bg=_background, fg=self.MUTED, font=("Avenir Next", 9, "bold"), anchor="nw").pack(side="left", padx=(0, 10))
                self.tk.Label(row, text=value, bg=_background, fg=self.MUTED if muted else self.TEXT, font=("Menlo", 10) if "answer" in label.casefold() else ("Avenir Next", 10), justify="left", anchor="w", wraplength=760).pack(side="left", fill="x", expand=True)

            if not correct:
                add_review_copy("Your answer", str(detail.get("selected") or "(blank)"), muted=True)
                add_review_copy("Correct answer", str(detail.get("correct_answer") or "(no answer provided)"))
                grader_detail = str(detail.get("detail") or "").strip()
                if grader_detail and grader_detail != f"Correct answer: {detail.get('correct_answer', '')}":
                    add_review_copy("Grader note", grader_detail, muted=True)
            explanation = str(detail.get("explanation") or "").strip()
            if explanation:
                add_review_copy("Why this works", explanation)

        buttons = self._button_row(panel)
        self.ttk.Button(buttons, text="Try again", command=lambda: self._start_quiz(self._active_quiz), style="Accent.TButton").pack(side="left")
        self.ttk.Button(buttons, text="Quiz history", command=lambda: self._show_quiz_history(self._active_quiz), style="Quiet.TButton").pack(side="left", padx=(10, 0))

    def _show_quiz_history(self, item: QuizCatalogItem | None) -> None:
        if item is None:
            return
        self._clear_body()
        self._title(f"History | {item.quiz.title}", "Attempts are stored locally so you can see improvement without an internet connection.")
        panel = self._panel(self.body)
        panel.pack(fill="both", expand=True)
        tree = self.ttk.Treeview(panel, columns=("result", "score", "date"), show="headings", style="Ducky.Treeview")
        for column, heading, width in (("result", "Result", 150), ("score", "Score", 160), ("date", "Completed", 230)):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w")
        tree.pack(fill="both", expand=True)
        store = QuizResultsStore()
        try:
            attempts = list(reversed(store.get_attempts(item.quiz.quiz_id, quiz_fingerprint(item.quiz))))
        finally:
            store.close()
        if not attempts:
            self.ttk.Label(panel, text="No completed attempts yet.", style="PanelMuted.TLabel").pack(anchor="w", pady=(12, 0))
        for attempt in attempts:
            total = int(attempt.get("total", 0) or 0)
            correct = int(attempt.get("correct", 0) or 0)
            tree.insert("", "end", values=("Passed" if attempt.get("passed") else "Needs review", f"{correct}/{total} ({round(correct / total * 100) if total else 0}%)", str(attempt.get("taken_at", "")).replace("T", " ")[:19]))
        self.ttk.Button(panel, text="Back to quiz center", command=self.show_quizzes, style="Quiet.TButton").pack(anchor="w", pady=(16, 0))

    def show_lessons(self) -> None:
        self._set_active_nav("lessons")
        self._clear_body()
        self._title("Lessons", "Open an assignment, edit your starter file, and run tests without leaving the app.")
        split = self.ttk.Panedwindow(self.body, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = self._panel(split, padding=12)
        right = self._panel(split, padding=22)
        split.add(left, weight=2)
        split.add(right, weight=4)
        tree = self.ttk.Treeview(left, columns=("status",), show="tree headings", style="Ducky.Treeview", selectmode="browse")
        tree.heading("#0", text="Assignment")
        tree.heading("status", text="Status")
        tree.column("#0", width=225, anchor="w")
        tree.column("status", width=110, anchor="w")
        tree.pack(fill="both", expand=True)
        self._lesson_tree = tree
        self._lesson_tree_items = {}
        for index, lesson in enumerate(self._lessons):
            iid = f"lesson-{index}"
            tree.insert("", "end", iid=iid, text=lesson.get("title", lesson.get("lesson_id", "Lesson")), values=(str(lesson.get("status", "not_started")).replace("_", " ").title(),))
            self._lesson_tree_items[iid] = lesson
        self._lesson_detail = right
        tree.bind("<<TreeviewSelect>>", lambda _event: self._select_lesson_tree())
        if self._lessons:
            tree.selection_set("lesson-0")
            self._render_lesson_detail(self._lessons[0])
        else:
            self._render_empty_detail_for(self._lesson_detail, "No lessons yet", "Sync with your teacher or place a lesson pack in your assignments folder.")

    def _render_empty_detail_for(self, parent: Any, title: str, message: str) -> None:
        for child in parent.winfo_children():
            child.destroy()
        self.ttk.Label(parent, text=title, style="PanelTitle.TLabel").pack(anchor="w")
        self.ttk.Label(parent, text=message, style="PanelMuted.TLabel", wraplength=520).pack(anchor="w", pady=(12, 0))

    def _select_lesson_tree(self) -> None:
        selection = self._lesson_tree.selection()
        if selection:
            lesson = self._lesson_tree_items.get(selection[0])
            if lesson:
                self._render_lesson_detail(lesson)

    def _render_lesson_detail(self, lesson: dict[str, Any]) -> None:
        self._selected_lesson = lesson
        for child in self._lesson_detail.winfo_children():
            child.destroy()
        self.ttk.Label(self._lesson_detail, text=lesson.get("title", "Lesson"), style="PanelTitle.TLabel", wraplength=520).pack(anchor="w")
        status = str(lesson.get("status", "not_started")).replace("_", " ").title()
        self.ttk.Label(self._lesson_detail, text=status, style="PanelMuted.TLabel").pack(anchor="w", pady=(6, 0))
        instructions = str(lesson.get("instructions", "") or "")
        if not instructions and lesson.get("workspace_dir"):
            instructions = get_lesson_instructions(str(lesson.get("lesson_id", ""))) or ""
        text = self.tk.Text(self._lesson_detail, height=12, wrap="word", bg=self.PANEL, fg=self.TEXT, relief="flat", font=("Avenir Next", 11), padx=2, pady=14)
        text.insert("1.0", instructions or "Start this assignment to see the teacher's instructions.")
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, pady=(16, 0))
        buttons = self._button_row(self._lesson_detail)
        self.ttk.Button(buttons, text="Start / open", command=self._start_selected_lesson, style="Accent.TButton").pack(side="left")
        if lesson.get("workspace_dir"):
            self.ttk.Button(buttons, text="Edit code", command=lambda: self._edit_lesson(lesson), style="Quiet.TButton").pack(side="left", padx=(10, 0))
            self.ttk.Button(buttons, text="Run tests", command=lambda: self._run_lesson_tests(lesson), style="Quiet.TButton").pack(side="left", padx=(10, 0))

    def _start_selected_lesson(self) -> None:
        if not self._selected_lesson:
            return
        lesson = self._selected_lesson
        try:
            pack = lesson.get("pack_file")
            if pack:
                lesson["workspace_dir"] = start_lesson(Path(pack), destination_root=get_student_assignments_dir())
                self.refresh_data()
                self._show_inline_message("Lesson ready. Your starter file is safe to edit.", kind="success")
            if lesson.get("workspace_dir"):
                self._edit_lesson(lesson)
            else:
                self._render_lesson_detail(lesson)
        except Exception as exc:  # noqa: BLE001
            self._show_inline_message(f"Could not open lesson: {exc}", kind="warning")

    def _entry_path(self, lesson: dict[str, Any]) -> Path | None:
        workspace = lesson.get("workspace_dir")
        if not workspace:
            return None
        root = Path(workspace)
        entry = str(lesson.get("entry_file", ""))
        if not entry:
            candidates = sorted(root.glob("*.py"))
            return candidates[0] if candidates else None
        path = root / entry
        return path if path.is_file() else None

    def _edit_lesson(self, lesson: dict[str, Any]) -> None:
        path = self._entry_path(lesson)
        if path is None:
            self._show_inline_message("This lesson has no editable Python starter file.", kind="warning")
            return
        self._clear_body()
        self._title(f"Edit | {lesson.get('title', 'Lesson')}", f"{path.name} | save before running tests")
        panel = self._panel(self.body, padding=14)
        panel.pack(fill="both", expand=True)
        editor = self.tk.Text(panel, wrap="none", undo=True, font=("Menlo", 12), bg="#172235", fg="#edf5ff", insertbackground="white", padx=14, pady=14)
        editor.insert("1.0", path.read_text(encoding="utf-8"))
        editor.pack(fill="both", expand=True)
        buttons = self._button_row(panel)

        def save_code() -> None:
            try:
                path.write_text(editor.get("1.0", "end-1c"), encoding="utf-8")
                self._show_inline_message("Code saved.", kind="success")
            except OSError as exc:
                self._show_inline_message(f"Could not save code: {exc}", kind="warning")

        self.ttk.Button(buttons, text="Save", command=save_code, style="Accent.TButton").pack(side="left")
        self.ttk.Button(buttons, text="Save and run tests", command=lambda: (save_code(), self._run_lesson_tests(lesson)), style="Quiet.TButton").pack(side="left", padx=(10, 0))
        self.ttk.Button(buttons, text="Back to lessons", command=self.show_lessons, style="Quiet.TButton").pack(side="right")

    def _run_lesson_tests(self, lesson: dict[str, Any]) -> None:
        workspace = lesson.get("workspace_dir")
        if not workspace or not Path(workspace).is_dir():
            self._show_inline_message("Open the lesson before running tests.", kind="warning")
            return
        self._status_var.set("Running isolated tests...")

        def work() -> TestRunReport:
            return run_from_dir(Path(workspace), timeout_seconds=8)

        def done(report: TestRunReport | None, error: Exception | None) -> None:
            if error or report is None:
                self._show_inline_message(f"Tests could not run: {error}", kind="warning")
                return
            mark_test_result(str(lesson.get("lesson_id", "")), report.passed)
            self._show_test_report(report)

        self._run_on_worker(work, done)

    def _show_test_report(self, report: TestRunReport) -> None:
        self._clear_body()
        self._title("Test results", f"{report.tests_passed} of {report.tests_total} tests passed.")
        panel = self._panel(self.body, padding=22)
        panel.pack(fill="both", expand=True)
        color = self.SUCCESS if report.passed else self.DANGER
        self.ttk.Label(panel, text="All tests passed" if report.passed else "Some tests need work", foreground=color, background=self.PANEL, font=("Avenir Next", 22, "bold")).pack(anchor="w")
        detail = self.tk.Text(panel, height=20, wrap="word", bg=self.PANEL, fg=self.TEXT, relief="flat", font=("Avenir Next", 11), padx=2, pady=18)
        if report.failures:
            failure = report.failures[0]
            detail.insert("end", f"First failure: {failure.name}\n\n")
            if failure.diagnosis:
                detail.insert("end", f"What happened: {failure.diagnosis}\n")
            if failure.expected is not None:
                detail.insert("end", f"Expected: {failure.expected}\n")
            if failure.actual is not None:
                detail.insert("end", f"Your result: {failure.actual}\n")
            if failure.hint:
                detail.insert("end", f"Try this: {failure.hint}\n")
        else:
            detail.insert("end", "The isolated test runner completed without a reported failure.")
        detail.configure(state="disabled")
        detail.pack(fill="both", expand=True)
        self.ttk.Button(panel, text="Back to lessons", command=self.show_lessons, style="Quiet.TButton").pack(anchor="w", pady=(14, 0))

    def show_progress(self) -> None:
        self._set_active_nav("progress")
        self._clear_body()
        self._title("My progress", "A private view of your lessons and quiz practice.")
        progress = get_progress()
        panel = self._panel(self.body)
        panel.pack(fill="x")
        total = int(progress.get("total", 0) or 0)
        completed = int(progress.get("completed", 0) or 0)
        self.ttk.Label(panel, text=f"{completed} / {total} lessons complete", style="PanelTitle.TLabel").pack(anchor="w")
        self.ttk.Progressbar(panel, maximum=max(1, total), value=completed, style="Ducky.Horizontal.TProgressbar").pack(fill="x", pady=(14, 0))
        quiz_panel = self._panel(self.body)
        quiz_panel.pack(fill="both", expand=True, pady=(16, 0))
        self.ttk.Label(quiz_panel, text="Quiz progress", style="PanelTitle.TLabel").pack(anchor="w")
        tree = self.ttk.Treeview(quiz_panel, columns=("status", "best", "attempts"), show="headings", style="Ducky.Treeview")
        for column, heading in (("status", "Status"), ("best", "Best"), ("attempts", "Attempts")):
            tree.heading(column, text=heading)
            tree.column(column, width=160, anchor="w")
        tree.column("status", width=330)
        tree.pack(fill="both", expand=True, pady=(14, 0))
        for item in self._catalog:
            tree.insert("", "end", values=(f"{item.quiz.title} | {item.status}", f"{item.best}/{len(item.quiz.questions)}" if item.attempts else "-", item.attempts))

    def show_classroom(self) -> None:
        self._set_active_nav("classroom")
        self._clear_body()
        self._title("Classroom", "Connect to your teacher when you are ready. Work remains available offline.")
        panel = self._panel(self.body)
        panel.pack(fill="x")
        config = load_config()
        url_var = self.tk.StringVar(value=str(config.get("teacher_url", "")))
        name_var = self.tk.StringVar(value=str(config.get("student_name", "")))
        code_var = self.tk.StringVar(value=str(config.get("teacher_access_code", "")))
        for label, variable, show in (("Teacher URL", url_var, ""), ("Your name", name_var, ""), ("Access code", code_var, "*")):
            row = self.ttk.Frame(panel, style="Panel.TFrame")
            row.pack(fill="x", pady=5)
            self.ttk.Label(row, text=label, style="PanelText.TLabel", width=18).pack(side="left")
            self.ttk.Entry(row, textvariable=variable, show=show, width=48, style="Ducky.TEntry").pack(side="left", fill="x", expand=True)
        self.ttk.Label(panel, text="Use the URL and code shown by your teacher. They are saved locally for reconnects.", style="PanelMuted.TLabel", wraplength=620).pack(anchor="w", pady=(10, 0))
        buttons = self._button_row(panel)

        def connect() -> None:
            self._status_var.set("Connecting to teacher...")

            def work() -> dict[str, Any]:
                client = StudentClient(teacher_url=url_var.get(), student_name=name_var.get())
                return client.connect(code_var.get(), name_var.get())

            def done(value: dict[str, Any] | None, error: Exception | None) -> None:
                if error:
                    self._status_var.set("Offline | check the URL and access code")
                    self._show_inline_message(str(error), kind="warning")
                    return
                save_config({"teacher_url": url_var.get(), "student_name": name_var.get(), "teacher_access_code": code_var.get()})
                self._show_inline_message("Connected. Pulling the latest lessons...", kind="success")
                self._sync()

            self._run_on_worker(work, done)

        self.ttk.Button(buttons, text="Connect and sync", command=connect, style="Accent.TButton").pack(side="left")
        self.ttk.Button(buttons, text="Sync saved connection", command=self._sync, style="Quiet.TButton").pack(side="left", padx=(10, 0))

    def show_settings(self) -> None:
        self._clear_body()
        self._title("Settings", "Keep the student app calm, local, and easy to recover.")
        panel = self._panel(self.body)
        panel.pack(fill="x")
        config = load_config()
        auto_var = self.tk.BooleanVar(value=bool(config.get("auto_update_app", True)))
        self.ttk.Checkbutton(panel, text="Check Cloudflare for student updates at launch", variable=auto_var, command=lambda: save_config({"auto_update_app": bool(auto_var.get())})).pack(anchor="w")
        self.ttk.Label(panel, text="Updates are verified against the published SHA-256 checksum and applied before the student window opens.", style="PanelMuted.TLabel", wraplength=650).pack(anchor="w", pady=(8, 0))
        self.ttk.Label(panel, text=f"Installed student version: {__version__}", style="PanelText.TLabel").pack(anchor="w", pady=(22, 0))
        self.ttk.Button(panel, text="Check now", command=self._check_update_now, style="Quiet.TButton").pack(anchor="w", pady=(14, 0))

    def _check_update_now(self) -> None:
        self._status_var.set("Checking Cloudflare...")

        def work() -> dict[str, Any]:
            from ..self_update import check_for_update

            return check_for_update()

        def done(value: dict[str, Any] | None, error: Exception | None) -> None:
            if error or not value:
                self._status_var.set("Cloudflare check failed; continuing offline")
                return
            if value.get("update_available"):
                self._update_var.set(f"Update available: v{value.get('latest_version', '?')} | restart to apply")
            elif value.get("error"):
                self._update_var.set("Cloudflare update check unavailable | working offline")
            else:
                self._update_var.set(f"Student app up to date | v{value.get('latest_version', __version__)}")
            self._status_var.set("Update check complete")

        self._run_on_worker(work, done)

    def close(self) -> None:
        if self._quiz_timer_job is not None:
            try:
                self.root.after_cancel(self._quiz_timer_job)
            except self.tk.TclError:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(*, startup_update: dict[str, Any] | None = None) -> None:
    """Launch the windowed student app."""

    app = StudentDesktopApp(startup_update=startup_update)
    app.run()
