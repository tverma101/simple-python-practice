"""Architecture and UX regressions for teacher quiz sources and student sessions."""

from __future__ import annotations

import json
from pathlib import Path

from ducky.lesson_builder import build_all_packs, detect_lesson
from ducky.quiz import (
    QuizSession,
    load_general_quizzes,
    load_quiz_from_lesson_dir,
    load_student_quiz,
    ordered_questions,
    parse_quiz_dict,
    validate_quiz_dict,
)


def _quiz_data() -> dict:
    return {
        "schema_version": 1,
        "quiz_id": "quiz.lesson",
        "lesson_id": "lesson",
        "title": "A useful quiz",
        "shuffle_questions": True,
        "questions": [
            {"id": "q1", "type": "multiple_choice", "prompt": "One?", "options": ["a", "b"], "correct": "a", "concept": "variables"},
            {"id": "q2", "type": "true_false", "prompt": "Two?", "correct": "false", "concept": "types"},
        ],
    }


def test_quizzes_folder_is_detected_and_loaded(tmp_path: Path):
    lesson = tmp_path / "lesson"
    (lesson / "starter").mkdir(parents=True)
    (lesson / "tests").mkdir()
    (lesson / "starter" / "solution.py").write_text("def f(): return 1\n")
    (lesson / "tests" / "test_solution.py").write_text("def test_f(): assert True\n")
    (lesson / "quizzes").mkdir()
    (lesson / "quizzes" / "quiz.json").write_text(json.dumps(_quiz_data()))

    assert detect_lesson(lesson)["has_quizzes"] is True
    quiz = load_quiz_from_lesson_dir(lesson)
    assert quiz is not None
    assert quiz.questions[0].concept == "variables"


def test_teacher_scan_bundles_shared_quiz_folder(tmp_path: Path):
    assignments = tmp_path / "assignments"
    lesson = assignments / "lesson"
    (lesson / "starter").mkdir(parents=True)
    (lesson / "tests").mkdir()
    (lesson / "starter" / "solution.py").write_text("def f(): return 1\n")
    (lesson / "tests" / "test_solution.py").write_text("def test_f(): assert True\n")
    shared = tmp_path / "quizzes"
    shared.mkdir()
    shared_quiz = _quiz_data()
    (shared / "lesson.json").write_text(json.dumps(shared_quiz))

    result = build_all_packs(assignments, tmp_path / "built")
    assert result[0]["status"] == "built"
    import zipfile

    with zipfile.ZipFile(result[0]["pack_path"]) as archive:
        packed = json.loads(archive.read("quizzes.json"))
    assert packed["quiz_id"] == "quiz.lesson"


def test_quiz_session_supports_back_and_answer_revision():
    quiz = parse_quiz_dict(_quiz_data())
    session = QuizSession(ordered_questions(quiz, attempt_number=2))
    first = session.current
    session.answer("first")
    session.back()
    assert session.current.id == first.id
    session.answer("revised")
    session.next_unanswered()
    session.answer("second")
    assert session.answered_count == 2
    assert session.answers[first.id] == "revised"


def test_grade_can_review_in_the_displayed_order():
    from ducky.quiz import grade_quiz

    quiz = parse_quiz_dict(_quiz_data())
    displayed = list(reversed(quiz.questions))
    result = grade_quiz(quiz, {"q1": "a", "q2": "true"}, questions=displayed)
    assert [row["id"] for row in result.per_question] == ["q2", "q1"]


def test_predict_output_accepts_terminal_escaped_newlines():
    from ducky.quiz import QuizQuestion, grade_question

    question = QuizQuestion(
        id="q", type="predict_output", prompt="p", code="print(1)", correct="one\ntwo"
    )
    assert grade_question(question, r"one\ntwo")[0] is True


def test_platform_sample_is_valid_and_has_fifteen_questions():
    path = Path(__file__).parents[1] / "quizzes" / "python_basics_15.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ok, errors, _warnings = validate_quiz_dict(data)
    assert ok, errors
    assert len(data["questions"]) == 15
    assert data["source"].startswith("PythonQuiz platform")


