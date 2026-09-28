"""Tests for custom quiz questions (quiz.py) and pack integration."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ducky.pack import DuckyManifest, create_duckypack
from ducky.quiz import (
    QuizDraft,
    QuizQuestion,
    QuizResultsStore,
    grade_question,
    grade_quiz,
    latest_missed_question_ids,
    load_general_quizzes,
    load_quiz_from_lesson_dir,
    load_quiz_from_pack,
    ordered_questions,
    parse_quiz_dict,
    quiz_deadline_at,
    quiz_fingerprint,
    quiz_seconds_remaining,
    quiz_to_dict,
    validate_quiz_dict,
)

# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def quiz_dict() -> dict:
    return {
        "quiz_id": "quiz.rv",
        "lesson_id": "python_basics.lesson_01_return_values",
        "title": "Return Values Quiz",
        "description": "Check understanding.",
        "questions": [
            {
                "id": "q1",
                "type": "multiple_choice",
                "prompt": "What does a function return by default?",
                "options": ["None", "0", "True"],
                "correct": "None",
            },
            {
                "id": "q2",
                "type": "true_false",
                "prompt": "A function must always return a value.",
                "correct": "false",
            },
            {
                "id": "q3",
                "type": "predict_output",
                "prompt": "What prints?",
                "code": "print(1 + 1)",
                "correct": "2",
            },
        ],
    }


@pytest.fixture
def tmp_pack(tmp_path: Path) -> Path:
    """Build a .duckypack that bundles a quizzes.json."""
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "lesson_01.py").write_text("def f(x):\n    return x\n")
    (tests / "test_lesson_01.py").write_text(
        "import unittest\nfrom lesson_01 import f\n"
        "class T(unittest.TestCase):\n"
        "    def test_f(self):\n        self.assertEqual(f(1), 1)\n"
    )
    quiz_file = tmp_path / "quizzes.json"
    quiz_file.write_text(json.dumps(quiz_dict_fixture_data()))
    manifest = DuckyManifest(
        lesson_id="python_basics.lesson_01_return_values",
        title="Return Values",
        entry_file="lesson_01.py",
    )
    out = tmp_path / "lesson.duckypack"
    create_duckypack(starter, tests, out, manifest, quizzes_path=quiz_file)
    return out


def quiz_dict_fixture_data() -> dict:
    return {
        "quiz_id": "quiz.rv",
        "lesson_id": "python_basics.lesson_01_return_values",
        "title": "Return Values Quiz",
        "questions": [
            {
                "id": "q1",
                "type": "multiple_choice",
                "prompt": "What does a function return by default?",
                "options": ["None", "0", "True"],
                "correct": "None",
            },
            {
                "id": "q2",
                "type": "true_false",
                "prompt": "A function must always return a value.",
                "correct": "false",
            },
            {
                "id": "q3",
                "type": "predict_output",
                "prompt": "What prints?",
                "code": "print(1 + 1)",
                "correct": "2",
            },
        ],
    }


# ── Validation ───────────────────────────────────────────────


def test_validate_quiz_dict_valid(quiz_dict):
    ok, errors, warnings = validate_quiz_dict(quiz_dict)
    assert ok is True
    assert errors == []


def test_validate_quiz_dict_empty_questions():
    ok, errors, _ = validate_quiz_dict({"questions": []})
    assert ok is False
    assert any("non-empty" in e for e in errors)


def test_validate_quiz_dict_missing_type():
    bad = {"questions": [{"prompt": "x", "correct": "y", "options": ["y"]}]}
    ok, errors, _ = validate_quiz_dict(bad)
    assert ok is False
    assert any("type" in e for e in errors)


def test_validate_quiz_dict_mcq_correct_not_in_options():
    bad = {
        "questions": [
            {
                "type": "multiple_choice",
                "prompt": "p",
                "options": ["a", "b"],
                "correct": "c",
            }
        ]
    }
    ok, errors, _ = validate_quiz_dict(bad)
    assert ok is False
    assert any("correct" in e for e in errors)


def test_validate_quiz_dict_true_false_bad_correct():
    bad = {
        "questions": [
            {"type": "true_false", "prompt": "p", "correct": "maybe"}
        ]
    }
    ok, errors, _ = validate_quiz_dict(bad)
    assert ok is False
    assert any("true/false" in e for e in errors)


def test_validate_quiz_dict_predict_output_empty_correct_allowed():
    # A program that prints nothing is a legitimately valid quiz.
    data = {
        "questions": [
            {
                "type": "predict_output",
                "prompt": "What prints?",
                "code": "pass",
                "correct": "",
            }
        ]
    }
    ok, errors, _ = validate_quiz_dict(data)
    assert ok is True
    assert errors == []


def test_validate_quiz_dict_mcq_alias_mcq_normalized():
    data = {
        "questions": [
            {
                "type": "mcq",
                "prompt": "p",
                "options": ["a", "b"],
                "correct": "a",
            }
        ]
    }
    ok, errors, _ = validate_quiz_dict(data)
    assert ok is True


def test_validate_quiz_dict_difficulty_must_be_int():
    """A non-integer ``difficulty`` (e.g. ``"hard"``) must be rejected with a
    clear message, not crash the quiz loader. Regression guard for the raw
    ``ValueError: invalid literal for int()`` that previously broke the whole
    quiz when a teacher authored difficulty as text."""
    data = {
        "questions": [
            {
                "type": "true_false",
                "prompt": "p",
                "correct": "true",
                "difficulty": "hard",
            }
        ]
    }
    ok, errors, _ = validate_quiz_dict(data)
    assert ok is False
    assert any("difficulty" in e for e in errors)

    # A valid integer (or int-shaped string) difficulty must still pass.
    data_ok = {
        "questions": [
            {
                "type": "true_false",
                "prompt": "p",
                "correct": "true",
                "difficulty": "3",
            }
        ]
    }
    ok2, errors2, _ = validate_quiz_dict(data_ok)
    assert ok2 is True
    assert errors2 == []


def test_parse_quiz_dict_non_int_difficulty_raises_clear_valueerror():
    """Even if validation is skipped, parse_quiz_dict must raise a readable
    ValueError (caught upstream to return None) rather than a raw int() crash
    with an opaque message."""
    data = {"questions": [{"type": "true_false", "prompt": "p", "correct": "true", "difficulty": "hard"}]}
    with pytest.raises(ValueError) as exc:
        parse_quiz_dict(data)
    assert "difficulty" in str(exc.value).lower()


# ── Grading: adversarial normalization ────────────────────────


def test_grade_mcq_exact():
    q = QuizQuestion(id="q1", type="multiple_choice", prompt="p", correct="None", options=["None", "0"])
    ok, _ = grade_question(q, "None")
    assert ok is True


def test_grade_mcq_case_insensitive():
    q = QuizQuestion(id="q1", type="multiple_choice", prompt="p", correct="Returns", options=["Returns", "Prints"])
    ok, _ = grade_question(q, "returns")
    assert ok is True


def test_grade_mcq_whitespace_insensitive():
    q = QuizQuestion(id="q1", type="multiple_choice", prompt="p", correct="Returns", options=["Returns", "Prints"])
    ok, _ = grade_question(q, "  Returns  ")
    assert ok is True


def test_grade_mcq_wrong_option_fails():
    q = QuizQuestion(id="q1", type="multiple_choice", prompt="p", correct="None", options=["None", "0"])
    ok, detail = grade_question(q, "0")
    assert ok is False
    assert "None" in detail


def test_grade_mcq_answer_not_an_option_fails():
    q = QuizQuestion(id="q1", type="multiple_choice", prompt="p", correct="None", options=["None", "0"])
    ok, _ = grade_question(q, "something else")
    assert ok is False


def test_grade_true_false_token_variants():
    q = QuizQuestion(id="q2", type="true_false", prompt="p", correct="false")
    for tok in ("false", "False", "FALSE", "no", "n", "0", "f"):
        ok, _ = grade_question(q, tok)
        assert ok is True, tok
    for tok in ("true", "yes", "y", "1", "t"):
        ok, _ = grade_question(q, tok)
        assert ok is False, tok


def test_grade_true_false_garbage_token_fails_not_crashes():
    q = QuizQuestion(id="q2", type="true_false", prompt="p", correct="true")
    ok, detail = grade_question(q, "garbage")
    assert ok is False
    assert "true or false" in detail


def test_grade_predict_exact():
    q = QuizQuestion(id="q3", type="predict_output", prompt="p", code="print(1)", correct="1")
    ok, _ = grade_question(q, "1")
    assert ok is True


def test_grade_predict_trailing_newline_ignored():
    q = QuizQuestion(id="q3", type="predict_output", prompt="p", code="print('a')", correct="a\n")
    ok, _ = grade_question(q, "a")
    assert ok is True


def test_grade_predict_internal_whitespace_change_fails():
    # Genuine difference in output must still fail.
    q = QuizQuestion(id="q3", type="predict_output", prompt="p", code="print('x y')", correct="x y")
    ok, _ = grade_question(q, "x  y")  # two spaces vs one
    assert ok is False


def test_grade_predict_empty_expected_output():
    q = QuizQuestion(id="q3", type="predict_output", prompt="p", code="pass", correct="")
    ok, _ = grade_question(q, "")
    assert ok is True
    ok2, _ = grade_question(q, "anything")
    assert ok2 is False


def test_grade_predict_preserves_indentation_and_linecount():
    q = QuizQuestion(
        id="q3",
        type="predict_output",
        prompt="p",
        code="print('a')",
        correct="line1\n    indented\nline3",
    )
    ok, _ = grade_question(q, "line1\n    indented\nline3")
    assert ok is True
    ok2, _ = grade_question(q, "line1\nindented\nline3")
    assert ok2 is False


def test_grade_mcq_accepts_legacy_numeric_and_letter_answers():
    q = QuizQuestion(
        id="q1",
        type="multiple_choice",
        prompt="p",
        correct="bravo",
        options=["alpha", "bravo", "charlie"],
    )

    assert grade_question(q, "2")[0] is True
    assert grade_question(q, "B")[0] is True


def test_grade_quiz_reports_canonical_selected_answer():
    quiz = parse_quiz_dict(
        {
            "questions": [
                {
                    "id": "q1",
                    "type": "multiple_choice",
                    "prompt": "p",
                    "options": ["alpha", "bravo"],
                    "correct": "bravo",
                }
            ]
        }
    )

    result = grade_quiz(quiz, {"q1": "2"})

    assert result.passed is True
    assert result.per_question[0]["selected"] == "bravo"


def test_missed_first_order_uses_latest_attempt():
    quiz = parse_quiz_dict(
        {
            "quiz_id": "quiz.review",
            "questions": [
                {
                    "id": "q1",
                    "type": "true_false",
                    "prompt": "p1",
                    "correct": "true",
                },
                {
                    "id": "q2",
                    "type": "true_false",
                    "prompt": "p2",
                    "correct": "false",
                },
                {
                    "id": "q3",
                    "type": "true_false",
                    "prompt": "p3",
                    "correct": "true",
                },
            ],
        }
    )
    attempts = [
        {
            "answers": {"q1": "false", "q2": "false", "q3": "true"},
            "taken_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "answers": {"q1": "true", "q2": "true", "q3": "true"},
            "taken_at": "2026-01-02T00:00:00+00:00",
        },
    ]

    missed = latest_missed_question_ids(quiz, attempts)
    ordered = ordered_questions(quiz, attempt_number=2, missed_ids=missed)

    assert missed == ["q2"]
    assert [question.id for question in ordered] == ["q2", "q1", "q3"]


# ── Full quiz grading ────────────────────────────────────────


def test_grade_quiz_all_correct(quiz_dict):
    quiz = parse_quiz_dict(quiz_dict)
    answers = {"q1": "None", "q2": "false", "q3": "2"}
    result = grade_quiz(quiz, answers)
    assert result.total == 3
    assert result.correct == 3
    assert result.passed is True
    assert result.score_pct == 100


def test_grade_quiz_partial(quiz_dict):
    quiz = parse_quiz_dict(quiz_dict)
    answers = {"q1": "None", "q2": "true", "q3": "2"}
    result = grade_quiz(quiz, answers)
    assert result.correct == 2
    assert result.passed is False
    assert result.score_pct == 67


def test_grade_quiz_missing_answer_grades_incorrect(quiz_dict):
    quiz = parse_quiz_dict(quiz_dict)
    result = grade_quiz(quiz, {})
    assert result.correct == 0
    assert result.passed is False


def test_grade_quiz_preserves_grader_detail(quiz_dict):
    """The auto-grader's rationale (``detail``) must be carried into the result
    so the TUI/CLI review screens can explain *why* an answer graded wrong —
    it was previously discarded."""
    quiz = parse_quiz_dict(quiz_dict)
    # q3 is predict_output; the wrong answer should yield the "Expected output" detail.
    result = grade_quiz(quiz, {"q1": "None", "q2": "false", "q3": "3"})
    pq3 = next(pq for pq in result.per_question if pq["id"] == "q3")
    assert pq3["correct"] is False
    assert pq3["detail"].startswith("Expected output")
    assert "2" in pq3["detail"]

    # q2 true/false wrong -> "Correct answer: false" detail.
    pq2 = next(pq for pq in result.per_question if pq["id"] == "q2")
    assert pq2["correct"] is True  # q2 was answered correctly here
    # A genuinely wrong true/false should carry the correct-answer detail.
    wrong = grade_quiz(quiz, {"q1": "None", "q2": "true", "q3": "2"})
    wrong_q2 = next(pq for pq in wrong.per_question if pq["id"] == "q2")
    assert wrong_q2["correct"] is False
    assert wrong_q2["detail"] == "Correct answer: false"


def test_parse_quiz_dict_mcq_alias_normalized():
    data = {
        "questions": [
            {"type": "mcq", "prompt": "p", "options": ["a", "b"], "correct": "a"}
        ]
    }
    quiz = parse_quiz_dict(data, lesson_id="rv")
    assert quiz.questions[0].type == "multiple_choice"


def test_parse_quiz_dict_invalid_raises():
    with pytest.raises(ValueError):
        parse_quiz_dict({"questions": [{"type": "bogus", "prompt": "x", "correct": "y"}]})


# ── Loaders ──────────────────────────────────────────────────


def test_load_quiz_from_pack_roundtrip(tmp_pack):
    quiz = load_quiz_from_pack(tmp_pack)
    assert quiz is not None
    assert quiz.quiz_id == "quiz.rv"
    assert len(quiz.questions) == 3


def test_pack_quiz_without_id_uses_manifest_lesson_id(tmp_path):
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "lesson.py").write_text("def f():\n    return 1\n")
    (tests / "test_lesson.py").write_text("def test_f(): assert True\n")
    quiz_file = tmp_path / "quizzes.json"
    quiz_file.write_text(
        json.dumps(
            {
                "title": "No explicit ID",
                "questions": [
                    {"type": "true_false", "prompt": "One?", "correct": "true"}
                ],
            }
        )
    )
    pack = tmp_path / "lesson.duckypack"
    create_duckypack(
        starter,
        tests,
        pack,
        DuckyManifest(lesson_id="manifest.lesson", title="Lesson", entry_file="lesson.py"),
        quizzes_path=quiz_file,
    )

    quiz = load_quiz_from_pack(pack)
    assert quiz is not None
    assert quiz.quiz_id == "quiz.manifest.lesson"
    assert quiz.lesson_id == "manifest.lesson"


def test_load_quiz_from_pack_absent(tmp_path):
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "a.py").write_text("x=1\n")
    (tests / "test_a.py").write_text("def test_a(): assert True\n")
    out = tmp_path / "noquiz.duckypack"
    create_duckypack(starter, tests, out, DuckyManifest(lesson_id="x", title="t", entry_file="a.py"))
    assert load_quiz_from_pack(out) is None


def test_load_quiz_from_pack_malformed_returns_none(tmp_path):
    # Build a pack, then overwrite quizzes.json with invalid JSON via repack.
    starter = tmp_path / "starter"
    starter.mkdir()
    (starter / "a.py").write_text("x=1\n")
    out = tmp_path / "bad.duckypack"
    create_duckypack(starter, starter, out, DuckyManifest(lesson_id="x", title="t", entry_file="a.py"))
    # Inject a malformed quizzes.json
    with zipfile.ZipFile(out, "a") as zf:
        zf.writestr("quizzes.json", "{not valid json")
    assert load_quiz_from_pack(out) is None


def test_general_quiz_catalog_skips_structurally_malformed_override(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": [],
                "questions": [
                    {"type": "true_false", "prompt": "broken", "correct": "true"}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DUCKY_PLATFORM_QUIZ", str(bad))
    quizzes = load_general_quizzes()
    assert quizzes


def test_load_quiz_from_lesson_dir(tmp_path):
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()
    (lesson_dir / "quizzes.json").write_text(json.dumps(quiz_dict_fixture_data()))
    quiz = load_quiz_from_lesson_dir(lesson_dir)
    assert quiz is not None
    assert quiz.title == "Return Values Quiz"


def test_load_student_quiz_falls_back_when_manifest_claims_quizzes_but_missing(tmp_path):
    """Regression: a pack whose manifest declares ``quizzes: true`` but ships
    no usable quizzes.json must NOT return ``(None, True)``. That combination
    advertises "quiz available" in the lesson menu yet dead-ends the student at
    "No quiz installed". It must fall back to the practice quiz and report
    teacher_assigned=False."""
    from ducky.pack import DuckyManifest, create_duckypack
    from ducky.quiz import load_student_quiz

    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "a.py").write_text("x=1\n")
    (tests / "test_a.py").write_text("def test_a(): assert True\n")
    # Manifest claims quizzes:True but we deliberately pass NO quizzes_path.
    out = tmp_path / "claimed.duckypack"
    create_duckypack(
        starter,
        tests,
        out,
        DuckyManifest(lesson_id="x", title="t", entry_file="a.py", quizzes=True),
    )
    quiz, assigned = load_student_quiz(tmp_path, out)
    # Assigned must be False so the menu does not advertise an untakeable quiz.
    assert assigned is False
    # And a quiz is still returned (the platform practice quiz, if one exists;
    # if not, the behavior is at least consistent: None paired with False).
    assert not (quiz is None and assigned is True)


def test_load_quiz_from_lesson_dir_absent(tmp_path):
    lesson_dir = tmp_path / "empty"
    lesson_dir.mkdir()
    assert load_quiz_from_lesson_dir(lesson_dir) is None


# ── Persistence ───────────────────────────────────────────────


def test_quiz_results_store_record_and_stats(tmp_path):
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        quiz = parse_quiz_dict(quiz_dict_fixture_data())
        full = grade_quiz(quiz, {"q1": "None", "q2": "false", "q3": "2"})
        partial = grade_quiz(quiz, {"q1": "None", "q2": "true", "q3": "2"})
        store.record(quiz.quiz_id, quiz.lesson_id, full, {})
        store.record(quiz.quiz_id, quiz.lesson_id, partial, {})

        stats = store.get_stats(quiz.quiz_id)
        assert stats["attempts"] == 2
        assert stats["best"] == 3
        assert stats["last_correct"] == 2
        assert stats["last_passed"] is False

        lesson_stats = store.get_lesson_stats(quiz.lesson_id)
        assert lesson_stats["attempts"] == 2
        assert lesson_stats["passed"] == 1
        assert lesson_stats["failed"] == 1
    finally:
        store.close()


def test_quiz_results_store_separates_edited_quiz_versions(tmp_path, quiz_dict):
    """A teacher edit must not make the student see stale scores as current."""
    quiz = parse_quiz_dict(quiz_dict)
    result = grade_quiz(quiz, {"q1": "None", "q2": "false", "q3": "2"})
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        store.record(
            quiz.quiz_id,
            quiz.lesson_id,
            result,
            {},
            quiz_fingerprint="old-version",
        )
        store.record(
            quiz.quiz_id,
            quiz.lesson_id,
            result,
            {},
            quiz_fingerprint="new-version",
        )
        assert store.get_stats(quiz.quiz_id, "old-version")["attempts"] == 1
        assert store.get_stats(quiz.quiz_id, "new-version")["attempts"] == 1
        assert len(store.get_attempts(quiz.quiz_id, "new-version")) == 1
        assert store.get_stats(quiz.quiz_id, "missing-version")["attempts"] == 0
    finally:
        store.close()


def test_quiz_results_store_get_attempts_returns_answers_and_order(tmp_path):
    """get_attempts must return stored attempts with re-parseable answers in
    chronological (oldest-first) order, so the status view can re-grade weak
    spots against the current quiz."""
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        quiz = parse_quiz_dict(quiz_dict_fixture_data())
        full = grade_quiz(quiz, {"q1": "None", "q2": "false", "q3": "2"})
        partial = grade_quiz(quiz, {"q1": "None", "q2": "true", "q3": "2"})
        store.record(quiz.quiz_id, quiz.lesson_id, full, {"q1": "None", "q2": "false", "q3": "2"})
        store.record(quiz.quiz_id, quiz.lesson_id, partial, {"q1": "None", "q2": "true", "q3": "2"})

        attempts = store.get_attempts(quiz.quiz_id)
        assert len(attempts) == 2
        # Oldest first; answers are preserved verbatim.
        assert attempts[0]["answers"] == {"q1": "None", "q2": "false", "q3": "2"}
        assert attempts[1]["answers"] == {"q1": "None", "q2": "true", "q3": "2"}
        assert attempts[0]["correct"] == 3
        assert attempts[1]["correct"] == 2

        # Empty store returns empty list (not None / not a crash).
        assert store.get_attempts("never.taken") == []
    finally:
        store.close()


def test_quiz_results_store_last_is_most_recent_not_insertion_order(tmp_path):
    """get_stats must treat the chronologically-newest attempt as 'last',
    independent of physical row/insertion order. A fix that merely reads
    ``rows[-1]`` passes the simple in-order case but is defeated when an
    out-of-order row (e.g. a future-dated or imported attempt) is inserted
    before the real latest one.
    """
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        quiz = parse_quiz_dict(quiz_dict_fixture_data())
        # Insert an attempt with a FUTURE timestamp first (models an imported /
        # out-of-order-synced record landing before the latest local attempt).
        with store._lock:
            conn = store._get_conn()
            conn.execute(
                "INSERT INTO quiz_attempts "
                "(quiz_id, lesson_id, total, correct, passed, answers_json, taken_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (quiz.quiz_id, quiz.lesson_id, 3, 3, 1, "{}", "2030-01-01T00:00:00+00:00"),
            )
            conn.commit()
        # Then record a fresh attempt now (earlier than 2030).
        partial = grade_quiz(quiz, {"q1": "None", "q2": "true", "q3": "2"})
        store.record(quiz.quiz_id, quiz.lesson_id, partial, {})

        stats = store.get_stats(quiz.quiz_id)
        # Most recent by taken_at is the 2030 row (correct=3, passed).
        assert stats["attempts"] == 2
        assert stats["last_correct"] == 3
        assert stats["last_passed"] is True
    finally:
        store.close()


def test_quiz_results_store_empty_stats(tmp_path):
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        stats = store.get_stats("never.taken")
        assert stats == {"attempts": 0, "best": 0, "last_correct": 0, "last_passed": False}
    finally:
        store.close()


def test_quiz_results_store_draft_round_trip_and_clear(tmp_path, quiz_dict):
    quiz = parse_quiz_dict(quiz_dict)
    fingerprint = quiz_fingerprint(quiz)
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        store.save_draft(
            quiz.quiz_id,
            quiz.lesson_id,
            fingerprint,
            attempt_number=2,
            position=1,
            question_order=["q2", "q1", "q3"],
            answers={"q2": "false"},
        )

        draft = store.get_draft(quiz.quiz_id, fingerprint)
        assert isinstance(draft, QuizDraft)
        assert draft.attempt_number == 2
        assert draft.position == 1
        assert draft.question_order == ["q2", "q1", "q3"]
        assert draft.answers == {"q2": "false"}

        store.clear_draft(quiz.quiz_id, fingerprint)
        assert store.get_draft(quiz.quiz_id, fingerprint) is None
    finally:
        store.close()


def test_timed_quiz_round_trips_and_has_a_real_deadline(quiz_dict):
    from datetime import datetime, timezone

    quiz_dict["time_limit_seconds"] = 90
    quiz = parse_quiz_dict(quiz_dict)
    assert quiz.time_limit_seconds == 90
    assert quiz_to_dict(quiz)["time_limit_seconds"] == 90
    deadline = quiz_deadline_at(quiz, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert quiz_seconds_remaining(
        deadline, now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    ) == 30


def test_quiz_timer_validation_rejects_invalid_limits(quiz_dict):
    quiz_dict["time_limit_seconds"] = -1
    ok, errors, _ = validate_quiz_dict(quiz_dict)
    assert ok is False
    assert any("time_limit_seconds" in error for error in errors)


def test_quiz_draft_persists_timer_deadline(tmp_path, quiz_dict):
    quiz = parse_quiz_dict(quiz_dict)
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        store.save_draft(
            quiz.quiz_id,
            quiz.lesson_id,
            quiz_fingerprint(quiz),
            1,
            0,
            ["q1", "q2", "q3"],
            {},
            deadline_at="2026-01-01T00:05:00+00:00",
        )
        draft = store.get_draft(quiz.quiz_id, quiz_fingerprint(quiz))
        assert draft is not None
        assert draft.deadline_at == "2026-01-01T00:05:00+00:00"
    finally:
        store.close()


def test_quiz_results_store_keeps_stale_fingerprint_draft_unresumable(tmp_path, quiz_dict):
    quiz = parse_quiz_dict(quiz_dict)
    old_fingerprint = quiz_fingerprint(quiz)
    store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
    try:
        store.save_draft(
            quiz.quiz_id,
            quiz.lesson_id,
            old_fingerprint,
            attempt_number=1,
            position=0,
            question_order=["q1", "q2", "q3"],
            answers={},
        )

        assert store.get_draft(quiz.quiz_id, "changed-quiz-content") is None
        assert store.get_draft(quiz.quiz_id, old_fingerprint) is not None
    finally:
        store.close()


def test_quiz_results_store_does_not_deadlock(tmp_path):
    """get_stats / record must not deadlock (lock must be reentrant).

    Regression guard: the store used a non-reentrant Lock while `_get_conn`
    also took the lock, so any store method hung forever on first call.
    """
    import signal

    def _timeout(signum, frame):
        raise TimeoutError("store call deadlocked")

    old = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(10)
    try:
        store = QuizResultsStore(db_path=tmp_path / "quiz_results.db")
        try:
            assert store.get_stats("x")["attempts"] == 0
        finally:
            store.close()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ── Pack integration ─────────────────────────────────────────


def test_create_duckypack_sets_manifest_quizzes_flag(tmp_pack):
    manifest = DuckyManifest.from_dict(json.loads(zipfile.ZipFile(tmp_pack).read("manifest.json")))
    assert manifest.quizzes is True


# ── Workspace integration (start_lesson extracts quizzes.json) ──


def test_start_lesson_extracts_quizzes_json(tmp_path, tmp_pack):
    from unittest.mock import patch

    from ducky.workspace import start_lesson

    ws = tmp_path / "ws"
    with patch("ducky.workspace.get_workspace_dir", return_value=ws), patch(
        "ducky.workspace.app_dir", return_value=tmp_path / "app"
    ):
        result = start_lesson(tmp_pack)
    assert result is not None
    assert (result / "quizzes.json").is_file(), "quizzes.json should be extracted into the lesson dir"


def test_list_available_lessons_reports_has_quizzes(tmp_path, tmp_pack):
    from unittest.mock import patch

    from ducky.workspace import list_available_lessons

    # list_available_lessons scans app_dir()/packs; place the quiz pack there.
    packs_dir = tmp_path / "app" / "packs"
    packs_dir.mkdir(parents=True)
    target = packs_dir / tmp_pack.name
    target.write_bytes(tmp_pack.read_bytes())

    with patch("ducky.workspace.app_dir", return_value=tmp_path / "app"):
        lessons = list_available_lessons()
    matching = [lesson for lesson in lessons if lesson.get("has_quizzes")]
    assert matching, "expected at least one lesson reporting has_quizzes=True"
