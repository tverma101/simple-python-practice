"""Custom quiz questions for Ducky lessons.

A quiz is a small set of teacher-authored questions that rides inside a
``.duckypack`` as ``quizzes.json`` (alongside ``manifest.json``). Quizzes are
fully offline and deterministically auto-graded — no LLM is required, which
keeps Ducky's local-first / privacy-first promise.

Supported question types (v1):
  * ``multiple_choice`` — prompt + options; one correct option (exact text).
  * ``predict_output``   — show a code snippet; student predicts the printed /
                           returned output (normalized exact match).
  * ``true_false``       — a binary statement; correct is ``true``/``false``.

All grading is normalized exact match so trivial whitespace differences do not
penalize students, while genuine answer differences still fail.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Canonical question types. ``mcq`` is accepted as a friendly alias for
# ``multiple_choice`` and normalized on load/validation.
ALLOWED_TYPES = ("multiple_choice", "predict_output", "true_false")
_MCQ_ALIAS = {"mcq": "multiple_choice"}

_QUIZ_FILENAME = "quizzes.json"


# ── Models ────────────────────────────────────────────────────


@dataclass
class QuizQuestion:
    __test__ = False

    id: str
    type: str
    prompt: str
    correct: str
    options: list[str] = field(default_factory=list)
    code: str = ""
    explanation: str = ""
    concept: str = ""
    difficulty: int | None = None


@dataclass
class Quiz:
    __test__ = False

    quiz_id: str
    lesson_id: str
    title: str
    description: str = ""
    questions: list[QuizQuestion] = field(default_factory=list)
    schema_version: int = 1
    shuffle_questions: bool = False
    source: str = ""
    review_mode: str = "missed_first"
    time_limit_seconds: int = 0


@dataclass
class QuizResult:
    __test__ = False

    total: int
    correct: int
    passed: bool
    score_pct: int
    per_question: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QuizDraft:
    """A locally saved, incomplete quiz attempt."""

    quiz_id: str
    lesson_id: str
    quiz_fingerprint: str
    attempt_number: int
    position: int
    question_order: list[str]
    answers: dict[str, str]
    updated_at: str
    deadline_at: str = ""


@dataclass
class QuizSession:
    """State for a forgiving terminal/GUI quiz interaction.

    Keeping navigation state outside the renderers lets the terminal menu and
    student GUI share the same behaviour: students can go back, change an
    answer, pause, or quit without losing answers already entered.
    """

    questions: list[QuizQuestion]
    position: int = 0
    answers: dict[str, str] = field(default_factory=dict)

    @property
    def current(self) -> QuizQuestion:
        return self.questions[self.position]

    @property
    def answered_count(self) -> int:
        return sum(1 for q in self.questions if q.id in self.answers)

    @property
    def complete(self) -> bool:
        return bool(self.questions) and self.answered_count == len(self.questions)

    def answer(self, value: str) -> None:
        self.answers[self.current.id] = value
        if self.position < len(self.questions) - 1:
            self.position += 1

    def back(self) -> bool:
        if self.position == 0:
            return False
        self.position -= 1
        return True

    def next_unanswered(self) -> None:
        for index in range(self.position + 1, len(self.questions)):
            if self.questions[index].id not in self.answers:
                self.position = index
                return


def quiz_deadline_at(quiz: Quiz, *, now: datetime | None = None) -> str:
    """Return an ISO deadline for a timed quiz, or an empty string if untimed."""
    if quiz.time_limit_seconds <= 0:
        return ""
    current = now or datetime.now(timezone.utc)
    return datetime.fromtimestamp(
        current.timestamp() + quiz.time_limit_seconds, timezone.utc
    ).isoformat()


def quiz_seconds_remaining(deadline_at: str, *, now: datetime | None = None) -> int | None:
    """Return whole seconds remaining for a local quiz deadline."""
    if not deadline_at:
        return None
    try:
        try:
            deadline = datetime.fromisoformat(deadline_at).timestamp()
        except ValueError:
            # Accept the early local draft format used during development.
            deadline = float(deadline_at)
        remaining = deadline - (now or datetime.now(timezone.utc)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, int(remaining + 0.999))


def ordered_questions(
    quiz: Quiz,
    *,
    attempt_number: int = 0,
    missed_ids: Iterable[str] | None = None,
) -> list[QuizQuestion]:
    """Return a stable, optional shuffled question order.

    The seed is deterministic for a quiz attempt, so a redraw does not move a
    student’s questions unexpectedly. Teachers opt in with
    ``shuffle_questions`` in the quiz JSON. When the quiz uses the default
    ``missed_first`` review mode, IDs from the previous attempt are moved to
    the front while preserving the deterministic order within each group.
    """
    questions = list(quiz.questions)
    if quiz.shuffle_questions and len(questions) > 1:
        import random

        random.Random(f"{quiz.quiz_id}:{attempt_number}").shuffle(questions)
    if quiz.review_mode == "missed_first" and missed_ids:
        missed = set(missed_ids)
        questions = [q for q in questions if q.id in missed] + [
            q for q in questions if q.id not in missed
        ]
    return questions


# ── Normalization helpers ────────────────────────────────────


def _normalize_choice(s: str) -> str:
    """Normalize a multiple-choice answer for comparison.

    Lowercases and collapses runs of whitespace (incl. newlines) to single
    spaces, then strips. So a pasted/over-spaced or differently-cased answer
    still matches the canonical option text.
    """
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _normalize_output(s: str) -> str:
    """Normalize predicted program output for comparison.

    Strips leading/trailing blank lines and right-trims each line (ignoring
    trailing-newline / trailing-blank-line differences) while preserving
    relative indentation and line count. Internal whitespace changes are left
    intact so a genuinely different answer still fails.
    """
    if s is None:
        return ""
    text = str(s)
    # Terminal input is normally one line. Accept human-friendly ``\\n``
    # notation so multi-line output answers do not require an editor.
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    lines = text.replace("\r\n", "\n").split("\n")
    # Drop leading/trailing blank lines.
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


def _parse_bool(v: Any) -> bool | None:
    """Parse a truthy/falsy token to a bool, or None if unparseable."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    token = str(v).strip().lower()
    if token in ("true", "t", "yes", "y", "1"):
        return True
    if token in ("false", "f", "no", "n", "0"):
        return False
    return None


