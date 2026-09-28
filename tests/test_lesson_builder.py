from __future__ import annotations

from pathlib import Path

from ducky.lesson_builder import (
    build_all_packs,
    build_pack_from_folder,
    detect_lesson,
    scan_lessons_folder,
)
from ducky.pack import read_manifest
from ducky.paths import ensure_app_dirs


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


def test_detect_lesson_valid(tmp_path):
    lesson = _make_lesson_folder(tmp_path, "lesson_01_return_values")
    result = detect_lesson(lesson)
    assert result is not None
    assert result["dir_name"] == "lesson_01_return_values"
    assert result["title"] == "Lesson 01 Return Values"
    assert result["entry_file"] == "solution.py"


def test_detect_lesson_no_starter(tmp_path):
    lesson = tmp_path / "lesson_no_starter"
    lesson.mkdir()
    (lesson / "tests").mkdir()
    result = detect_lesson(lesson)
    assert result["is_valid"] is False


def test_detect_lesson_no_tests(tmp_path):
    lesson = tmp_path / "lesson_no_tests"
    lesson.mkdir()
    starter = lesson / "starter"
    starter.mkdir()
    (starter / "solution.py").write_text("pass\n")
    result = detect_lesson(lesson)
    assert result["is_valid"] is False


def test_scan_lessons_folder(tmp_path):
    _make_lesson_folder(tmp_path, "lesson_01_return_values")
    _make_lesson_folder(tmp_path, "lesson_02_conditionals")
    results = scan_lessons_folder(tmp_path)
    assert len(results) == 2
    ids = [r["dir_name"] for r in results]
    assert "lesson_01_return_values" in ids
    assert "lesson_02_conditionals" in ids


def test_build_pack_from_folder(tmp_path):
    lesson = _make_lesson_folder(tmp_path, "lesson_01_return_values")
    ensure_app_dirs()
    output = tmp_path / "output"
    output.mkdir()
    out_path = output / "lesson_01_return_values.duckypack"
    result_path = build_pack_from_folder(lesson, out_path)
    assert result_path.is_file()
    manifest = read_manifest(result_path)
    assert manifest.lesson_id == "lesson_01_return_values"
    assert manifest.instructions == "# Instructions\nDo the thing.\n"


def test_build_all_packs(tmp_path):
    _make_lesson_folder(tmp_path, "lesson_01_return_values")
    _make_lesson_folder(tmp_path, "lesson_02_conditionals")
    ensure_app_dirs()
    output = tmp_path / "output"
    output.mkdir()
    results = build_all_packs(tmp_path, output)
    assert len(results) == 2
    for item in results:
        assert item["lesson_id"].startswith("lesson_")
        assert item["pack_path"] is not None


def test_build_pack_includes_instructions(tmp_path):
    lesson = _make_lesson_folder(tmp_path, "lesson_01_return_values")
    ensure_app_dirs()
    output = tmp_path / "output"
    output.mkdir()
    out_path = output / "lesson_01_return_values.duckypack"
    build_pack_from_folder(lesson, out_path)
    manifest = read_manifest(out_path)
    assert "Do the thing" in manifest.instructions


def test_build_pack_rejects_invalid_quiz_before_replacing_output(tmp_path):
    lesson = _make_lesson_folder(tmp_path, "lesson_invalid_quiz")
    (lesson / "quizzes.json").write_text(
        '{"time_limit_seconds": -5, "questions": [{"type": "true_false", '
        '"prompt": "Broken", "correct": "maybe"}]}',
        encoding="utf-8",
    )
    output = tmp_path / "lesson.duckypack"
    output.write_bytes(b"previous-pack")
    import pytest

    with pytest.raises(ValueError, match="Quiz validation failed"):
        build_pack_from_folder(lesson, output)
    assert output.read_bytes() == b"previous-pack"


def test_build_pack_rejects_test_files_that_execute_no_tests(tmp_path):
    lesson = _make_lesson_folder(tmp_path, "lesson_empty_suite")
    (lesson / "tests" / "test_solution.py").write_text("helper = 1\n", encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match=r"test_.*function|Test\* class"):
        build_pack_from_folder(lesson, tmp_path / "lesson.duckypack")


def test_humanize_dir_name():
    from ducky.lesson_builder import _humanize_dir_name

    assert _humanize_dir_name("lesson_01_return_values") == "Lesson 01 Return Values"
    assert _humanize_dir_name("lesson_02_if_else") == "Lesson 02 If Else"
    assert _humanize_dir_name("advanced_01") == "Advanced 01"
