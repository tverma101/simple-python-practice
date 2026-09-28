from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from ducky.authoring import (
    add_function_test_case,
    atomic_write_json,
    load_quiz_questions_from_database,
    preview_lesson,
)
from ducky.lesson_builder import build_pack_from_folder
from ducky.pack import create_duckypack
from ducky.runner import run_pack
from ducky.teacher_workspace import create_blank_assignment
from ducky.validator import validate_duckypack


def _lesson(tmp_path: Path) -> Path:
    lesson = tmp_path / "lesson_functions"
    (lesson / "starter").mkdir(parents=True)
    (lesson / "tests").mkdir()
    (lesson / "starter" / "solution.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (lesson / "tests" / "test_smoke.py").write_text(
        "from solution import add\n\ndef test_smoke():\n    assert add(1, 1) == 2\n", encoding="utf-8"
    )
    (lesson / "INSTRUCTIONS.md").write_text("# Functions\n", encoding="utf-8")
    return lesson


def test_atomic_json_write_preserves_previous_file_on_serialization_failure(tmp_path):
    destination = tmp_path / "quizzes.json"
    atomic_write_json(destination, {"version": 1})
    with pytest.raises(TypeError):
        atomic_write_json(destination, {"bad": object()})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"version": 1}


def test_function_test_case_is_added_and_runs_through_pack_boundary(tmp_path):
    lesson = _lesson(tmp_path)
    destination = add_function_test_case(
        lesson,
        function_name="add",
        args="(2, 3)",
        expected="5",
        name="two_plus_three",
        description="Adds two positive numbers",
    )
    assert destination.is_file()
    assert "test_two_plus_three" in destination.read_text(encoding="utf-8")
    pack = build_pack_from_folder(lesson, tmp_path / "lesson.duckypack")
    report = run_pack(pack)
    assert report.passed is True


def test_function_test_case_rejects_duplicate_names(tmp_path):
    lesson = _lesson(tmp_path)
    add_function_test_case(lesson, function_name="add", args="(1, 2)", expected="3", name="sum")
    with pytest.raises(ValueError, match="already exists"):
        add_function_test_case(lesson, function_name="add", args="(1, 2)", expected="3", name="sum")


def test_function_test_case_reports_invalid_arguments_as_authoring_error(tmp_path):
    lesson = _lesson(tmp_path)
    with pytest.raises(ValueError, match="valid Python tuple or list"):
        add_function_test_case(
            lesson,
            function_name="add",
            args="not python",
            expected="3",
            name="bad_args",
        )


def test_preview_uses_manifest_identity_and_warns_about_multiple_quiz_sources(tmp_path):
    lesson = _lesson(tmp_path)
    (lesson / "manifest.json").write_text(
        json.dumps(
            {
                "lesson_id": "functions.v2",
                "title": "Functions v2",
                "entry_file": "solution.py",
            }
        ),
        encoding="utf-8",
    )
    (lesson / "quizzes").mkdir()
    quiz = {
        "quiz_id": "quiz.functions.v2",
        "lesson_id": "functions.v2",
        "title": "Functions Check",
        "questions": [
            {"id": "q1", "type": "true_false", "prompt": "Functions can return.", "correct": "true"}
        ],
    }
    (lesson / "quizzes.json").write_text(json.dumps(quiz), encoding="utf-8")
    (lesson / "quizzes" / "backup.json").write_text(json.dumps(quiz), encoding="utf-8")

    preview = preview_lesson(lesson)

    assert preview["lesson_id"] == "functions.v2"
    assert preview["title"] == "Functions v2"
    assert preview["ok"] is True
    assert preview["warnings"]


