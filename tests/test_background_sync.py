import time
import unittest
from unittest.mock import MagicMock, patch

from ducky.background_sync import BackgroundSync


class BackgroundSyncTests(unittest.TestCase):
    def test_instantiate(self):
        sync = BackgroundSync()
        self.assertIsNotNone(sync)
        self.assertFalse(sync.is_running)

    def test_instantiate_with_callback(self):
        callback = MagicMock()
        sync = BackgroundSync(on_update=callback)
        self.assertIsNotNone(sync)

    def test_start_and_stop(self):
        sync = BackgroundSync()
        sync.start()
        time.sleep(0.05)
        self.assertTrue(sync.is_running)
        sync.stop()
        self.assertFalse(sync.is_running)

    def test_start_is_idempotent(self):
        sync = BackgroundSync()
        sync.start()
        time.sleep(0.05)
        sync.start()
        time.sleep(0.05)
        sync.stop()
        self.assertFalse(sync.is_running)

    def test_stop_without_start(self):
        sync = BackgroundSync()
        sync.stop()
        self.assertFalse(sync.is_running)

    def test_check_and_update_handles_student_client_error(self):
        sync = BackgroundSync()
        from ducky.student_client import StudentClientError

        with patch("ducky.student_client.StudentClient") as MockClient:
            mock_client = MagicMock()
            mock_client.teacher_url = "http://localhost:8420"
            mock_client.list_packs.side_effect = StudentClientError("server unreachable")
            MockClient.from_config.return_value = mock_client
            result = sync._check_and_update()
            self.assertTrue(result.get("skipped"))

    def test_check_and_update_with_no_teacher_url(self):
        sync = BackgroundSync()
        with patch("ducky.student_client.StudentClient") as MockClient:
            mock_client = MagicMock()
            mock_client.teacher_url = ""
            MockClient.from_config.return_value = mock_client
            result = sync._check_and_update()
            self.assertTrue(result.get("skipped"))

    def test_check_and_update_with_empty_packs(self):
        sync = BackgroundSync()
        with patch("ducky.student_client.StudentClient") as MockClient:
            mock_client = MagicMock()
            mock_client.teacher_url = "http://localhost:8420"
            mock_client.list_packs.return_value = []
            MockClient.from_config.return_value = mock_client
            result = sync._check_and_update()
            self.assertTrue(result.get("skipped"))

    def test_get_last_result_initial(self):
        sync = BackgroundSync()
        self.assertIsNone(sync.get_last_result())

    def test_check_and_update_with_packs_downloaded(self):
        sync = BackgroundSync()
        callback = MagicMock()
        sync._on_update = callback
        with patch("ducky.student_client.StudentClient") as MockClient:
            with patch("ducky.student_client.update_packs_from_server") as mock_update:
                mock_client = MagicMock()
                mock_client.teacher_url = "http://localhost:8420"
                mock_client.list_packs.return_value = [{"lesson_id": "test"}]
                MockClient.from_config.return_value = mock_client
                mock_update.return_value = {"downloaded": 1}
                result = sync._check_and_update()
                self.assertEqual(result.get("downloaded"), 1)
                mock_client.list_packs.assert_called_once()

    def test_sync_progress_handles_error(self):
        sync = BackgroundSync()
        with patch("ducky.background_sync.load_config", return_value={"auto_sync_progress": False}):
            sync._sync_progress()

    def test_run_loop_handles_check_and_update_error(self):
        sync = BackgroundSync()
        with patch(
            "ducky.background_sync.load_config", return_value={"auto_update": True, "update_interval_seconds": 0}
        ), patch.object(sync, "_check_and_update", side_effect=Exception("check error")):
            with patch.object(sync, "_stop_event") as mock_event:
                mock_event.is_set.side_effect = [False, False, False, True]
                mock_event.wait.return_value = None
                sync._run_loop()

    def test_invalid_interval_is_clamped_to_avoid_hot_loop(self):
        self.assertEqual(BackgroundSync._interval(0, 60), 5)
        self.assertEqual(BackgroundSync._interval(-10, 60), 5)
        self.assertEqual(BackgroundSync._interval("bad", 60), 60)
        self.assertEqual(BackgroundSync._interval(999999, 60), 86400)


class AnnouncementCheckTests(unittest.TestCase):
    """Tests for _check_announcements: dedup + surfacing once."""

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path

        self.tmpdir = tempfile.mkdtemp()
        self._old_home = os.environ.get("DUCKY_HOME")
        self._cfg_home = Path(self.tmpdir) / "cfg"
        self._cfg_home.mkdir(parents=True, exist_ok=True)
        os.environ["DUCKY_HOME"] = str(self._cfg_home)

    def tearDown(self):
        import os
        import shutil

        if self._old_home is None:
            os.environ.pop("DUCKY_HOME", None)
        else:
            os.environ["DUCKY_HOME"] = self._old_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_surfaces_fresh_announcement_once(self):
        """A new announcement should fire the callback exactly once, then never again."""
        from ducky.student_client import StudentClient

        mock_client = MagicMock(spec=StudentClient)
        mock_client.teacher_url = "http://localhost:1"
        mock_client.check_announcements.return_value = [
            {"announcement_id": "a1", "message": "Hello"}
        ]

        sync = BackgroundSync(on_announcement=MagicMock())
        with patch("ducky.student_client.StudentClient") as MockClient:
            MockClient.from_config.return_value = mock_client
            sync._check_announcements()
            # Second call: id already seen in config → no callback.
            sync._check_announcements()

        self.assertEqual(sync._on_announcement.call_count, 1)
        seen = __import__("ducky.config", fromlist=["load_config"]).load_config().get("seen_announcement_ids", [])
        self.assertIn("a1", seen)

    def test_no_teacher_url_is_noop(self):
        """Without a teacher URL, checking announcements must not raise or fire."""
        mock_client = MagicMock()
        mock_client.teacher_url = ""
        sync = BackgroundSync(on_announcement=MagicMock())
        with patch("ducky.student_client.StudentClient") as MockClient:
            MockClient.from_config.return_value = mock_client
            sync._check_announcements()  # must not raise
        sync._on_announcement.assert_not_called()

    def test_dedup_distinguishes_multiple_ids(self):
        """Distinct new announcements each surface; already-seen ones do not repeat."""
        from ducky.student_client import StudentClient

        mock_client = MagicMock(spec=StudentClient)
        mock_client.teacher_url = "http://localhost:1"
        mock_client.check_announcements.side_effect = [
            [{"announcement_id": "a1", "message": "one"}],
            [{"announcement_id": "a1", "message": "one"}, {"announcement_id": "a2", "message": "two"}],
        ]
        sync = BackgroundSync(on_announcement=MagicMock())
        with patch("ducky.student_client.StudentClient") as MockClient:
            MockClient.from_config.return_value = mock_client
            sync._check_announcements()
            sync._check_announcements()
        # a1 first call, a2 second call → exactly 2 distinct surfaces.
        self.assertEqual(sync._on_announcement.call_count, 2)


if __name__ == "__main__":
    unittest.main()
