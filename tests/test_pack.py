import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ducky.pack import DuckyManifest, create_duckypack, read_manifest, unpack_duckypack


class PackTests(unittest.TestCase):
    def test_create_read_unpack_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            starter = tmp / "starter"
            tests = tmp / "tests"
            starter.mkdir()
            tests.mkdir()
            (starter / "lesson.py").write_text("def f():\n    return 1\n")
            (tests / "test_lesson.py").write_text("import unittest\n")
            manifest = DuckyManifest(lesson_id="x.y", title="Title", entry_file="lesson.py")
            out = tmp / "lesson.duckypack"
            create_duckypack(starter, tests, out, manifest)
            loaded = read_manifest(out)
            self.assertEqual(loaded.lesson_id, "x.y")
            unpacked_manifest, unpacked = unpack_duckypack(out, tmp / "unpacked")
            self.assertTrue((unpacked / "starter" / "lesson.py").exists())
            self.assertEqual(unpacked_manifest.title, "Title")

    def test_manifest_requires_entry_file(self):
        with self.assertRaises(ValueError):
            DuckyManifest.from_dict({"lesson_id": "x", "title": "t"})

    def test_pack_validates_starter_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tests = tmp / "tests"
            tests.mkdir()
            (tests / "test_x.py").write_text("")
            manifest = DuckyManifest(lesson_id="x", title="t", entry_file="lesson.py")
            with self.assertRaises(FileNotFoundError):
                create_duckypack(tmp / "nonexistent", tests, tmp / "out.duckypack", manifest)

    def test_manifest_rejects_entry_file_traversal(self):
        with self.assertRaises(ValueError):
            DuckyManifest(lesson_id="x", title="t", entry_file="../lesson.py")

    def test_unpack_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = tmp / "bad.duckypack"
            with zipfile.ZipFile(pack, "w") as zf:
                zf.writestr(
                    "manifest.json",
                    json.dumps({"lesson_id": "x", "title": "t", "entry_file": "lesson.py"}),
                )
                zf.writestr("../evil.txt", "nope")

            with self.assertRaises(ValueError):
                unpack_duckypack(pack, tmp / "unpacked")
            self.assertFalse((tmp / "evil.txt").exists())

    def test_unpack_invalid_replacement_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = tmp / "bad.duckypack"
            destination = tmp / "lesson"
            destination.mkdir()
            (destination / "student.py").write_text("student work\n")
            with zipfile.ZipFile(pack, "w") as zf:
                zf.writestr("manifest.json", "{not json")

            with self.assertRaises((ValueError, json.JSONDecodeError)):
                unpack_duckypack(pack, destination)

            assert (destination / "student.py").read_text() == "student work\n"

    def test_manifest_rejects_lesson_id_path_traversal(self):
        with self.assertRaises(ValueError):
            DuckyManifest.from_dict(
                {"lesson_id": "../outside", "title": "t", "entry_file": "lesson.py"}
            )