def _canonicalize_answer(q: QuizQuestion, answer: Any) -> str:
    """Convert a stored or interactive answer to the grader's canonical form.

    The interactive flows store MCQ option text, but older attempts and
    integrations may have stored a numeric index or option letter. Accepting
    those aliases here makes re-grading deterministic and prevents a valid
    answer from becoming wrong merely because it crossed a storage boundary.
    Exact option text wins before alias parsing, which keeps options such as
    ``"2"`` valid when they are authored as text.
    """
    if answer is None:
        return ""
    raw = str(answer)

    if q.type == "multiple_choice":
        normalized = _normalize_choice(raw)
        for option in q.options:
            if _normalize_choice(option) == normalized:
                return str(option)

        token = raw.strip()
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(q.options):
                return q.options[index]
        if len(token) == 1 and token.isalpha():
            index = ord(token.upper()) - ord("A")
            if 0 <= index < len(q.options):
                return q.options[index]
        return raw

    if q.type == "true_false":
        parsed = _parse_bool(raw)
        return "true" if parsed is True else "false" if parsed is False else raw

    # Prediction answers intentionally retain all meaningful whitespace. The
    # output comparator owns the normalization for that question type.
    return raw


def latest_missed_question_ids(
    quiz: Quiz, attempts: Iterable[dict[str, Any]] | None
) -> list[str]:
    """Return missed IDs from the most recent stored attempt.

    This is a small shared policy helper for the CLI and TUI. It re-grades the
    stored answers against the current quiz, so a quiz update cannot make the
    retry order depend on stale aggregate counts. Invalid history is ignored;
    local practice should remain usable even if an old database row is messy.
    """
    if not attempts:
        return []
    try:
        records = [attempt for attempt in attempts if isinstance(attempt, dict)]
    except TypeError:
        return []
    if not records:
        return []
    latest = max(records, key=lambda attempt: str(attempt.get("taken_at", "")))
    answers = latest.get("answers", {})
    if not isinstance(answers, dict):
        return []
    graded = grade_quiz(quiz, answers, questions=quiz.questions)
    return [row["id"] for row in graded.per_question if not row["correct"]]


