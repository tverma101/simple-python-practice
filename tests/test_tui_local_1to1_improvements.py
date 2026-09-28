from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ducky.classroom import ClassroomDB
from ducky.pack import DuckyManifest, create_duckypack
from ducky.student_client import StudentClient, StudentClientError
from ducky.workspace import start_lesson


def test_pack_replacement_is_versioned_and_rolls_back(tmp_path: Path):
    db = ClassroomDB(tmp_path / "classroom.db")
    try:
        db.create_class("class-1", "One to one")
        db.add_pack("class-1", "loops", "Loops", b"version one")
        db.add_pack("class-1", "loops", "Loops updated", b"version two")

        with pytest.raises(ValueError, match="already used"):
            db.add_pack("class-2", "loops", "Collision", b"other class")

        restored = db.rollback_pack("loops", "class-1")
        assert restored and restored["version"] == 1
        assert db.get_pack("loops", "class-1")["pack_bytes"] == b"version one"
    finally:
        db.close()


def test_backup_restore_recovers_local_classroom_data(tmp_path: Path):
    db = ClassroomDB(tmp_path / "classroom.db")
    db.create_class("class-1", "One to one")
    db.add_pack("class-1", "lesson", "Lesson", b"saved")
    backup = db.backup_to(tmp_path / "backup.db")
    db.delete_pack("lesson", "class-1")
    db.restore_from(backup)
    try:
        assert db.get_pack("lesson", "class-1")["pack_bytes"] == b"saved"
    finally:
        db.close()


def test_reopening_lesson_preserves_student_edits(tmp_path: Path, monkeypatch):
    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_main.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    pack = tmp_path / "lesson.duckypack"
    create_duckypack(
        starter,
        tests,
        pack,
        DuckyManifest(lesson_id="lesson", title="Lesson", entry_file="main.py"),
    )
    monkeypatch.setenv("DUCKY_WORKSPACE", str(tmp_path / "assignments"))

    lesson_dir = start_lesson(pack)
    edited = lesson_dir / "main.py"
    edited.write_text("VALUE = 99\n", encoding="utf-8")
    assert start_lesson(pack) == lesson_dir
    assert edited.read_text(encoding="utf-8") == "VALUE = 99\n"


def test_progress_acknowledgement_uses_exact_attempt_ids():
    client = StudentClient(teacher_url="http://teacher", token="token")
    with patch.object(client, "_post_json", return_value={"ok": True, "accepted_ids": [2]}):
        result = client.sync_progress(
            [
                {"id": 1, "lesson_id": ""},
                {"id": 2, "lesson_id": "lesson-2", "passed": True},
            ]
        )
    assert result["synced_ids"] == [2]
    assert result["sent"] == 1
    assert result["failed"] == 1


def test_corrupt_update_does_not_replace_existing_pack(tmp_path: Path):
    destination = tmp_path / "lesson.duckypack"
    destination.write_bytes(b"known good")
    client = StudentClient(teacher_url="http://teacher", token="token")
    with patch.object(client, "_get_bytes", return_value=(b"not a zip archive", {})):
        with pytest.raises(StudentClientError, match="valid duckypack"):
            client.download_pack("lesson", destination)
    assert destination.read_bytes() == b"known good"