def test_beginner_quiz_series_has_fourteen_distinct_timed_quizzes():
    paths = sorted(Path(__file__).parents[1].glob("quizzes/python_*_10.json"))
    assert len(paths) == 14

    quiz_ids = set()
    concepts = set()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        ok, errors, _warnings = validate_quiz_dict(data)
        assert ok, f"{path.name}: {errors}"
        assert len(data["questions"]) == 10
        assert data["time_limit_seconds"] == 600
        assert data["quiz_id"] not in quiz_ids
        quiz_ids.add(data["quiz_id"])
        concepts.update(question.get("concept", "") for question in data["questions"])
    assert len(quiz_ids) == 14
    assert len(concepts) >= 35


def test_beginner_quiz_bank_uses_realistic_distractors_and_explanations():
    """Keep the shipped bank useful for learning, not trivia or joke choices."""
    banned_phrases = (
        "restart the computer",
        "delete the assertion",
        "stop python imports",
        "hidden teacher database",
        "second main",
        "quiz title",
        "random answer",
        "randomly",
        "repeats the function",
        "a new python language",
        "every file on the computer",
    )
    root = Path(__file__).parents[1]
    for path in sorted(root.glob("quizzes/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for question in data["questions"]:
            assert question.get("concept"), f"{path.name}/{question['id']} needs a skill label"
            assert len(question.get("explanation", "").strip()) >= 45, (
                f"{path.name}/{question['id']} needs a useful explanation"
            )
            for option in question.get("options", []):
                lowered = option.casefold()
                assert not any(phrase in lowered for phrase in banned_phrases), (
                    f"{path.name}/{question['id']} has a throwaway distractor: {option!r}"
                )
            if question["type"] == "predict_output":
                assert len(question.get("code", "").splitlines()) >= 2


def test_bundled_practice_catalog_loads_every_quiz_once():
    catalog = load_general_quizzes()
    catalog_ids = {quiz.quiz_id for quiz in catalog}
    expected_ids = {
        json.loads(path.read_text(encoding="utf-8"))["quiz_id"]
        for path in Path(__file__).parents[1].glob("quizzes/*.json")
    }

    assert expected_ids <= catalog_ids
    assert len(catalog) == len(catalog_ids)


def test_student_quiz_falls_back_to_general_practice_when_pack_has_no_quiz(tmp_path, monkeypatch):
    lesson = tmp_path / "lesson"
    lesson.mkdir()
    quiz_file = tmp_path / "general.json"
    quiz_file.write_text(json.dumps(_quiz_data()))
    monkeypatch.setenv("DUCKY_PLATFORM_QUIZ", str(quiz_file))

    quiz, teacher_assigned = load_student_quiz(lesson, None)

    assert quiz is not None
    assert quiz.quiz_id == "quiz.lesson"
    assert teacher_assigned is False


def test_student_quiz_only_uses_pack_quiz_when_manifest_configures_it(tmp_path, monkeypatch):
    lesson = tmp_path / "lesson"
    lesson.mkdir()
    starter = tmp_path / "starter"
    starter.mkdir()
    (starter / "solution.py").write_text("def f(): return 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_solution.py").write_text("def test_f(): assert True\n")
    quiz_pack = tmp_path / "teacher.duckypack"
    quiz_file = tmp_path / "teacher-quiz.json"
    quiz_file.write_text(json.dumps({**_quiz_data(), "quiz_id": "teacher.quiz"}))
    from ducky.pack import DuckyManifest, create_duckypack

    create_duckypack(
        starter,
        tests,
        quiz_pack,
        DuckyManifest(lesson_id="lesson", title="Lesson", entry_file="solution.py"),
        quizzes_path=quiz_file,
    )
    general = tmp_path / "general.json"
    general.write_text(json.dumps(_quiz_data()))
    monkeypatch.setenv("DUCKY_PLATFORM_QUIZ", str(general))

    quiz, teacher_assigned = load_student_quiz(lesson, quiz_pack)

    assert quiz is not None
    assert quiz.quiz_id == "teacher.quiz"
    assert teacher_assigned is True