# ── Parsing / validation ─────────────────────────────────────


def _coerce_type(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    t = raw.strip().lower()
    return _MCQ_ALIAS.get(t, t)


def _is_int_like(value: Any) -> bool:
    """True if ``value`` is an int (or an int-shaped string like ``"3"``)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value)
            return True
        except (TypeError, ValueError):
            return False
    return False


def validate_quiz_dict(data: Any) -> tuple[bool, list[str], list[str]]:
    """Validate a parsed quizzes.json structure.

    Returns ``(ok, errors, warnings)``. ``ok`` is True when there are no
    errors. Reused by the pack validator and the teacher ``quiz validate``
    command so a malformed quiz is caught before distribution.

    Adversarial cases handled deliberately:
      * ``correct`` present but padded with whitespace (MCQ) — matched against
        options via whitespace-collapse so it still validates.
      * expected empty output ``""`` (predict_output that prints nothing) is
        allowed and graded as empty.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        return False, ["quizzes.json root must be a JSON object"], warnings

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        errors.append("quizzes.json must contain a non-empty 'questions' list")
        return False, errors, warnings

    time_limit = data.get("time_limit_seconds", 0)
    if isinstance(time_limit, bool) or not _is_int_like(time_limit):
        errors.append("'time_limit_seconds' must be a non-negative integer")
    elif int(time_limit) < 0 or int(time_limit) > 86400:
        errors.append("'time_limit_seconds' must be between 0 and 86400")

    schema_version = data.get("schema_version", 1)
    if isinstance(schema_version, bool) or not _is_int_like(schema_version) or int(schema_version) < 1:
        errors.append("'schema_version' must be a positive integer")

    ids: set[str] = set()

    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            errors.append(f"question #{i + 1}: must be a JSON object")
            continue
        qtype = _coerce_type(q.get("type"))
        if qtype is None or qtype not in ALLOWED_TYPES:
            allowed = ", ".join(sorted(set(ALLOWED_TYPES) | set(_MCQ_ALIAS)))
            errors.append(
                f"question #{i + 1}: 'type' must be one of [{allowed}], got {q.get('type')!r}"
            )
            continue

        qid = str(q.get("id") or f"q{i + 1}").strip()
        if not qid:
            errors.append(f"question #{i + 1}: 'id' must not be empty")
        elif qid in ids:
            errors.append(f"question #{i + 1}: duplicate question id {qid!r}")
        ids.add(qid)

        prompt = q.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"question #{i + 1}: 'prompt' must be a non-empty string")

        if qtype == "multiple_choice":
            options = q.get("options")
            if not isinstance(options, list) or len(options) < 2:
                errors.append(f"question #{i + 1}: 'options' must be a list of at least 2 items")
                continue
            norm_options = [_normalize_choice(o) for o in options]
            correct = q.get("correct")
            if not isinstance(correct, str) or _normalize_choice(correct) not in norm_options:
                errors.append(
                    f"question #{i + 1}: 'correct' must exactly match one of the options"
                )

        elif qtype == "predict_output":
            if not isinstance(q.get("code"), str) or not q.get("code", "").strip():
                errors.append(f"question #{i + 1}: 'code' must be a non-empty string")
            # correct may legitimately be "" (program prints nothing).
            if "correct" not in q or not isinstance(q.get("correct"), str):
                errors.append(f"question #{i + 1}: 'correct' (expected output) must be a string")

        elif qtype == "true_false":
            if _parse_bool(q.get("correct")) is None:
                errors.append(f"question #{i + 1}: 'correct' must be true/false (got {q.get('correct')!r})")

        # `difficulty` is optional but, when present, must be an integer (or an
        # int-shaped string). A non-int like "hard" is a common authoring
        # mistake that previously crashed the whole quiz load with a raw
        # ValueError instead of a clear validation message — see parse_quiz_dict.
        difficulty = q.get("difficulty")
        if difficulty is not None:
            if isinstance(difficulty, bool) or not _is_int_like(difficulty):
                errors.append(
                    f"question #{i + 1}: 'difficulty' must be an integer "
                    f"(got {difficulty!r})"
                )

    return (len(errors) == 0), errors, warnings