def test_built_teacher_quiz_pack_is_picked_up_by_student_sync(tmp_path, monkeypatch):
    from ducky.quiz import load_student_quiz
    from ducky.student_client import StudentClient, update_packs_from_server

    lesson = _lesson(tmp_path)
    (lesson / "quizzes.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quiz_id": "quiz.lesson_functions",
                "lesson_id": lesson.name,
                "title": "Functions Check",
                "questions": [
                    {
                        "id": "q1",
                        "type": "true_false",
                        "prompt": "A function can return a value.",
                        "correct": "true",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pack = build_pack_from_folder(lesson, tmp_path / "lesson_functions.duckypack")
    pack_bytes = pack.read_bytes()
    destination_root = tmp_path / "student" / "packs"
    client = StudentClient(teacher_url="http://teacher", token="student-token")
    monkeypatch.setattr("ducky.student_client.app_dir", lambda: tmp_path / "student")
    monkeypatch.setattr("ducky.student_client.load_config", lambda: {"pack_versions": {}})
    monkeypatch.setattr("ducky.student_client.save_config", lambda _values: None)
    monkeypatch.setattr(
        client,
        "download_pack",
        lambda _pack_id, dest=None: (Path(dest).parent.mkdir(parents=True, exist_ok=True), Path(dest).write_bytes(pack_bytes), Path(dest))[2],
    )

    result = update_packs_from_server(
        client,
        [{"pack_id": lesson.name, "lesson_id": lesson.name, "version": 1, "title": "Functions Check"}],
        dest=destination_root,
    )

    assert result["downloaded"] == 1
    quiz, teacher_assigned = load_student_quiz(tmp_path / "student" / "workspace", destination_root / f"{lesson.name}.duckypack")
    assert teacher_assigned is True
    assert quiz is not None
    assert quiz.title == "Functions Check"
    assert quiz.questions[0].prompt == "A function can return a value."


def test_pack_sync_downloads_checksum_change_even_when_version_is_unchanged(tmp_path, monkeypatch):
    import hashlib

    from ducky.student_client import StudentClient, update_packs_from_server

    destination = tmp_path / "student" / "packs"
    destination.mkdir(parents=True)
    old_bytes = b"old-pack"
    new_bytes = b"teacher-rebuilt-pack"
    (destination / "lesson.duckypack").write_bytes(old_bytes)
    client = StudentClient(teacher_url="http://teacher", token="student-token")
    monkeypatch.setattr("ducky.student_client.app_dir", lambda: tmp_path / "student")
    monkeypatch.setattr("ducky.student_client.load_config", lambda: {"pack_versions": {"lesson": 3}})
    monkeypatch.setattr("ducky.student_client.save_config", lambda _values: None)

    def download(_pack_id, dest=None):
        Path(dest).write_bytes(new_bytes)
        return Path(dest)

    monkeypatch.setattr(client, "download_pack", download)
    result = update_packs_from_server(
        client,
        [
            {
                "pack_id": "lesson",
                "version": 3,
                "pack_sha256": hashlib.sha256(new_bytes).hexdigest(),
                "title": "Updated lesson",
            }
        ],
        dest=destination,
    )

    assert result["updated"] == 1
    assert result["updated_pack_ids"] == ["lesson"]
    assert (destination / "lesson.duckypack").read_bytes() == new_bytes


def test_config_backed_pack_sync_rejects_opaque_download(tmp_path, monkeypatch):
    from ducky.student_client import StudentClient, update_packs_from_server

    destination = tmp_path / "student" / "packs"
    client = StudentClient.from_config()
    monkeypatch.setattr("ducky.student_client.load_config", lambda: {"pack_versions": {}})
    monkeypatch.setattr("ducky.student_client.save_config", lambda _values: None)
    monkeypatch.setattr(
        client,
        "download_pack",
        lambda _pack_id, dest=None: Path(dest).write_bytes(b"not-a-duckypack") or Path(dest),
    )

    result = update_packs_from_server(
        client,
        [{"pack_id": "lesson", "version": 1, "title": "Broken"}],
        dest=destination,
    )

    assert result["downloaded"] == 0
    assert result["errors"]
    assert not (destination / "lesson.duckypack").exists()


def test_preview_reports_invalid_quiz_before_publish(tmp_path):
    lesson = _lesson(tmp_path)
    (lesson / "quizzes.json").write_text(
        json.dumps({"questions": [{"type": "true_false", "prompt": "Broken", "correct": "maybe"}]}),
        encoding="utf-8",
    )
    preview = preview_lesson(lesson)
    assert preview["ok"] is False
    assert any("Quiz:" in issue for issue in preview["issues"])


def test_invalid_embedded_quiz_blocks_pack_distribution(tmp_path):
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "solution.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tests / "test_main.py").write_text("def test_main():\n    assert True\n", encoding="utf-8")
    quiz = tmp_path / "quizzes.json"
    quiz.write_text(
        json.dumps({"questions": [{"type": "true_false", "prompt": "Broken", "correct": "maybe"}]}),
        encoding="utf-8",
    )
    from ducky.pack import DuckyManifest

    pack = create_duckypack(
        starter,
        tests,
        tmp_path / "bad.duckypack",
        DuckyManifest("lesson", "Lesson", "solution.py"),
        quizzes_path=quiz,
    )
    result = validate_duckypack(pack)
    assert result.ok is False
    assert any("Quiz invalid" in error.message for error in result.errors)


def test_blank_assignment_scaffold_fails_until_implemented(tmp_path, monkeypatch):
    import ducky.teacher_workspace as workspace

    monkeypatch.setattr(workspace, "TEACHER_WORKSPACE", tmp_path / "teacher")
    result = create_blank_assignment("lesson_blank", "Blank")
    assert result["ok"] is True
    lesson = Path(result["path"])
    pack = build_pack_from_folder(lesson, tmp_path / "blank.duckypack")
    report = run_pack(pack)
    assert report.passed is False
    assert any("NotImplementedError" in failure.error for failure in report.failures)
    manifest = json.loads((lesson / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lesson_id"] == "lesson_blank"
    assert manifest["title"] == "Blank"


def test_teacher_tui_test_case_wizard_writes_case(tmp_path):
    from ducky.gui.teacher import TeacherMenuMixin

    lesson = _lesson(tmp_path)
    mixin = TeacherMenuMixin()
    answers = iter(["add", "from_two", "(2, 4)", "6", "Adds two values", "n", ""])
    with patch.object(mixin, "_teacher_pick_lesson", return_value=lesson), patch(
        "builtins.input", side_effect=answers
    ):
        mixin._teacher_add_test_case()
    assert "test_from_two" in (lesson / "tests" / "test_cases.py").read_text(encoding="utf-8")


def test_teacher_tui_quiz_wizard_saves_stable_question_id(tmp_path):
    from ducky.gui.teacher import TeacherMenuMixin

    lesson = _lesson(tmp_path)
    mixin = TeacherMenuMixin()
    answers = iter([
        "Quick quiz", "", "0", "a", "1", "Which?", "Yes", "No", "", "Yes", "Helpful", "s", ""
    ])
    with patch.object(mixin, "_teacher_pick_lesson", return_value=lesson), patch(
        "builtins.input", side_effect=answers
    ):
        mixin._teacher_quiz_flow()
    quiz = json.loads((lesson / "quizzes.json").read_text(encoding="utf-8"))
    assert quiz["questions"][0]["id"] == "q1"
    assert quiz["questions"][0]["correct"] == "Yes"


def test_teacher_tui_quiz_edit_preserves_metadata_and_supports_seconds(tmp_path):
    from ducky.gui.teacher import TeacherMenuMixin

    lesson = _lesson(tmp_path)
    (lesson / "quizzes.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quiz_id": "quiz.existing",
                "lesson_id": lesson.name,
                "title": "Existing quiz",
                "description": "Keep this",
                "source": "python quiz.db",
                "shuffle_questions": True,
                "review_mode": "all",
                "time_limit_seconds": 60,
                "questions": [
                    {
                        "id": "stable",
                        "type": "true_false",
                        "prompt": "Python is Python.",
                        "correct": "true",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    mixin = TeacherMenuMixin()
    # title, description, timer, save, replace, pause
    answers = iter(["", "", "90s", "s", "y", ""])
    with patch.object(mixin, "_teacher_pick_lesson", return_value=lesson), patch(
        "builtins.input", side_effect=answers
    ):
        mixin._teacher_quiz_flow()
    quiz = json.loads((lesson / "quizzes.json").read_text(encoding="utf-8"))
    assert quiz["time_limit_seconds"] == 90
    assert quiz["source"] == "python quiz.db"
    assert quiz["shuffle_questions"] is True
    assert quiz["review_mode"] == "all"
    assert quiz["questions"][0]["id"] == "stable"


def _question_database(tmp_path: Path) -> Path:
    database = tmp_path / "python_quiz.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE questions ("
            "id TEXT, question TEXT, question_type TEXT, options TEXT, answer TEXT, "
            "code_snippet TEXT, explanation TEXT, topic TEXT)"
        )
        connection.executemany(
            "INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "intro-1",
                    "Which keyword defines a function?",
                    "mcq",
                    '["def", "func", "function"]',
                    "1",
                    "",
                    "Python uses def to start a function definition.",
                    "functions",
                ),
                (
                    "output-1",
                    "What does this print?",
                    "predict_output",
                    "",
                    "3",
                    "print(1 + 2)",
                    "Addition is evaluated before print.",
                    "expressions",
                ),
                (
                    "bool-1",
                    "Python is dynamically typed.",
                    "true_false",
                    "",
                    "yes",
                    "",
                    "",
                    "types",
                ),
            ],
        )
        connection.commit()
    return database


def test_import_quiz_questions_from_sqlite_maps_common_python_quiz_fields(tmp_path):
    imported = load_quiz_questions_from_database(_question_database(tmp_path))
    assert imported.ok is True
    assert imported.table == "questions"
    assert imported.quiz_count == 3
    assert imported.questions[0]["type"] == "multiple_choice"
    assert imported.questions[0]["correct"] == "def"
    assert imported.questions[1]["type"] == "predict_output"
    assert imported.questions[2]["correct"] == "true"


def test_import_quiz_questions_supports_option_columns_and_duplicate_source_ids(tmp_path):
    database = tmp_path / "bank.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE question_bank (question_id INTEGER, prompt TEXT, option_a TEXT, "
            "option_b TEXT, option_c TEXT, correct_answer TEXT)"
        )
        connection.executemany(
            "INSERT INTO question_bank VALUES (?, ?, ?, ?, ?, ?)",
            [(7, "First", "A", "B", "C", "B"), (7, "Second", "X", "Y", "Z", "C")],
        )
        connection.commit()
    imported = load_quiz_questions_from_database(database)
    assert imported.ok is True
    assert [question["id"] for question in imported.questions] == ["7", "7_2"]
    assert [question["correct"] for question in imported.questions] == ["B", "Z"]


def test_import_quiz_questions_rejects_malformed_rows_without_writing_database(tmp_path):
    database = tmp_path / "bad.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE questions (prompt TEXT, options TEXT, correct TEXT)")
        connection.execute("INSERT INTO questions VALUES ('Broken', '[\"only\"]', 'only')")
        connection.commit()
    imported = load_quiz_questions_from_database(database)
    assert imported.ok is False
    assert any("at least two options" in error for error in imported.errors)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1


def test_teacher_tui_imports_sqlite_questions_atomically(tmp_path):
    from ducky.gui.teacher import TeacherMenuMixin

    lesson = _lesson(tmp_path)
    database = _question_database(tmp_path)
    mixin = TeacherMenuMixin()
    answers = iter([str(database), "", "", "y", ""])
    with patch.object(mixin, "_teacher_pick_lesson", return_value=lesson), patch(
        "builtins.input", side_effect=answers
    ):
        mixin._teacher_import_quiz_database()
    quiz = json.loads((lesson / "quizzes.json").read_text(encoding="utf-8"))
    assert len(quiz["questions"]) == 3
    assert quiz["source"].startswith("local SQLite database:")
