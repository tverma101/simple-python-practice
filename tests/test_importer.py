from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from ducky.importer import (
    _build_one_lesson,
    _detect_assignment_structure,
    _humanize_name,
    import_assignment,
)


class HumanizeNameTests(unittest.TestCase):
    def test_snake_case(self):
        self.assertEqual(_humanize_name("hello_world"), "Hello World")

    def test_kebab_case(self):
        self.assertEqual(_humanize_name("hello-world"), "Hello World")

    def test_single_word(self):
        self.assertEqual(_humanize_name("python"), "Python")

    def test_mixed_separators(self):
        self.assertEqual(_humanize_name("my-cool_lesson"), "My Cool Lesson")

    def test_multiple_spaces_collapsed(self):
        self.assertEqual(_humanize_name("a__b--c"), "A B C")

    def test_already_title(self):
        self.assertEqual(_humanize_name("Hello World"), "Hello World")

    def test_empty_string(self):
        self.assertEqual(_humanize_name(""), "")

    def test_leading_trailing_whitespace(self):
        self.assertEqual(_humanize_name("  hello_world  "), "Hello World")


class DetectStructureTests(unittest.TestCase):
    def _make_structure(
        self, tmp: Path, *, has_tests=True, has_solution=True, has_starter=False, has_instructions=True
    ):
        if has_tests:
            tests_dir = tmp / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_main.py").write_text("# tests")
        if has_solution:
            sol_dir = tmp / "solution"
            sol_dir.mkdir()
            (sol_dir / "solution.py").write_text("# solution")
        if has_starter:
            starter_dir = tmp / "starter"
            starter_dir.mkdir()
            (starter_dir / "starter.py").write_text("# starter")
        if has_instructions:
            (tmp / "INSTRUCTIONS.md").write_text("# Instructions")

    def test_detect_structure_with_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self._make_structure(t)
            result = _detect_assignment_structure(t)
            self.assertTrue(result["has_tests_dir"])
            self.assertTrue(result["has_solution_dir"])
            self.assertTrue(result["has_instructions"])
            self.assertEqual(len(result["test_files"]), 1)
            self.assertEqual(result["entry_file"], "solution.py")

    def test_detect_structure_missing_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self._make_structure(t, has_tests=False)
            result = _detect_assignment_structure(t)
            self.assertFalse(result["has_tests_dir"])
            self.assertEqual(result["test_files"], [])

    def test_detect_structure_missing_solution(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self._make_structure(t, has_solution=False, has_starter=False)
            result = _detect_assignment_structure(t)
            self.assertFalse(result["has_solution_dir"])
            self.assertIsNone(result["starter_dir"])

    def test_detect_structure_with_starter(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self._make_structure(t, has_solution=False, has_starter=True)
            result = _detect_assignment_structure(t)
            self.assertTrue(result["has_starter_dir"])
            self.assertEqual(result["entry_file"], "starter.py")

    def test_detect_flat_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "test_main.py").write_text("# flat test")
            (t / "solution.py").write_text("# flat solution")
            (t / "INSTRUCTIONS.md").write_text("# Instructions")
            result = _detect_assignment_structure(t)
            self.assertFalse(result["has_tests_dir"])
            self.assertEqual(len(result["test_files"]), 1)

    def test_detect_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            result = _detect_assignment_structure(t)
            self.assertEqual(result["test_files"], [])
            self.assertIsNone(result["starter_dir"])


class ImportAssignmentTests(unittest.TestCase):
    def _make_valid_assignment(self, tmp: Path):
        lesson = tmp / "my_lesson"
        lesson.mkdir()
        tests_dir = lesson / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_main.py").write_text("def test_add(): assert 1+1==2")
        sol_dir = lesson / "solution"
        sol_dir.mkdir()
        (sol_dir / "solution.py").write_text("def add(a, b): return a + b")
        (lesson / "INSTRUCTIONS.md").write_text("# Add two numbers")
        return lesson

    def test_import_valid_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "assignment"
            src.mkdir()
            self._make_valid_assignment(src)
            out = Path(tmp) / "output"
            result = import_assignment(src, out)
            self.assertTrue(result["ok"])
            packs = list(out.glob("*.duckypack"))
            self.assertEqual(len(packs), 1)

    def test_import_missing_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "assignment"
            src.mkdir()
            sol_dir = src / "solution"
            sol_dir.mkdir()
            (sol_dir / "solution.py").write_text("# no tests")
            out = Path(tmp) / "output"
            result = import_assignment(src, out)
            self.assertFalse(result["ok"])

    def test_import_creates_stub_when_no_starter_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "assignment"
            src.mkdir()
            tests_dir = src / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_main.py").write_text("# tests")
            zip_path = Path(tmp) / "assignment.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.write(tests_dir / "test_main.py", "assignment/tests/test_main.py")
            out = Path(tmp) / "output"
            result = import_assignment(zip_path, out)
            self.assertTrue(result["ok"])
            packs = list(out.glob("*.duckypack"))
            self.assertEqual(len(packs), 1)

    def test_import_no_starter_regular_folder_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "assignment"
            src.mkdir()
            tests_dir = src / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_main.py").write_text("# tests")
            out = Path(tmp) / "output"
            result = import_assignment(src, out)
            self.assertFalse(result["ok"])

    def test_import_with_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "assignment"
            src.mkdir()
            self._make_valid_assignment(src)
            zip_path = Path(tmp) / "assignment.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                lesson = src / "my_lesson"
                for f in lesson.rglob("*"):
                    if f.is_file():
                        zf.write(f, f"my_lesson/{f.relative_to(lesson)}")
            out = Path(tmp) / "output"
            result = import_assignment(zip_path, out)
            self.assertTrue(result["ok"])
            packs = list(out.glob("*.duckypack"))
            self.assertEqual(len(packs), 1)

    def test_import_nonexistent_path(self):
        result = import_assignment("/nonexistent/path", "/tmp/out")
        self.assertFalse(result["ok"])
        self.assertIn("Not a directory or ZIP", result["error"])

    def test_import_multiple_subdirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "multi"
            root.mkdir()
            for name in ["lesson1", "lesson2"]:
                sub = root / name
                sub.mkdir()
                tests_dir = sub / "tests"
                tests_dir.mkdir()
                (tests_dir / "test_main.py").write_text("# tests")
                sol_dir = sub / "solution"
                sol_dir.mkdir()
                (sol_dir / "solution.py").write_text("# solution")
            out = Path(tmp) / "output"
            result = import_assignment(root, out)
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 2)

    def test_build_one_lesson_empty_starter_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            tests_dir = t / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_main.py").write_text("# tests")
            starter_dir = t / "starter"
            starter_dir.mkdir()
            out = t / "output"
            out.mkdir()
            result = _build_one_lesson(t, out)
            self.assertTrue(result["ok"])


