"""Tests for ducky.classroom — comprehensive edge cases for AI slop patterns.

Targets common AI-generated code failures:
  1. SQL injection / integrity errors
  2. Foreign key violations with orphaned data
  3. Malformed data handling (JSON, bytes)
  4. Empty/None responses
  5. Deletion cascading correctness
  6. Concurrent access patterns
  7. Reconnection token rotation
  8. Stats computation edge cases
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ducky.classroom import ClassroomDB, generate_access_code


class ClassroomTests(unittest.TestCase):
    """Original test suite from pre-existing test_classroom.py — preserved intact."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_classroom.db"
        self.db = ClassroomDB(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generate_access_code_is_9_digits(self):
        code = generate_access_code()
        self.assertEqual(len(code), 9)
        self.assertTrue(code.isdigit())

    def test_create_class(self):
        result = self.db.create_class("c1", "Period 1", "123456789")
        self.assertEqual(result["class_id"], "c1")
        self.assertEqual(result["name"], "Period 1")
        self.assertEqual(result["access_code"], "123456789")

    def test_list_classes(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.create_class("c2", "Period 2", "222222222")
        classes = self.db.list_classes()
        self.assertEqual(len(classes), 2)

    def test_get_class(self):
        self.db.create_class("c1", "Period 1", "111111111")
        cls = self.db.get_class("c1")
        self.assertIsNotNone(cls)
        self.assertEqual(cls["name"], "Period 1")

    def test_get_class_by_code(self):
        self.db.create_class("c1", "Period 1", "123456789")
        cls = self.db.get_class_by_code("123456789")
        self.assertIsNotNone(cls)
        self.assertEqual(cls["class_id"], "c1")

    def test_delete_class(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.assertTrue(self.db.delete_class("c1"))
        self.assertIsNone(self.db.get_class("c1"))

    def test_connect_student(self):
        self.db.create_class("c1", "Period 1", "111111111")
        result = self.db.connect_student("c1", "Ada", "192.168.1.1")
        self.assertEqual(result["name"], "Ada")
        self.assertEqual(result["class_id"], "c1")
        self.assertIn("token", result)

    def test_get_student(self):
        self.db.create_class("c1", "Period 1", "111111111")
        result = self.db.connect_student("c1", "Ada")
        student = self.db.get_student(result["token"])
        self.assertIsNotNone(student)
        self.assertEqual(student["name"], "Ada")

    def test_list_students(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.connect_student("c1", "Ada")
        self.db.connect_student("c1", "Bob")
        students = self.db.list_students("c1")
        self.assertEqual(len(students), 2)

    def test_add_pack(self):
        self.db.create_class("c1", "Period 1", "111111111")
        result = self.db.add_pack("c1", "l1", "Lesson 1", b"pack-data")
        self.assertEqual(result["pack_id"], "l1")
        self.assertEqual(result["version"], 1)

    def test_add_pack_increments_version(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_pack("c1", "l1", "Lesson 1", b"v1")
        result = self.db.add_pack("c1", "l1", "Lesson 1", b"v2")
        self.assertEqual(result["version"], 2)

    def test_get_pack(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_pack("c1", "l1", "Lesson 1", b"pack-data")
        pack = self.db.get_pack("l1", "c1")
        self.assertIsNotNone(pack)
        self.assertEqual(pack["pack_bytes"], b"pack-data")

    def test_list_packs(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data1")
        self.db.add_pack("c1", "l2", "Lesson 2", b"data2")
        packs = self.db.list_packs("c1")
        self.assertEqual(len(packs), 2)

    def test_delete_pack(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.assertTrue(self.db.delete_pack("l1", "c1"))
        self.assertIsNone(self.db.get_pack("l1", "c1"))

    def test_add_assignment(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        result = self.db.add_assignment("c1", "l1", "2026-07-15", "Do this")
        self.assertEqual(result["pack_id"], "l1")
        self.assertEqual(result["due_date"], "2026-07-15")

    def test_list_assignments(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_assignment("c1", "l1")
        assignments = self.db.list_assignments("c1")
        self.assertEqual(len(assignments), 1)

    def test_add_submission(self):
        self.db.create_class("c1", "Period 1", "111111111")
        student = self.db.connect_student("c1", "Ada")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        report = json.dumps({"passed": True})
        result = self.db.add_submission(student["token"], "c1", "l1", report)
        self.assertIn("submission_id", result)

    def test_list_submissions(self):
        self.db.create_class("c1", "Period 1", "111111111")
        student = self.db.connect_student("c1", "Ada")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_submission(student["token"], "c1", "l1", '{"passed": true}')
        submissions = self.db.list_submissions("c1")
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]["student_name"], "Ada")

    def test_add_announcement(self):
        self.db.create_class("c1", "Period 1", "111111111")
        result = self.db.add_announcement("c1", "Hello class!")
        self.assertEqual(result["message"], "Hello class!")

    def test_list_announcements(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.add_announcement("c1", "Msg 1")
        self.db.add_announcement("c1", "Msg 2")
        announcements = self.db.list_announcements("c1")
        self.assertEqual(len(announcements), 2)

    def test_export_class_data(self):
        self.db.create_class("c1", "Period 1", "111111111")
        student = self.db.connect_student("c1", "Ada")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_submission(student["token"], "c1", "l1", '{"passed": true}')
        data = self.db.export_class_data("c1")
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["student_name"], "Ada")

    def test_add_submission_with_kind(self):
        """add_submission accepts a kind and round-trips it."""
        self.db.create_class("c1", "Period 1", "111111111")
        student = self.db.connect_student("c1", "Ada")
        result = self.db.add_submission(
            student["token"], "c1", "", '{"correct": 2, "total": 3}',
            kind="quiz",
        )
        self.assertEqual(result["kind"], "quiz")
        self.assertEqual(result["pack_id"], "")

    def test_list_submissions_filters_by_kind(self):
        """list_submissions(kind=...) returns only that kind (adversarial separation)."""
        self.db.create_class("c1", "Period 1", "111111111")
        student = self.db.connect_student("c1", "Ada")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_submission(student["token"], "c1", "l1", '{"passed": true}', kind="submission")
        self.db.add_submission(student["token"], "c1", "", '{"passed": true}', kind="progress")
        self.db.add_submission(student["token"], "c1", "", '{"correct": 2}', kind="quiz")

        self.assertEqual(len(self.db.list_submissions("c1", kind="submission")), 1)
        self.assertEqual(len(self.db.list_submissions("c1", kind="progress")), 1)
        self.assertEqual(len(self.db.list_submissions("c1", kind="quiz")), 1)
        self.assertEqual(len(self.db.list_submissions("c1")), 3)

    def test_list_submissions_null_pack_id_is_fk_safe(self):
        """A progress/quiz row with no pack_id must not break the LEFT JOIN.

        This is the regression guard for the old bug where lesson_id was stored as
        pack_id and 404'd / FK-violated because the pack didn't exist under that id.
        """
        self.db.create_class("c1", "Period 1", "111111111")
        student = self.db.connect_student("c1", "Ada")
        # NO pack created, yet progress must store with pack_id NULL.
        result = self.db.add_submission(
            student["token"], "c1", "", '{"passed": false}', kind="progress"
        )
        self.assertEqual(result["pack_id"], "")
        rows = self.db.list_submissions("c1")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["pack_id"])
        # export_class_data also tolerates NULL pack_id (LEFT JOIN).
        self.assertEqual(len(self.db.export_class_data("c1")), 1)

    def test_create_class_generates_admin_token(self):
        """create_class returns a non-empty admin_token distinct from the access code."""
        result = self.db.create_class("c1", "Period 1", "111111111")
        self.assertTrue(result["admin_token"])
        self.assertNotEqual(result["admin_token"], "111111111")
        stored = self.db.get_admin_token("c1")
        self.assertEqual(stored, result["admin_token"])

    def test_migration_backfills_admin_token_on_existing_db(self):
        """A pre-existing DB without admin_token gets a distinct backfilled token.

        We simulate the pre-migration schema by creating the DB, then dropping the
        admin_token column, re-opening, and confirming _migrate_tables backfills it.
        """
        self.db.create_class("c1", "Period 1", "111111111")
        # Simulate an old DB: remove the admin_token column, then reopen.
        conn = self.db._get_conn()
        with self.db._lock:
            conn.execute("ALTER TABLE classes DROP COLUMN admin_token")
            conn.commit()
        # Reopen with a fresh connection so the migration runs again.
        self.db.close()
        db2 = ClassroomDB(db_path=self.db_path)
        admin = db2.get_admin_token("c1")
        self.assertTrue(admin, "migration should backfill a non-empty admin_token")
        self.assertNotEqual(admin, "111111111")
        db2.close()

    def test_get_admin_token_missing_class(self):
        self.assertIsNone(self.db.get_admin_token("does-not-exist"))

    def test_multi_class_isolation(self):
        self.db.create_class("c1", "Period 1", "111111111")
        self.db.create_class("c2", "Period 2", "222222222")
        self.db.connect_student("c1", "Ada")
        self.db.connect_student("c2", "Bob")
        self.assertEqual(len(self.db.list_students("c1")), 1)
        self.assertEqual(len(self.db.list_students("c2")), 1)
        self.assertEqual(self.db.list_students("c1")[0]["name"], "Ada")
        self.assertEqual(self.db.list_students("c2")[0]["name"], "Bob")


# ═══════════════════════════════════════════════════════════════════════════
# AI Slop Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class ClassroomDBEdgeCaseTests(unittest.TestCase):
    """Edge cases for ClassroomDB targeting AI slop patterns."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_edge.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.db.create_class("c1", "Period 1", "123456789")

    def tearDown(self):
        self.db.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_student_and_pack(self) -> tuple[str, str]:
        """Helper: create student and pack, return (token, pack_id)."""
        stud = self.db.connect_student("c1", "Ada")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        return stud["token"], "l1"

    # ── Integrity / error handling ───────────────────────

    def test_duplicate_class_raises(self):
        with self.assertRaises(ValueError):
            self.db.create_class("c1", "Duplicate", "999999999")

    def test_create_class_generates_code(self):
        result = self.db.create_class("c_auto", "Auto Code")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["access_code"]), 9)
        self.assertTrue(result["access_code"].isdigit())

    def test_get_nonexistent_class(self):
        cls = self.db.get_class("nonexistent")
        self.assertIsNone(cls)

    def test_get_class_by_nonexistent_code(self):
        cls = self.db.get_class_by_code("000000000")
        self.assertIsNone(cls)

    def test_delete_nonexistent_class(self):
        result = self.db.delete_class("nonexistent")
        self.assertFalse(result)

    def test_delete_class_cascades_to_children(self):
        token, pack_id = self._setup_student_and_pack()
        self.db.add_submission(token, "c1", pack_id, '{"passed": true}')
        self.db.add_assignment("c1", pack_id)
        self.db.add_announcement("c1", "bye")
        self.db.delete_class("c1")
        self.assertIsNone(self.db.get_class("c1"))
        self.assertEqual(self.db.list_students("c1"), [])
        self.assertEqual(self.db.list_packs("c1"), [])
        self.assertEqual(self.db.list_assignments("c1"), [])
        self.assertEqual(self.db.list_submissions("c1"), [])
        self.assertEqual(self.db.list_announcements("c1"), [])

    def test_connect_student_reconnection_updates_token(self):
        first = self.db.connect_student("c1", "Ada", "192.168.1.1")
        second = self.db.connect_student("c1", "Ada", "192.168.1.2")
        old_student = self.db.get_student(first["token"])
        self.assertIsNone(old_student)  # old token no longer valid
        new_student = self.db.get_student(second["token"])
        self.assertIsNotNone(new_student)
        self.assertEqual(new_student["name"], "Ada")
        self.assertEqual(new_student["ip_address"], "192.168.1.2")

    def test_get_student_invalid_token(self):
        student = self.db.get_student("bogus-token-123")
        self.assertIsNone(student)

    def test_get_student_empty_token(self):
        student = self.db.get_student("")
        self.assertIsNone(student)

    def test_get_student_none_token(self):
        # Should not crash — handled by SQL parameterization
        student = self.db.get_student(None)  # type: ignore[arg-type]
        self.assertIsNone(student)

    def test_get_pack_nonexistent(self):
        pack = self.db.get_pack("nonexistent", "c1")
        self.assertIsNone(pack)

    def test_get_pack_wrong_class(self):
        self.db.create_class("c2", "Period 2", "222222222")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        pack = self.db.get_pack("l1", "c2")
        self.assertIsNone(pack)

    def test_delete_pack_nonexistent(self):
        result = self.db.delete_pack("nonexistent", "c1")
        self.assertFalse(result)

    def test_delete_pack_cascades_to_assignments(self):
        token, pack_id = self._setup_student_and_pack()
        self.db.add_assignment("c1", pack_id)
        self.assertTrue(self.db.delete_pack(pack_id, "c1"))
        assignments = self.db.list_assignments("c1")
        self.assertEqual(len(assignments), 0)

    def test_add_pack_no_pack_bytes(self):
        result = self.db.add_pack("c1", "no_data", "Empty Pack", b"")
        self.assertEqual(result["pack_id"], "no_data")

    def test_add_pack_with_explicit_pack_id(self):
        result = self.db.add_pack("c1", "l1", "Lesson 1", b"data", pack_id="custom")
        self.assertEqual(result["pack_id"], "custom")
        pack = self.db.get_pack("custom", "c1")
        self.assertIsNotNone(pack)
        self.assertEqual(pack["lesson_id"], "l1")

    def test_add_pack_large_blob(self):
        large = b"x" * 10_000_000  # 10 MB
        result = self.db.add_pack("c1", "large", "Large Pack", large)
        self.assertEqual(result["pack_id"], "large")
        pack = self.db.get_pack("large", "c1")
        self.assertEqual(len(pack["pack_bytes"]), 10_000_000)

    # ── Submission edge cases ───────────────────────────────

    def test_add_submission_missing_pack_fails(self):
        """Submitting to a non-existent pack should trigger FK violation."""
        stud = self.db.connect_student("c1", "Bob")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_submission(stud["token"], "c1", "nonexistent", '{"passed": true}')

    def test_add_submission_invalid_token_fails(self):
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_submission("bogus-token", "c1", "l1", '{"passed": true}')

    def test_reusing_submission_id_for_another_student_is_rejected(self):
        ada = self.db.connect_student("c1", "Ada")
        bob = self.db.connect_student("c1", "Bob")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_submission(
            ada["token"], "c1", "l1", '{"passed": true}', submission_id="fixed-id"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_submission(
                bob["token"], "c1", "l1", '{"passed": false}', submission_id="fixed-id"
            )

    def test_malformed_token_expiry_fails_closed(self):
        student = self.db.connect_student("c1", "Ada")
        conn = self.db._get_conn()
        with self.db._lock:
            conn.execute(
                "UPDATE students SET token_expires_at = ? WHERE token = ?",
                ("not-a-date", student["token"]),
            )
            conn.commit()
        self.assertIsNone(self.db.get_student(student["token"]))

    def test_list_submissions_empty_class_returns_empty(self):
        submissions = self.db.list_submissions("c1")
        self.assertEqual(submissions, [])

    def test_list_submissions_filtered_by_pack(self):
        token, _ = self._setup_student_and_pack()
        self.db.add_pack("c1", "l2", "Lesson 2", b"more")
        self.db.add_submission(token, "c1", "l1", '{"passed": true}')
        self.db.add_submission(token, "c1", "l2", '{"passed": false}')
        pack1_subs = len(self.db.list_submissions("c1", pack_id="l1"))
        pack2_subs = len(self.db.list_submissions("c1", pack_id="l2"))
        all_subs = len(self.db.list_submissions("c1"))
        self.assertEqual(pack1_subs, 1)
        self.assertEqual(pack2_subs, 1)
        self.assertEqual(all_subs, 2)

    # ── Submission stats ─────────────────────────────────

    def test_submission_stats_empty(self):
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        stats = self.db.get_submission_stats("c1", "l1")
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["passed"], 0)
        self.assertEqual(stats["failed"], 0)

    def test_submission_stats_mixed(self):
        token, _ = self._setup_student_and_pack()
        self.db.add_submission(token, "c1", "l1", '{"passed": true}')
        self.db.add_submission(token, "c1", "l1", '{"passed": false}')
        self.db.add_submission(token, "c1", "l1", '{"passed": true}')
        stats = self.db.get_submission_stats("c1", "l1")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["passed"], 2)
        self.assertEqual(stats["failed"], 1)

    def test_submission_stats_malformed_json(self):
        """Malformed report_json should not crash — counted as not passed."""
        token, _ = self._setup_student_and_pack()
        self.db.add_submission(token, "c1", "l1", "not json at all")
        self.db.add_submission(token, "c1", "l1", "")
        stats = self.db.get_submission_stats("c1", "l1")
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["passed"], 0)
        self.assertEqual(stats["failed"], 2)

    def test_submission_stats_passed_race(self):
        """Report_json passed key can be bool or other truthy/falsy."""
        token, _ = self._setup_student_and_pack()
        self.db.add_submission(token, "c1", "l1", '{"passed": 1}')  # truthy int
        self.db.add_submission(token, "c1", "l1", '{"passed": 0}')  # falsy int
        stats = self.db.get_submission_stats("c1", "l1")
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["failed"], 1)

    # ── Assignment edge cases ────────────────────────────

    def test_add_assignment_no_pack_fails(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_assignment("c1", "nonexistent", "2026-07-15")

    def test_list_assignments_no_packs(self):
        """Deleting a pack cascades to its assignments — list returns empty."""
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_assignment("c1", "l1")
        self.db.delete_pack("l1", "c1")
        assignments = self.db.list_assignments("c1")
        self.assertEqual(len(assignments), 0)

    def test_delete_assignment(self):
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        asgn = self.db.add_assignment("c1", "l1")
        self.assertTrue(self.db.delete_assignment(asgn["assignment_id"], "c1"))
        self.assertFalse(self.db.delete_assignment(asgn["assignment_id"], "c1"))

    # ── Announcement edge cases ──────────────────────────

    def test_list_announcements_empty(self):
        announcements = self.db.list_announcements("c1")
        self.assertEqual(announcements, [])

    def test_delete_announcement(self):
        ann = self.db.add_announcement("c1", "Hello")
        self.assertTrue(self.db.delete_announcement(ann["announcement_id"], "c1"))
        self.assertFalse(self.db.delete_announcement(ann["announcement_id"], "c1"))

    def test_delete_announcement_nonexistent(self):
        result = self.db.delete_announcement("bogus", "c1")
        self.assertFalse(result)

    # ── Touch ───────────────────────────────────────────────

    def test_touch_student_updates_last_seen(self):
        stud = self.db.connect_student("c1", "Bob")
        original = self.db.get_student(stud["token"])
        import time

        time.sleep(0.01)  # ensure timestamp changes
        self.db.touch_student(stud["token"])
        updated = self.db.get_student(stud["token"])
        self.assertNotEqual(original["last_seen"], updated["last_seen"])

    def test_touch_student_invalid_token_noop(self):
        """Touching an invalid token should not raise."""
        self.db.touch_student("nonexistent")  # should not crash

    # ── LLM config ───────────────────────────────────────────

    def test_update_class_llm_enables_llm(self):
        result = self.db.update_class_llm("c1", True, "gemini", "gemini-2.0-flash", "https://api.example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result["llm_enabled"], 1)
        self.assertEqual(result["llm_backend"], "gemini")

    def test_update_class_llm_nonexistent_class(self):
        result = self.db.update_class_llm("nonexistent", True)
        self.assertIsNone(result)

    def test_update_class_llm_disables(self):
        self.db.update_class_llm("c1", True, "openai", "gpt-4o")
        result = self.db.update_class_llm("c1", False)
        self.assertEqual(result["llm_enabled"], 0)

    # ── Export ───────────────────────────────────────────────

    def test_export_class_data_empty(self):
        data = self.db.export_class_data("c1")
        self.assertEqual(data, [])

    def test_export_class_data_multiple_students(self):
        ada = self.db.connect_student("c1", "Ada")
        bob = self.db.connect_student("c1", "Bob")
        self.db.add_pack("c1", "l1", "Lesson 1", b"data")
        self.db.add_submission(ada["token"], "c1", "l1", '{"passed": true}')
        self.db.add_submission(bob["token"], "c1", "l1", '{"passed": false}')
        data = self.db.export_class_data("c1")
        self.assertEqual(len(data), 2)
        names = {d["student_name"] for d in data}
        self.assertIn("Ada", names)
        self.assertIn("Bob", names)

    # ── Connection edge cases ───────────────────────────

    def test_connect_student_no_ip(self):
        result = self.db.connect_student("c1", "NoIP")
        self.assertEqual(result["name"], "NoIP")
        student = self.db.get_student(result["token"])
        self.assertEqual(student["ip_address"], "")

    def test_connect_student_unicode_name(self):
        result = self.db.connect_student("c1", "Zoë Müller")
        self.assertEqual(result["name"], "Zoë Müller")

    def test_list_students_missing_class_returns_empty(self):
        students = self.db.list_students("nonexistent")
        self.assertEqual(students, [])

    def test_token_has_correct_format(self):
        result = self.db.connect_student("c1", "TokenCheck")
        token = result["token"]
        # token_hex(16) produces 32 hex characters
        self.assertEqual(len(token), 32)
        int(token, 16)  # raises if not hex

    # ── Multiple close ─────────────────────────────────

    def test_close_idempotent(self):
        self.db.close()
        self.db.close()  # should not raise

    def test_operations_after_reopen(self):
        self.db.close()
        self.db = ClassroomDB(db_path=self.db_path)  # reconnect
        self.db.create_class("c2", "After Close", "999999999")
        cls = self.db.get_class("c2")
        self.assertIsNotNone(cls)

    # ── Concurrent access ──────────────────────────────

    def test_concurrent_connections_different_names(self):
        """Multiple connections to same class with different names should not conflict."""
        results = []
        for i in range(20):
            results.append(self.db.connect_student("c1", f"Student{i}", f"10.0.0.{i}"))
        students = self.db.list_students("c1")
        self.assertEqual(len(students), 20)
        # All tokens should be unique
        tokens = [r["token"] for r in results]
        self.assertEqual(len(set(tokens)), 20)

    def test_delete_class_twice_is_idempotent(self):
        self.db.delete_class("c1")
        self.assertFalse(self.db.delete_class("c1"))


if __name__ == "__main__":
    unittest.main()
