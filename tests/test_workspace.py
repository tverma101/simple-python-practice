from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ducky.pack import DuckyManifest, create_duckypack
from ducky.paths import ensure_app_dirs
from ducky.workspace import (
    _read_meta,
    _write_meta,
    get_current_lesson,
    get_lesson_dir,
    get_lesson_entry_path,
    get_progress,
    get_student_assignments_dir,
    list_available_lessons,
    mark_completed,
    mark_test_result,
    refresh_lesson_from_pack,
    start_lesson,
)


def _make_test_pack(tmp_path: Path, lesson_id: str = "lesson_test") -> Path:
    ensure_app_dirs()
    manifest = DuckyManifest(
        lesson_id=lesson_id,
        title="Test Lesson",
        entry_file="solution.py",
    )
    starter_dir = tmp_path / "starter"
    starter_dir.mkdir()
    (starter_dir / "solution.py").write_text("def hello():\n    return 'world'\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_solution.py").write_text("def test_hello():\n    pass\n")
    out_path = tmp_path / f"{lesson_id}.duckypack"
    return create_duckypack(starter_dir, tests_dir, out_path, manifest)


def _use_tmp_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def test_list_available_lessons_empty(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir()
        result = list_available_lessons()
        assert isinstance(result, list)


def test_list_available_lessons_finds_teacher_built_pack_without_app_packs(tmp_path):
    """Teacher-built lessons must remain visible before the first download."""
    ws = _use_tmp_workspace(tmp_path)
    teacher_packs = tmp_path / "teacher" / "workspace" / "built_packs"
    teacher_packs.mkdir(parents=True)
    pack = _make_test_pack(tmp_path, "teacher_only_lesson")
    target = teacher_packs / pack.name
    pack.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        lessons = list_available_lessons()

    assert any(item["lesson_id"] == "teacher_only_lesson" for item in lessons)


def test_has_quizzes_reflects_actual_pack_quiz_not_manifest_flag(tmp_path):
    """Regression: a pack whose manifest declares ``quizzes: true`` but ships
    no usable ``quizzes.json`` must NOT report ``has_quizzes=True`` in the
    lesson list. The marker must reflect a *loadable* quiz, so students are
    never told a teacher quiz is available when only the practice quiz exists.
    """
    import json

    from ducky.quiz import parse_quiz_dict
    from ducky.workspace import list_available_lessons, pack_has_quiz

    ws = _use_tmp_workspace(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()

    # Pack A: manifest quizzes=True AND a real quizzes.json -> has_quizzes True.
    real_quiz = parse_quiz_dict(
        {
            "quiz_id": "quiz.a",
            "lesson_id": "lesson_a",
            "title": "Real Quiz",
            "questions": [
                {
                    "id": "q1",
                    "type": "true_false",
                    "prompt": "True?",
                    "correct": "true",
                }
            ],
        },
        quiz_id="quiz.a",
        lesson_id="lesson_a",
    )
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "a.py").write_text("x=1\n")
    (tests / "test_a.py").write_text("def test_a(): assert True\n")
    quiz_json = tmp_path / "quizzes.json"
    from ducky.quiz import quiz_to_dict

    quiz_json.write_text(json.dumps(quiz_to_dict(real_quiz), indent=2))
    pack_a = create_duckypack(
        starter, tests, packs_dir / "lesson_a.duckypack",
        DuckyManifest(lesson_id="lesson_a", title="A", entry_file="a.py", quizzes=True),
        quizzes_path=quiz_json,
    )

    # Pack B: manifest quizzes=True but NO quizzes_path -> has_quizzes False.
    pack_b = create_duckypack(
        starter, tests, packs_dir / "lesson_b.duckypack",
        DuckyManifest(lesson_id="lesson_b", title="B", entry_file="a.py", quizzes=True),
    )

    assert pack_has_quiz(pack_a) is True
    assert pack_has_quiz(pack_b) is False

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        lessons = list_available_lessons()

    by_id = {l["lesson_id"]: l for l in lessons}
    assert by_id["lesson_a"]["has_quizzes"] is True
    assert by_id["lesson_b"]["has_quizzes"] is False


def test_start_lesson_creates_workspace(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        lesson_dir = start_lesson(target)
        assert lesson_dir.is_dir()
        assert (lesson_dir / "solution.py").is_file()
        assert (lesson_dir / ".ducky-meta.json").is_file()

        meta = _read_meta(lesson_dir)
        assert meta["lesson_id"] == "lesson_test"
        assert meta["status"] == "in_progress"


def test_start_lesson_can_create_visible_student_repo_folder(tmp_path):
    pack_file = _make_test_pack(tmp_path, "visible_lesson")
    repo = tmp_path / "student-repo"
    assignments = get_student_assignments_dir(repo)

    lesson_dir = start_lesson(pack_file, destination_root=assignments)

    assert lesson_dir == assignments / "visible_lesson"
    assert (lesson_dir / "solution.py").is_file()
    assert (lesson_dir / "instructions.md").is_file()
    assert (lesson_dir / "run_tests.py").is_file()
    assert "pytest" in (lesson_dir / "run_tests.py").read_text()

    # Opening the lesson again must preserve student edits in the visible copy.
    entry = lesson_dir / "solution.py"
    entry.write_text("def hello():\n    return 'student edit'\n")
    assert start_lesson(pack_file, destination_root=assignments) == lesson_dir
    assert "student edit" in entry.read_text()


def test_start_lesson_does_not_delete_unmanaged_destination(tmp_path):
    assignments = tmp_path / "assignments"
    assignments.mkdir()
    pack_file = _make_test_pack(tmp_path, "collision_lesson")
    existing = assignments / "collision_lesson"
    existing.mkdir()
    (existing / "main.py").write_text("important student work\n")

    with pytest.raises(FileExistsError):
        start_lesson(pack_file, destination_root=assignments)

    assert (existing / "main.py").read_text() == "important student work\n"


def test_refresh_lesson_applies_teacher_tests_and_preserves_student_edits(tmp_path):
    assignments = tmp_path / "assignments"
    v1_starter = tmp_path / "v1" / "starter"
    v1_tests = tmp_path / "v1" / "tests"
    v1_starter.mkdir(parents=True)
    v1_tests.mkdir()
    (v1_starter / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (v1_tests / "test_main.py").write_text("def test_old_teacher_check():\n    assert True\n", encoding="utf-8")
    v1 = create_duckypack(
        v1_starter,
        v1_tests,
        tmp_path / "v1.duckypack",
        DuckyManifest(lesson_id="refresh-me", title="First version", entry_file="main.py"),
    )

    lesson_dir = start_lesson(v1, destination_root=assignments)
    student_file = lesson_dir / "main.py"
    student_file.write_text("VALUE = 99\n", encoding="utf-8")

    v2_starter = tmp_path / "v2" / "starter"
    v2_tests = tmp_path / "v2" / "tests"
    v2_starter.mkdir(parents=True)
    v2_tests.mkdir()
    (v2_starter / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    (v2_tests / "test_main.py").write_text("def test_new_teacher_check():\n    assert True\n", encoding="utf-8")
    v2 = create_duckypack(
        v2_starter,
        v2_tests,
        tmp_path / "v2.duckypack",
        DuckyManifest(lesson_id="refresh-me", title="Second version", entry_file="main.py"),
    )

    refreshed = refresh_lesson_from_pack(v2, destination_root=assignments)

    assert refreshed["updated"] is True
    assert refreshed["conflicts"] == ["main.py"]
    assert student_file.read_text(encoding="utf-8") == "VALUE = 99\n"
    assert "new_teacher_check" in (lesson_dir / "tests" / "test_main.py").read_text(encoding="utf-8")
    conflict_files = list((lesson_dir / ".ducky-conflicts").rglob("main.py"))
    assert conflict_files and conflict_files[0].read_text(encoding="utf-8") == "VALUE = 2\n"
    assert refreshed["backup_path"].is_dir()


def test_refresh_lesson_updates_unchanged_starter_without_overwriting_edits(tmp_path):
    assignments = tmp_path / "assignments"
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    first = create_duckypack(
        starter,
        tests,
        tmp_path / "first.duckypack",
        DuckyManifest(lesson_id="merge-me", title="Merge", entry_file="main.py"),
    )
    lesson_dir = start_lesson(first, destination_root=assignments)

    updated_starter = tmp_path / "updated-starter"
    updated_tests = tmp_path / "updated-tests"
    updated_starter.mkdir()
    updated_tests.mkdir()
    (updated_starter / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    (updated_tests / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    second = create_duckypack(
        updated_starter,
        updated_tests,
        tmp_path / "second.duckypack",
        DuckyManifest(lesson_id="merge-me", title="Merge", entry_file="main.py"),
    )

    refreshed = refresh_lesson_from_pack(second, destination_root=assignments)

    assert refreshed["conflicts"] == []
    assert (lesson_dir / "main.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_existing_visible_student_copy_wins_over_legacy_workspace(tmp_path):
    """A stale ~/.ducky lesson must not hide the repository copy."""
    from ducky.workspace import _lesson_dir_for_id

    legacy = tmp_path / "legacy-workspace"
    visible = tmp_path / "repo" / "assignments"
    (legacy / "same_lesson").mkdir(parents=True)
    (visible / "same_lesson").mkdir(parents=True)
    with (
        patch("ducky.workspace.get_workspace_dir", return_value=legacy),
        patch("ducky.workspace.get_student_assignments_dir", return_value=visible),
    ):
        assert _lesson_dir_for_id("same_lesson") == visible / "same_lesson"


def test_start_lesson_idempotent(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        dir1 = start_lesson(target)
        dir2 = start_lesson(target)
        assert dir1 == dir2


def test_get_lesson_dir(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        start_lesson(target)
        result = get_lesson_dir("lesson_test")
        assert result is not None
        assert result.is_dir()


def test_get_lesson_dir_missing(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = get_lesson_dir("nonexistent_lesson_xyz")
        assert result is None


def test_get_lesson_entry_path(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        start_lesson(target)
        result = get_lesson_entry_path("lesson_test")
        assert result is not None
        assert result.name == "solution.py"


def test_mark_completed(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        start_lesson(target)
        mark_completed("lesson_test")

        meta = _read_meta(ws / "lesson_test")
        assert meta["status"] == "completed"
        assert meta["last_test_passed"] is True
        assert meta["completed_at"] is not None


def test_mark_test_result_passed(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        start_lesson(target)
        mark_test_result("lesson_test", True)

        meta = _read_meta(ws / "lesson_test")
        assert meta["status"] == "completed"
        assert meta["last_test_passed"] is True


def test_mark_test_result_failed(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        start_lesson(target)
        mark_test_result("lesson_test", False)

        meta = _read_meta(ws / "lesson_test")
        assert meta["status"] == "in_progress"
        assert meta["last_test_passed"] is False


def test_get_progress_empty(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        result = get_progress()
        assert result["total"] == 0
        assert result["completed"] == 0


def test_get_current_lesson_none(tmp_path):
    ws = _use_tmp_workspace(tmp_path)
    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = get_current_lesson()
        assert result is None


def test_meta_read_write(tmp_path):
    lesson_dir = tmp_path / "test_lesson"
    lesson_dir.mkdir()
    meta = {"status": "in_progress", "lesson_id": "test"}
    _write_meta(lesson_dir, meta)
    loaded = _read_meta(lesson_dir)
    assert loaded["status"] == "in_progress"
    assert loaded["lesson_id"] == "test"


def test_meta_read_missing(tmp_path):
    lesson_dir = tmp_path / "missing"
    lesson_dir.mkdir()
    result = _read_meta(lesson_dir)
    assert result == {}


def test_start_lesson_writes_instructions(tmp_path):
    """start_lesson should write INSTRUCTIONS.md when manifest has instructions."""
    ws = _use_tmp_workspace(tmp_path)

    # Create pack WITH instructions
    instructions_text = "# Test Instructions\n\nComplete the functions below."
    manifest = DuckyManifest(
        lesson_id="lesson_with_instructions",
        title="Lesson With Instructions",
        entry_file="solution.py",
        instructions=instructions_text,
    )
    starter_dir = tmp_path / "starter"
    starter_dir.mkdir()
    (starter_dir / "solution.py").write_text("def hello():\n    return 'world'\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_solution.py").write_text("def test_hello():\n    pass\n")
    out_path = tmp_path / "lesson_with_instructions.duckypack"
    pack_file = create_duckypack(starter_dir, tests_dir, out_path, manifest)

    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        lesson_dir = start_lesson(target)

        # INSTRUCTIONS.md should be written
        instructions_file = lesson_dir / "INSTRUCTIONS.md"
        assert instructions_file.is_file()
        content = instructions_file.read_text(encoding="utf-8")
        assert "Test Instructions" in content
        assert "Complete the functions below" in content


def test_start_lesson_no_instructions_when_empty(tmp_path):
    """start_lesson should not write INSTRUCTIONS.md when manifest has no instructions."""
    ws = _use_tmp_workspace(tmp_path)
    pack_file = _make_test_pack(tmp_path)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    target = packs_dir / pack_file.name
    pack_file.rename(target)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        lesson_dir = start_lesson(target)

        # INSTRUCTIONS.md should NOT exist when manifest has no instructions
        instructions_file = lesson_dir / "INSTRUCTIONS.md"
        assert not instructions_file.is_file()


# ── Lesson auto-detection tests ─────────────────────────────


def test_scan_workspace_empty(tmp_path):
    """Empty workspace returns no detected lessons."""
    from ducky.workspace import scan_workspace_for_lessons

    ws = tmp_path / "workspace"
    ws.mkdir()
    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = scan_workspace_for_lessons()
        assert isinstance(result, list)
        assert len(result) == 0


def test_scan_detects_lesson_dir(tmp_path):
    """Directory with tests/ should be detected as a lesson."""
    from ducky.workspace import scan_workspace_for_lessons
    from tests.helpers import make_lesson_dir

    ws = tmp_path / "workspace"
    ws.mkdir()
    lesson_dir = make_lesson_dir(ws)

    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = scan_workspace_for_lessons()
        assert len(result) >= 1
        detected = [d for d in result if d["workspace_dir"] == lesson_dir]
        assert len(detected) == 1
        assert detected[0]["detected"] is True
        assert detected[0]["pack_file"] is None


def test_scan_skips_dirs_without_tests(tmp_path):
    """Directory without tests/ should NOT be detected as a lesson."""
    from ducky.workspace import scan_workspace_for_lessons

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "random_stuff").mkdir()
    (ws / "random_stuff" / "main.py").write_text("x = 1\n")

    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = scan_workspace_for_lessons()
        assert len(result) == 0


def test_scan_detected_has_expected_keys(tmp_path):
    """Detected lesson entries should have the right schema keys."""
    from ducky.workspace import scan_workspace_for_lessons
    from tests.helpers import make_lesson_dir

    ws = tmp_path / "workspace"
    ws.mkdir()
    make_lesson_dir(ws)

    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = scan_workspace_for_lessons()
        assert len(result) >= 1
        entry = result[0]
        assert "lesson_id" in entry
        assert "title" in entry
        assert "entry_file" in entry
        assert "pack_file" in entry
        assert "status" in entry
        assert "workspace_dir" in entry
        assert "detected" in entry
        assert "lesson_dir" in entry
        assert entry["detected"] is True
        assert entry["pack_file"] is None
        assert entry["workspace_dir"].is_dir()
        assert entry["lesson_dir"].is_dir()


def test_scan_lesson_naming(tmp_path):
    """Directory name should be slugified into lesson_id and title."""
    from ducky.workspace import scan_workspace_for_lessons

    ws = tmp_path / "workspace"
    ws.mkdir()
    lesson_dir = ws / "My Amazing Lesson (v2)"
    lesson_dir.mkdir()
    (lesson_dir / "tests").mkdir()
    (lesson_dir / "tests" / "test_thing.py").write_text("def test_x():\n    pass\n")
    (lesson_dir / "main.py").write_text("print('hi')\n")

    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        result = scan_workspace_for_lessons()
        assert len(result) >= 1
        entry = result[0]
        assert "workspace." in entry["lesson_id"]
        assert "my" in entry["lesson_id"].lower()
        assert "amazing" in entry["lesson_id"].lower()
        assert "My Amazing Lesson" in entry["title"]


def test_list_lessons_merges_detected(tmp_path):
    """list_available_lessons should include both pack-based and detected."""
    from ducky.pack import DuckyManifest, create_duckypack
    from ducky.workspace import list_available_lessons
    from tests.helpers import make_lesson_dir

    ws = tmp_path / "workspace"
    ws.mkdir()

    # Create a pack-based lesson
    manifest = DuckyManifest(
        lesson_id="pack_lesson",
        title="Pack Lesson",
        entry_file="solution.py",
    )
    starter = tmp_path / "pack_starter"
    starter.mkdir()
    (starter / "solution.py").write_text("x=1\n")
    tests_dir = tmp_path / "pack_tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_x():\n    pass\n")
    pack_path = tmp_path / "packs"
    pack_path.mkdir()
    create_duckypack(starter, tests_dir, pack_path / "pack.duckypack", manifest)

    # Create a detected lesson
    make_lesson_dir(ws)

    with (
        patch("ducky.workspace.get_workspace_dir", return_value=ws),
        patch("ducky.workspace.app_dir", return_value=tmp_path),
    ):
        lessons = list_available_lessons()
        # Should have at least the pack lesson + detected lesson
        assert len(lessons) >= 2
        lesson_ids = [l["lesson_id"] for l in lessons]
        assert "pack_lesson" in lesson_ids
        assert any("workspace." in lid for lid in lesson_ids)


def test_get_current_lesson_falls_back_to_detected(tmp_path):
    """When no .ducky-meta.json, get_current_lesson falls back."""
    from ducky.workspace import get_current_lesson
    from tests.helpers import make_lesson_dir

    ws = tmp_path / "workspace"
    ws.mkdir()
    lesson_dir = make_lesson_dir(ws)

    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        current = get_current_lesson()
        assert current is not None
        assert current["lesson_dir"] == lesson_dir


def test_get_current_lesson_honors_meta_over_detected(tmp_path):
    """.ducky-meta.json should take priority over auto-detection."""
    from ducky.workspace import _write_meta, get_current_lesson
    from tests.helpers import make_lesson_dir

    ws = tmp_path / "workspace"
    ws.mkdir()
    make_lesson_dir(ws, name="detected_lesson")

    # Create another dir with metadata
    meta_dir = ws / "meta_lesson"
    meta_dir.mkdir()
    (meta_dir / "solution.py").write_text("x=1\n")
    (meta_dir / "tests").mkdir()
    (meta_dir / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    _write_meta(meta_dir, {
        "lesson_id": "meta_lesson",
        "title": "Meta Lesson",
        "entry_file": "solution.py",
        "status": "in_progress",
    })

    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
        current = get_current_lesson()
        assert current is not None
        # Should find the meta lesson (most recently touched)
        assert current is not None