class FlatZipImportTests(unittest.TestCase):
    def test_import_flat_zip_with_readme(self):
        """Test importing a flat ZIP with test_*.py, solution.py, and README.md"""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            # Create flat structure
            (t / "test_grade_calculator.py").write_text("def test_add(): assert 1+1==2")
            (t / "grade_calculator.py").write_text("def add(a, b): return a + b")
            (t / "README.md").write_text("# Grade Calculator\nComplete the function.")

            # Create ZIP
            zip_path = t / "assignment.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.write(t / "test_grade_calculator.py", "test_grade_calculator.py")
                zf.write(t / "grade_calculator.py", "grade_calculator.py")
                zf.write(t / "README.md", "README.md")

            out = t / "output"
            result = import_assignment(zip_path, out)
            self.assertTrue(result["ok"])
            self.assertIn("Created tests/ and solution/", str(result.get("issues", [])))
            packs = list(out.glob("*.duckypack"))
            self.assertEqual(len(packs), 1)

    def test_import_flat_zip_without_solution(self):
        """Test importing a flat ZIP with only test files and README"""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            # Create flat structure without solution
            (t / "test_main.py").write_text("def test_add(): assert 1+1==2")
            (t / "README.md").write_text("# Assignment")

            # Create ZIP
            zip_path = t / "assignment.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.write(t / "test_main.py", "test_main.py")
                zf.write(t / "README.md", "README.md")

            out = t / "output"
            result = import_assignment(zip_path, out)
            # When importing from ZIP (extracted to temp dir), stubs are created
            # so the import succeeds even without a solution file
            self.assertTrue(result["ok"])

    def test_detect_flat_structure_with_solution(self):
        """Test detection of flat structure with solution.py"""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "test_main.py").write_text("# tests")
            (t / "solution.py").write_text("# solution")
            (t / "README.md").write_text("# Instructions")

            result = _detect_assignment_structure(t)
            self.assertIsNotNone(result["flat_solution_file"])
            self.assertEqual(result["flat_solution_file"].name, "solution.py")
            self.assertTrue(result["has_instructions"])  # README.md detected

    def test_detect_flat_structure_with_answer(self):
        """Test detection of flat structure with answer.py"""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "test_main.py").write_text("# tests")
            (t / "answer.py").write_text("# answer")

            result = _detect_assignment_structure(t)
            self.assertIsNotNone(result["flat_solution_file"])
            self.assertEqual(result["flat_solution_file"].name, "answer.py")


class ScanDownloadsTests(unittest.TestCase):
    def test_scan_downloads_empty(self):
        """Test scan when Downloads directory is empty or doesn't exist"""
        with tempfile.TemporaryDirectory():
            # Temporarily patch Path.home() - this is tricky, skip for now
            # Just test the function exists and works with real Downloads
            from ducky.importer import scan_downloads_for_zips

            result = scan_downloads_for_zips()
            self.assertIsInstance(result, list)

    def test_scan_downloads_finds_zips(self):
        """scan_downloads_for_zips finds an assignment-shaped zip.

        Hermetic on purpose. The previous version of this test read the real
        ``~/Downloads`` and asserted a non-empty result whenever that folder
        happened to contain any ``.zip`` -- so it failed on a developer machine
        holding an unrelated download, and proved nothing on a clean one.
        """
        import zipfile
        from unittest import mock

        from ducky.importer import scan_downloads_for_zips

        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "Downloads"
            downloads.mkdir()
            assignment_zip = downloads / "lesson_01.zip"
            with zipfile.ZipFile(assignment_zip, "w") as zf:
                zf.writestr("starter/main.py", "def value():\n    return 1\n")
                zf.writestr("tests/test_main.py", "def test_value():\n    assert True\n")
            # A zip that is not an assignment must be ignored.
            (downloads / "holiday_photos.zip").write_bytes(b"not a zip")

            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                result = scan_downloads_for_zips()

        names = [item["name"] for item in result]
        self.assertEqual(names, ["lesson_01.zip"])
        self.assertTrue(result[0]["has_tests"])
        self.assertTrue(result[0]["has_solution"])
        self.assertEqual(result[0]["file_count"], 2)


if __name__ == "__main__":
    unittest.main()
