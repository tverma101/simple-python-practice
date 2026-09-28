import shutil
from pathlib import Path

from ducky.pack import DuckyManifest, create_duckypack


def make_add_pack(tmp: Path) -> Path:
    starter = tmp / "starter"
    tests = tmp / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "lesson_01.py").write_text("def add_numbers(a, b):\n    pass\n")
    (tests / "test_lesson_01.py").write_text(
        "import unittest\nfrom lesson_01 import add_numbers\n\n"
        "class TestAddNumbers(unittest.TestCase):\n"
        "    def test_adds_two_numbers(self):\n        self.assertEqual(add_numbers(2, 3), 5)\n"
        "    def test_adds_zero(self):\n        self.assertEqual(add_numbers(0, 8), 8)\n"
    )
    manifest = DuckyManifest(
        lesson_id="python_basics.lesson_01",
        title="Return Values",
        entry_file="lesson_01.py",
        concept_tags=["functions", "return_vs_print"],
        diagnostic_cases=[
            {"name": "adds two numbers", "function_name": "add_numbers", "args": [2, 3], "expected": 5},
            {"name": "adds zero", "function_name": "add_numbers", "args": [0, 8], "expected": 8},
        ],
    )
    out = tmp / "lesson.duckypack"
    create_duckypack(starter, tests, out, manifest)
    return out


def make_lesson_dir(
    tmp: Path,
    name: str = "lesson_01_returns",
    code: str = "def add_numbers(a, b):\n    return a + b\n",
) -> Path:
    """Create a directory that looks like a detected lesson (no .duckypack).

    Creates a directory with a ``tests/`` folder and test files, mimicking
    what a student would have on their filesystem manually.

    Returns the lesson directory path.
    """
    lesson_dir = tmp / name
    lesson_dir.mkdir(parents=True, exist_ok=True)

    (lesson_dir / "solution.py").write_text(code, encoding="utf-8")

    tests_dir = lesson_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_solution.py").write_text(
        "import unittest\nfrom solution import add_numbers\n\n"
        "class TestAddNumbers(unittest.TestCase):\n"
        "    def test_adds_two_numbers(self):\n        self.assertEqual(add_numbers(2, 3), 5)\n"
        "    def test_adds_zero(self):\n        self.assertEqual(add_numbers(0, 8), 8)\n",
        encoding="utf-8",
    )

    return lesson_dir
