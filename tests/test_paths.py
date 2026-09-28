import unittest

from ducky.paths import app_dir, ensure_app_dirs


class PathsTests(unittest.TestCase):
    def test_app_dir_returns_path(self):
        result = app_dir()
        self.assertTrue(result.is_absolute())

    def test_ensure_app_dirs_creates_directories(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            paths = ensure_app_dirs(Path(tmp))
            self.assertTrue(paths["reports"].exists())
            self.assertTrue(paths["dataset"].exists())
            self.assertTrue(paths["logs"].exists())
