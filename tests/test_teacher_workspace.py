from __future__ import annotations

import shutil
from pathlib import Path

from ducky.teacher_workspace import (
    TEACHER_WORKSPACE,
    create_blank_assignment,
    delete_assignment,
    ensure_teacher_workspace,
    get_built_packs_dir,
    get_lesson_issues,
    get_lessons_dir,
    get_teacher_workspace_status,
    list_built_packs,
    list_teacher_lessons,
    validate_lesson,
)


def _make_lesson_folder(base: Path, name: str) -> Path:
    lesson = base / name
    lesson.mkdir()
    starter = lesson / "starter"
    starter.mkdir()
    (starter / "solution.py").write_text("def add(a, b):\n    return a + b\n")
    tests = lesson / "tests"
    tests.mkdir()
    (tests / "test_solution.py").write_text("def test_add():\n    assert add(1, 2) == 3\n")
    (lesson / "INSTRUCTIONS.md").write_text("# Instructions\nDo the thing.\n")
    return lesson


def test_ensure_teacher_workspace():
    ws = ensure_teacher_workspace()
    assert ws.is_dir()
    assert (ws / "lessons").is_dir()
    assert (ws / "built_packs").is_dir()


def test_get_lessons_dir():
    d = get_lessons_dir()
    assert d.is_dir()
    assert d.name == "lessons"


def test_get_built_packs_dir():
    d = get_built_packs_dir()
    assert d.is_dir()
    assert d.name == "built_packs"


def test_list_teacher_lessons_empty():
    ensure_teacher_workspace()
    result = list_teacher_lessons()
    assert isinstance(result, list)


def test_list_teacher_lessons_with_valid():
    lessons_dir = get_lessons_dir()
    _make_lesson_folder(lessons_dir, "lesson_01_addition")
    try:
        result = list_teacher_lessons()
        assert len(result) >= 1
        names = [r["dir_name"] for r in result]
        assert "lesson_01_addition" in names
    finally:
        shutil.rmtree(lessons_dir / "lesson_01_addition", ignore_errors=True)


def test_list_built_packs_empty():
    ensure_teacher_workspace()
    result = list_built_packs()
    assert isinstance(result, list)


def test_get_teacher_workspace_status():
    status = get_teacher_workspace_status()
    assert "workspace_dir" in status
    assert "total_lessons" in status
    assert "valid_lessons" in status
    assert "total_packs" in status
    assert "repo_dir" in status
    assert isinstance(status["total_lessons"], int)


def test_workspace_paths_are_under_app_dir():
    assert str(TEACHER_WORKSPACE).startswith(str(Path.home()))


def test_create_blank_assignment():
    try:
        result = create_blank_assignment("test_blank_lesson", "Test Blank")
        assert result["ok"] is True
        lesson_dir = result["path"]
        assert lesson_dir.is_dir()
        assert (lesson_dir / "starter" / "solution.py").is_file()
        assert (lesson_dir / "tests" / "test_solution.py").is_file()
        assert (lesson_dir / "INSTRUCTIONS.md").is_file()
    finally:
        lessons_dir = get_lessons_dir()
        shutil.rmtree(lessons_dir / "test_blank_lesson", ignore_errors=True)


def test_create_blank_assignment_duplicate():
    try:
        create_blank_assignment("test_dup_lesson")
        result = create_blank_assignment("test_dup_lesson")
        assert result["ok"] is False
        assert "already exists" in result["error"]
    finally:
        lessons_dir = get_lessons_dir()
        shutil.rmtree(lessons_dir / "test_dup_lesson", ignore_errors=True)


def test_create_blank_assignment_bad_name():
    result = create_blank_assignment("lesson with spaces!")
    assert result["ok"] is False


def test_delete_assignment():
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / "test_delete_lesson"
    lesson_dir.mkdir()
    (lesson_dir / "starter").mkdir()
    (lesson_dir / "tests").mkdir()
    try:
        result = delete_assignment("test_delete_lesson")
        assert result["ok"] is True
        assert not lesson_dir.exists()
    finally:
        shutil.rmtree(lesson_dir, ignore_errors=True)


def test_delete_assignment_not_found():
    result = delete_assignment("nonexistent_lesson_xyz")
    assert result["ok"] is False
    assert "Not found" in result["error"]


def test_get_lesson_issues_valid():
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / "test_valid_lesson"
    _make_lesson_folder(lessons_dir, "test_valid_lesson")
    try:
        issues = get_lesson_issues(lesson_dir)
        assert len(issues) == 0
    finally:
        shutil.rmtree(lesson_dir, ignore_errors=True)


def test_get_lesson_issues_missing_starter():
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / "test_no_starter"
    lesson_dir.mkdir()
    (lesson_dir / "tests").mkdir()
    (lesson_dir / "INSTRUCTIONS.md").write_text("# Hi\n")
    try:
        issues = get_lesson_issues(lesson_dir)
        assert any("starter" in i for i in issues)
    finally:
        shutil.rmtree(lesson_dir, ignore_errors=True)


def test_get_lesson_issues_missing_tests():
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / "test_no_tests"
    lesson_dir.mkdir()
    (lesson_dir / "starter").mkdir()
    (lesson_dir / "starter" / "solution.py").write_text("pass\n")
    (lesson_dir / "INSTRUCTIONS.md").write_text("# Hi\n")
    try:
        issues = get_lesson_issues(lesson_dir)
        assert any("tests" in i for i in issues)
    finally:
        shutil.rmtree(lesson_dir, ignore_errors=True)


def test_get_lesson_issues_missing_instructions():
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / "test_no_inst"
    lesson_dir.mkdir()
    (lesson_dir / "starter").mkdir()
    (lesson_dir / "starter" / "solution.py").write_text("pass\n")
    (lesson_dir / "tests").mkdir()
    (lesson_dir / "tests" / "test_solution.py").write_text("pass\n")
    try:
        issues = get_lesson_issues(lesson_dir)
        assert any("INSTRUCTIONS" in i for i in issues)
    finally:
        shutil.rmtree(lesson_dir, ignore_errors=True)


def test_validate_lesson():
    lessons_dir = get_lessons_dir()
    _make_lesson_folder(lessons_dir, "test_validate")
    try:
        result = validate_lesson("test_validate")
        assert result["valid"] is True
        assert len(result["issues"]) == 0
    finally:
        shutil.rmtree(lessons_dir / "test_validate", ignore_errors=True)


def test_validate_lesson_not_found():
    result = validate_lesson("nonexistent_lesson_xyz")
    assert result["valid"] is False