def parse_quiz_dict(data: Any, *, lesson_id: str = "", quiz_id: str = "") -> Quiz:
    """Parse a validated quizzes.json structure into a :class:`Quiz`.

    Raises ``ValueError`` when validation fails (callers such as loaders catch
    this and return ``None`` so a bad quiz never breaks lesson listing).
    """
    ok, errors, _warnings = validate_quiz_dict(data)
    if not ok:
        raise ValueError("Invalid quiz: " + "; ".join(errors))

    questions: list[QuizQuestion] = []
    for i, q in enumerate(data["questions"]):
        qtype = _coerce_type(q["type"])
        correct = q.get("correct", "")
        qid = str(q.get("id") or f"q{i + 1}")
        if qtype == "true_false":
            correct = "true" if _parse_bool(correct) else "false"
        questions.append(
            QuizQuestion(
                id=qid,
                type=qtype,  # type: ignore[arg-type]
                prompt=str(q.get("prompt", "")),
                correct=str(correct),
                options=[str(o) for o in q.get("options", [])],
                code=str(q.get("code", "")),
                explanation=str(q.get("explanation", "")),
                concept=str(q.get("concept") or q.get("concept_tag") or ""),
                difficulty=(
                    int(q["difficulty"])
                    if q.get("difficulty") is not None
                    else None
                ),
            )
        )

    resolved_quiz_id = data.get("quiz_id")
    if not resolved_quiz_id:
        resolved_quiz_id = f"quiz.{lesson_id}" if lesson_id else "quiz"
    return Quiz(
        quiz_id=str(resolved_quiz_id),
        lesson_id=str(data.get("lesson_id") or lesson_id),
        title=str(data.get("title") or "Lesson Quiz"),
        description=str(data.get("description", "")),
        questions=questions,
        schema_version=int(data.get("schema_version", 1) or 1),
        shuffle_questions=bool(data.get("shuffle_questions", False)),
        source=str(data.get("source", "")),
        review_mode=str(data.get("review_mode", "missed_first")),
        time_limit_seconds=int(data.get("time_limit_seconds", 0) or 0),
    )


def quiz_to_dict(quiz: Quiz) -> dict[str, Any]:
    """Serialize a :class:`Quiz` back to a quizzes.json-shaped dict."""
    return {
        "quiz_id": quiz.quiz_id,
        "lesson_id": quiz.lesson_id,
        "title": quiz.title,
        "description": quiz.description,
        "schema_version": quiz.schema_version,
        "shuffle_questions": quiz.shuffle_questions,
        "source": quiz.source,
        "review_mode": quiz.review_mode,
        "time_limit_seconds": quiz.time_limit_seconds,
        "questions": [
            {
                "id": q.id,
                "type": q.type,
                "prompt": q.prompt,
                "options": q.options,
                "code": q.code,
                "correct": q.correct,
                "explanation": q.explanation,
                "concept": q.concept,
                "difficulty": q.difficulty,
            }
            for q in quiz.questions
        ],
    }


