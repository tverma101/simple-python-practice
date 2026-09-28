from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from ducky.progress import ProgressTracker


class ProgressTrackerTests(unittest.TestCase):
    def test_record_and_get_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=True, diagnosis="")
            stats = tracker.get_lesson_stats("lesson1")
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["passed"], 1)
            self.assertEqual(stats["failed"], 0)
            tracker.close()

    def test_multiple_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test2.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=True)
            tracker.record_attempt("lesson1", passed=False, diagnosis="wrong return")
            tracker.record_attempt("lesson1", passed=True)
            stats = tracker.get_lesson_stats("lesson1")
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["passed"], 2)
            self.assertEqual(stats["failed"], 1)
            tracker.close()

    def test_get_lesson_stats_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test3.db"
            tracker = ProgressTracker(db_path=db)
            stats = tracker.get_lesson_stats("nonexistent")
            self.assertEqual(stats["total"], 0)
            self.assertEqual(stats["passed"], 0)
            self.assertEqual(stats["failed"], 0)
            tracker.close()

    def test_get_all_stats_multiple_lessons(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test4.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=True)
            tracker.record_attempt("lesson1", passed=False)
            tracker.record_attempt("lesson2", passed=True)
            tracker.record_attempt("lesson2", passed=True)
            stats1 = tracker.get_lesson_stats("lesson1")
            stats2 = tracker.get_lesson_stats("lesson2")
            self.assertEqual(stats1["total"], 2)
            self.assertEqual(stats1["passed"], 1)
            self.assertEqual(stats1["failed"], 1)
            self.assertEqual(stats2["total"], 2)
            self.assertEqual(stats2["passed"], 2)
            self.assertEqual(stats2["failed"], 0)
            tracker.close()

    def test_thread_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test5.db"
            tracker = ProgressTracker(db_path=db)
            errors = []

            def record_many(lesson_id, count):
                try:
                    for i in range(count):
                        tracker.record_attempt(lesson_id, passed=(i % 2 == 0))
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=record_many, args=("lesson1", 50))
            t2 = threading.Thread(target=record_many, args=("lesson2", 50))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            self.assertEqual(errors, [])
            stats1 = tracker.get_lesson_stats("lesson1")
            stats2 = tracker.get_lesson_stats("lesson2")
            self.assertEqual(stats1["total"], 50)
            self.assertEqual(stats2["total"], 50)
            tracker.close()

    def test_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test6.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=True)
            tracker.close()
            tracker2 = ProgressTracker(db_path=db)
            stats = tracker2.get_lesson_stats("lesson1")
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["passed"], 1)
            tracker2.close()

    def test_record_with_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test7.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=False, diagnosis="IndexError: list index out of range")
            stats = tracker.get_lesson_stats("lesson1")
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["failed"], 1)
            tracker.close()

    def test_different_lessons_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test8.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("a", passed=True)
            tracker.record_attempt("b", passed=False)
            stats_a = tracker.get_lesson_stats("a")
            stats_b = tracker.get_lesson_stats("b")
            self.assertEqual(stats_a["passed"], 1)
            self.assertEqual(stats_a["failed"], 0)
            self.assertEqual(stats_b["passed"], 0)
            self.assertEqual(stats_b["failed"], 1)
            tracker.close()

    def test_get_unsynced_attempts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_unsynced_empty.db"
            tracker = ProgressTracker(db_path=db)
            unsynced = tracker.get_unsynced_attempts()
            self.assertIsInstance(unsynced, list)
            self.assertEqual(len(unsynced), 0)
            tracker.close()

    def test_get_unsynced_attempts_with_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_unsynced_data.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=True)
            unsynced = tracker.get_unsynced_attempts()
            self.assertEqual(len(unsynced), 1)
            self.assertEqual(unsynced[0]["lesson_id"], "lesson1")
            self.assertTrue(unsynced[0]["passed"])
            tracker.close()

    def test_mark_attempt_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_synced.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("sync_test", True)
            unsynced = tracker.get_unsynced_attempts()
            self.assertEqual(len(unsynced), 1)
            tracker.mark_attempt_synced([unsynced[0]["id"]])
            remaining = tracker.get_unsynced_attempts()
            self.assertEqual(len(remaining), 0)
            tracker.close()

    def test_mark_attempt_synced_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_synced_empty.db"
            tracker = ProgressTracker(db_path=db)
            tracker.mark_attempt_synced([])
            unsynced = tracker.get_unsynced_attempts()
            self.assertEqual(len(unsynced), 0)
            tracker.close()

    def test_get_progress_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_summary.db"
            tracker = ProgressTracker(db_path=db)
            tracker.record_attempt("lesson1", passed=True)
            tracker.record_attempt("lesson1", passed=False)
            tracker.record_attempt("lesson2", passed=True)
            summary = tracker.get_progress_summary()
            self.assertIn("lesson1", summary)
            self.assertIn("lesson2", summary)
            self.assertEqual(summary["lesson1"]["total"], 2)
            self.assertEqual(summary["lesson1"]["passed"], 1)
            self.assertEqual(summary["lesson2"]["total"], 1)
            self.assertEqual(summary["lesson2"]["passed"], 1)
            tracker.close()

    def test_get_progress_summary_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_summary_empty.db"
            tracker = ProgressTracker(db_path=db)
            summary = tracker.get_progress_summary()
            self.assertEqual(summary, {})
            tracker.close()

    def test_multiple_sync_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test_multi_sync.db"
            tracker = ProgressTracker(db_path=db)
            for i in range(5):
                tracker.record_attempt(f"lesson_{i}", passed=(i % 2 == 0))
            unsynced = tracker.get_unsynced_attempts()
            self.assertEqual(len(unsynced), 5)
            ids = [u["id"] for u in unsynced[:3]]
            tracker.mark_attempt_synced(ids)
            remaining = tracker.get_unsynced_attempts()
            self.assertEqual(len(remaining), 2)
            tracker.close()


if __name__ == "__main__":
    unittest.main()