def quiz_fingerprint(quiz: Quiz) -> str:
    """Return a stable content fingerprint for safe local draft resuming."""
    payload = json.dumps(
        quiz_to_dict(quiz),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── Loaders ──────────────────────────────────────────────────


def load_quiz_from_pack(pack_path: str | Path) -> Quiz | None:
    """Read ``quizzes.json`` from inside a ``.duckypack``.

    Returns ``None`` when the file is absent **or** malformed — never raises
    into the caller, so a missing/broken quiz can never break lesson listing.
    """
    pack_path = Path(pack_path)
    if not pack_path.is_file():
        return None
    manifest_lesson_id = ""
    try:
        with zipfile.ZipFile(pack_path, "r") as zf:
            if _QUIZ_FILENAME not in zf.namelist():
                return None
            raw = zf.read(_QUIZ_FILENAME).decode("utf-8")
            if "manifest.json" in zf.namelist():
                try:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                    if isinstance(manifest, dict):
                        manifest_lesson_id = str(manifest.get("lesson_id") or "")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # The pack validator reports a malformed manifest; keep
                    # quiz parsing resilient so diagnostics can still inspect it.
                    pass
    except (zipfile.BadZipFile, OSError, KeyError, UnicodeDecodeError):
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    try:
        # Teacher-authored quizzes often omit redundant lesson_id/quiz_id.
        # Use the pack manifest as the stable fallback so two such quizzes do
        # not both become the generic ``quiz`` record in the student catalog.
        return parse_quiz_dict(data, lesson_id=manifest_lesson_id)
    except (TypeError, ValueError, OverflowError):
        return None


def load_quiz_from_lesson_dir(lesson_dir: str | Path) -> Quiz | None:
    """Read a lesson quiz from the canonical file or ``quizzes/`` folder."""
    lesson_dir = Path(lesson_dir)
    candidates = [lesson_dir / _QUIZ_FILENAME]
    quiz_dir = lesson_dir / "quizzes"
    if quiz_dir.is_dir():
        candidates.extend(sorted(quiz_dir.glob("*.json")))
    for quiz_file in candidates:
        if not quiz_file.is_file():
            continue
        try:
            data = json.loads(quiz_file.read_text(encoding="utf-8"))
            return parse_quiz_dict(data, lesson_id=lesson_dir.name)
        except (json.JSONDecodeError, OSError, TypeError, ValueError, OverflowError):
            continue
    return None


def _general_quiz_candidates() -> list[Path]:
    """Return platform quiz locations, ordered from local override to bundled."""
    candidates: list[Path] = []
    configured = os.environ.get("DUCKY_PLATFORM_QUIZ", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    # A teacher may place a general practice quiz in the student's repo, but
    # it is never inferred from the current lesson directory.
    candidates.extend(sorted(Path.cwd().glob("quizzes/*.json")))

    try:
        from .paths import app_dir

        candidates.extend(sorted((app_dir() / "quizzes").glob("*.json")))
    except OSError:
        pass

    try:
        from importlib.resources import files

        bundled = files("ducky").joinpath("data", "quizzes")
        candidates.extend(
            Path(str(item)) for item in bundled.iterdir() if item.name.endswith(".json")
        )
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen and candidate.is_file():
            seen.add(key)
            unique.append(candidate)
    return unique


def load_general_quiz() -> Quiz | None:
    """Load a platform practice quiz independent of the active lesson."""
    quizzes = load_general_quizzes()
    return quizzes[0] if quizzes else None


def load_general_quizzes() -> list[Quiz]:
    """Load every valid platform practice quiz in stable, local-first order.

    ``load_general_quiz`` is kept as the compatibility helper for the CLI and
    older integrations.  The student Quiz Center needs the full catalog,
    though: a fresh installation has no lesson pack to provide a row for, so
    returning only the first quiz makes the bundled practice bank effectively
    invisible.  Duplicate quiz IDs are collapsed with the first valid source
    winning, which lets a local override replace the bundled copy without
    showing the same quiz twice when the app is run from the source checkout.
    """
    quizzes: list[Quiz] = []
    seen_ids: set[str] = set()
    for quiz_file in _general_quiz_candidates():
        try:
            data = json.loads(quiz_file.read_text(encoding="utf-8"))
            quiz = parse_quiz_dict(data)
        except (json.JSONDecodeError, OSError, TypeError, ValueError, OverflowError):
            continue
        if quiz.quiz_id in seen_ids:
            continue
        seen_ids.add(quiz.quiz_id)
        quizzes.append(quiz)
    return quizzes


def load_student_quiz(lesson_dir: str | Path, pack_path: str | Path | None = None) -> tuple[Quiz | None, bool]:
    """Resolve a student quiz and report whether it is teacher-assigned.

    Lesson-specific quizzes are opt-in: only a pack whose manifest has
    ``quizzes: true`` can select the quiz embedded in that pack. Otherwise the
    student receives the platform practice quiz, which is intentionally not
    tied to the current assignment.
    """
    configured_pack = Path(pack_path) if pack_path else None
    if configured_pack and configured_pack.is_file():
        try:
            from .pack import read_manifest

            manifest = read_manifest(configured_pack)
            if manifest.quizzes:
                quiz = load_quiz_from_pack(configured_pack)
                if quiz is not None:
                    return quiz, True
                # The manifest claims a quiz but the pack has no usable
                # quizzes.json (missing or corrupt). Rather than report
                # teacher_assigned=True with a None quiz — which makes the
                # lesson menu advertise "quiz available" but leaves the student
                # at a dead-end "No quiz installed" when they open it — fall
                # back to the platform practice quiz and report it as such.
                logger.warning(
                    "Pack %s declares quizzes:true but no quiz could be loaded; "
                    "falling back to the platform practice quiz.",
                    configured_pack,
                )
        except (OSError, ValueError, zipfile.BadZipFile):
            pass

    return load_general_quiz(), False


# ── Grading ──────────────────────────────────────────────────


def grade_question(q: QuizQuestion, answer: str | None) -> tuple[bool, str]:
    """Grade a single question. Returns ``(correct, detail)``.

    Never raises for a malformed student answer — unknown tokens simply grade
    as incorrect with an explanatory detail string.
    """
    ans = _canonicalize_answer(q, answer)

    if q.type == "multiple_choice":
        ok = _normalize_choice(ans) == _normalize_choice(q.correct)
        return ok, "Correct!" if ok else f"Correct answer: {q.correct}"

    if q.type == "predict_output":
        got = _normalize_output(ans)
        exp = _normalize_output(q.correct)
        return (got == exp, "Correct!" if got == exp else f"Expected output:\n{q.correct}")

    if q.type == "true_false":
        parsed = _parse_bool(ans)
        if parsed is None:
            return False, "Please answer true or false."
        expected = _parse_bool(q.correct)
        ok = parsed == expected
        return ok, "Correct!" if ok else f"Correct answer: {q.correct}"

    # Unknown type — should be impossible after validation, but never crash.
    return False, f"Unsupported question type: {q.type}"


def grade_quiz(
    quiz: Quiz,
    answers: dict[str, str],
    *,
    questions: list[QuizQuestion] | None = None,
) -> QuizResult:
    """Grade a quiz, optionally preserving a student's displayed order."""
    per_question: list[dict[str, Any]] = []
    correct_count = 0
    ordered = questions if questions is not None else quiz.questions
    for q in ordered:
        answer = answers.get(q.id) if isinstance(answers, dict) else None
        canonical_answer = _canonicalize_answer(q, answer)
        ok, detail = grade_question(q, canonical_answer)
        if ok:
            correct_count += 1
        per_question.append(
            {
                "id": q.id,
                "type": q.type,
                "correct": ok,
                "selected": canonical_answer,
                "correct_answer": q.correct,
                # ``detail`` is the auto-grader's rationale for this question's
                # result (e.g. "Expected output:\n..." or "Correct answer: ...").
                # It is preserved so the TUI/CLI review screens can explain *why*
                # a predicted-output or true/false answer graded the way it did.
                "detail": detail,
                "explanation": q.explanation,
            }
        )

    total = len(ordered)
    passed = total > 0 and correct_count == total
    score_pct = round((correct_count / total) * 100) if total > 0 else 0
    return QuizResult(
        total=total,
        correct=correct_count,
        passed=passed,
        score_pct=score_pct,
        per_question=per_question,
    )


# ── Persistence (local results store) ───────────────────────


class QuizResultsStore:
    """Local SQLite store for student quiz attempts.

    Mirrors the threading/lock pattern of :class:`progress.ProgressTracker`.
    Resides at ``<app_dir>/quiz_results.db``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        from .paths import app_dir

        self.db_path = Path(db_path) if db_path else (app_dir() / "quiz_results.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _get_conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quiz_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        quiz_id TEXT NOT NULL,
                        lesson_id TEXT NOT NULL,
                        quiz_fingerprint TEXT NOT NULL DEFAULT '',
                        total INTEGER NOT NULL,
                        correct INTEGER NOT NULL,
                        passed INTEGER NOT NULL,
                        answers_json TEXT,
                        taken_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quiz_attempts_quiz ON quiz_attempts(quiz_id)"
                )
                columns = {
                    str(row[1])
                    for row in self._conn.execute("PRAGMA table_info(quiz_attempts)").fetchall()
                }
                if "quiz_fingerprint" not in columns:
                    self._conn.execute(
                        "ALTER TABLE quiz_attempts ADD COLUMN quiz_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quiz_attempts_fingerprint "
                    "ON quiz_attempts(quiz_id, quiz_fingerprint)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quiz_attempts_lesson ON quiz_attempts(lesson_id)"
                )
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS quiz_drafts ("
                    "quiz_id TEXT NOT NULL, quiz_fingerprint TEXT NOT NULL, "
                    "lesson_id TEXT NOT NULL, attempt_number INTEGER NOT NULL, "
                    "position INTEGER NOT NULL, question_order_json TEXT NOT NULL, "
                    "answers_json TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "deadline_at TEXT NOT NULL DEFAULT '', "
                    "PRIMARY KEY (quiz_id, quiz_fingerprint))"
                )
                columns = {
                    str(row[1])
                    for row in self._conn.execute("PRAGMA table_info(quiz_drafts)").fetchall()
                }
                if "deadline_at" not in columns:
                    self._conn.execute(
                        "ALTER TABLE quiz_drafts ADD COLUMN deadline_at TEXT NOT NULL DEFAULT ''"
                    )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quiz_drafts_updated "
                    "ON quiz_drafts(updated_at)"
                )
                self._conn.commit()
            return self._conn

    def record(
        self,
        quiz_id: str,
        lesson_id: str,
        result: QuizResult,
        answers: dict[str, str],
        quiz_fingerprint: str = "",
    ) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO quiz_attempts "
                "(quiz_id, lesson_id, quiz_fingerprint, total, correct, passed, answers_json, taken_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    quiz_id,
                    lesson_id,
                    str(quiz_fingerprint or ""),
                    result.total,
                    result.correct,
                    int(result.passed),
                    json.dumps(answers, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def save_draft(
        self,
        quiz_id: str,
        lesson_id: str,
        quiz_fingerprint: str,
        attempt_number: int,
        position: int,
        question_order: list[str],
        answers: dict[str, str],
        deadline_at: str = "",
    ) -> None:
        """Atomically save or replace one in-progress quiz draft."""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO quiz_drafts ("
                "quiz_id, quiz_fingerprint, lesson_id, attempt_number, position, "
                "question_order_json, answers_json, updated_at, deadline_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(quiz_id, quiz_fingerprint) DO UPDATE SET "
                "lesson_id = excluded.lesson_id, "
                "attempt_number = excluded.attempt_number, "
                "position = excluded.position, "
                "question_order_json = excluded.question_order_json, "
                "answers_json = excluded.answers_json, "
                "deadline_at = excluded.deadline_at, "
                "updated_at = excluded.updated_at",
                (
                    str(quiz_id),
                    str(quiz_fingerprint),
                    str(lesson_id),
                    max(1, int(attempt_number)),
                    max(0, int(position)),
                    json.dumps(list(question_order), ensure_ascii=False),
                    json.dumps(dict(answers), ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                    str(deadline_at or ""),
                ),
            )
            conn.commit()

    def get_draft(self, quiz_id: str, quiz_fingerprint: str) -> QuizDraft | None:
        """Return a valid matching draft, or None for stale/corrupt data."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT quiz_id, lesson_id, quiz_fingerprint, attempt_number, "
                "position, question_order_json, answers_json, updated_at "
                ", deadline_at "
                "FROM quiz_drafts WHERE quiz_id = ? AND quiz_fingerprint = ?",
                (str(quiz_id), str(quiz_fingerprint)),
            ).fetchone()
        if row is None:
            return None
        try:
            question_order = json.loads(row["question_order_json"])
            answers = json.loads(row["answers_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(question_order, list)
            or not all(isinstance(item, str) and item for item in question_order)
            or not isinstance(answers, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in answers.items())
        ):
            return None
        try:
            attempt_number = max(1, int(row["attempt_number"]))
            position = max(0, int(row["position"]))
        except (TypeError, ValueError):
            return None
        return QuizDraft(
            quiz_id=str(row["quiz_id"]),
            lesson_id=str(row["lesson_id"]),
            quiz_fingerprint=str(row["quiz_fingerprint"]),
            attempt_number=attempt_number,
            position=position,
            question_order=question_order,
            answers=answers,
            updated_at=str(row["updated_at"]),
            deadline_at=str(row["deadline_at"] or ""),
        )

    def clear_draft(self, quiz_id: str, quiz_fingerprint: str) -> None:
        """Remove one completed or abandoned draft."""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM quiz_drafts WHERE quiz_id = ? AND quiz_fingerprint = ?",
                (str(quiz_id), str(quiz_fingerprint)),
            )
            conn.commit()

    def get_stats(
        self, quiz_id: str, quiz_fingerprint: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            if quiz_fingerprint is None:
                rows = conn.execute(
                    "SELECT correct, total, passed, taken_at FROM quiz_attempts WHERE quiz_id = ?",
                    (quiz_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT correct, total, passed, taken_at FROM quiz_attempts "
                    "WHERE quiz_id = ? AND quiz_fingerprint = ?",
                    (quiz_id, str(quiz_fingerprint)),
                ).fetchall()
        if not rows:
            return {"attempts": 0, "best": 0, "last_correct": 0, "last_passed": False}
        best = max(r["correct"] for r in rows)
        # taken_at is stored as an ISO-8601 UTC timestamp, so lexical ordering
        # matches chronological order. Order explicitly so "last" is the most
        # recent attempt, not whichever row SQLite happens to return last.
        last = max(rows, key=lambda r: r["taken_at"])
        return {
            "attempts": len(rows),
            "best": best,
            "last_correct": last["correct"],
            "last_passed": bool(last["passed"]),
        }

    def get_lesson_stats(self, lesson_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT COUNT(*) AS cnt, SUM(passed) AS passed FROM quiz_attempts WHERE lesson_id = ?",
                (lesson_id,),
            ).fetchone()
        total = rows["cnt"] or 0
        passed = rows["passed"] or 0
        return {"attempts": total, "passed": passed, "failed": total - passed}

    def get_attempts(
        self, quiz_id: str, quiz_fingerprint: str | None = None
    ) -> list[dict[str, Any]]:
        """Return every stored attempt for ``quiz_id``, newest last.

        Each entry carries ``correct``/``total``/``passed``/``taken_at`` plus the
        re-parseable ``answers`` dict (from ``answers_json``). Callers can
        re-grade answers against the *current* quiz to surface per-question
        weak spots and auto-grading explanations in the status view — the stored
        answers are the authoritative record of what the student actually picked.
        """
        with self._lock:
            conn = self._get_conn()
            if quiz_fingerprint is None:
                rows = conn.execute(
                    "SELECT correct, total, passed, answers_json, taken_at "
                    "FROM quiz_attempts WHERE quiz_id = ? ORDER BY taken_at ASC",
                    (quiz_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT correct, total, passed, answers_json, taken_at "
                    "FROM quiz_attempts WHERE quiz_id = ? AND quiz_fingerprint = ? "
                    "ORDER BY taken_at ASC",
                    (quiz_id, str(quiz_fingerprint)),
                ).fetchall()
        attempts = []
        for r in rows:
            raw = r["answers_json"] or "{}"
            try:
                answers = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                answers = {}
            attempts.append(
                {
                    "correct": r["correct"],
                    "total": r["total"],
                    "passed": bool(r["passed"]),
                    "answers": answers if isinstance(answers, dict) else {},
                    "taken_at": r["taken_at"],
                }
            )
        return attempts

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
