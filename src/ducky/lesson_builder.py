from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from .pack import DuckyManifest, create_duckypack


def find_quiz_sources(lesson_dir: Path, quiz_root: Path | None = None) -> list[Path]:
    """Find the teacher-owned quiz source for a lesson.

    New workspaces keep quizzes in ``quizzes/<lesson_id>.json`` (or the
    lesson's own ``quizzes/`` folder). The legacy root ``quizzes.json`` is
    still accepted so existing packs continue to build.
    """
    candidates = [lesson_dir / "quizzes.json"]
    local_dir = lesson_dir / "quizzes"
    if local_dir.is_dir():
        candidates.extend(sorted(local_dir.glob("*.json")))
    if quiz_root:
        candidates.append(quiz_root / f"{lesson_dir.name}.json")
        candidates.append(quiz_root / lesson_dir.name / "quiz.json")
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen and candidate.is_file():
            seen.add(resolved)
            unique.append(candidate)
    return unique


def find_quiz_source(lesson_dir: Path, quiz_root: Path | None = None) -> Path | None:
    """Return the first quiz source using the established precedence order."""
    sources = find_quiz_sources(lesson_dir, quiz_root)
    return sources[0] if sources else None


def _humanize_dir_name(dir_name: str) -> str:
    name = dir_name.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    words = name.split()
    result = []
    for word in words:
        result.append(word.capitalize())
    return " ".join(result)


def _validate_authoring_sources(lesson_dir: Path, manifest: DuckyManifest) -> None:
    """Reject lesson structures that would produce a false-green pack."""
    starter_dir = lesson_dir / "starter"
    tests_dir = lesson_dir / "tests"
    entry_file = starter_dir / manifest.entry_file
    if not entry_file.is_file():
        raise ValueError(f"Entry file '{manifest.entry_file}' not found in starter/")

    try:
        ast.parse(entry_file.read_text(encoding="utf-8"), filename=str(entry_file))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"Starter entry file is not valid Python: {exc}") from exc

    test_files = sorted(tests_dir.rglob("*.py"))
    if not test_files:
        raise ValueError("Lesson must contain at least one Python test file in tests/")

    has_test = False
    for test_file in test_files:
        try:
            source = test_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(test_file))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise ValueError(f"Test file '{test_file.relative_to(lesson_dir)}' is invalid: {exc}") from exc
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            or isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            for node in tree.body
        ):
            has_test = True
    if not has_test:
        raise ValueError("tests/ must contain a test_ function or Test* class")


def detect_lesson(lesson_dir: Path) -> dict:
    starter_dir = lesson_dir / "starter"
    tests_dir = lesson_dir / "tests"
    instructions_file = lesson_dir / "INSTRUCTIONS.md"
    manifest_file = lesson_dir / "manifest.json"
    quizzes_file = lesson_dir / "quizzes.json"

    has_starter = starter_dir.is_dir()
    has_tests = tests_dir.is_dir()
    has_instructions = instructions_file.is_file()
    has_manifest = manifest_file.is_file()
    has_quizzes = quizzes_file.is_file() or any((lesson_dir / "quizzes").glob("*.json"))

    entry_file = ""
    if has_starter:
        py_files = sorted(starter_dir.glob("*.py"))
        if py_files:
            entry_file = py_files[0].name

    title = _humanize_dir_name(lesson_dir.name)

    return {
        "dir_name": lesson_dir.name,
        "dir_path": lesson_dir,
        "has_starter": has_starter,
        "has_tests": has_tests,
        "has_instructions": has_instructions,
        "has_manifest": has_manifest,
        "has_quizzes": has_quizzes,
        "entry_file": entry_file,
        "title": title,
        "is_valid": bool(has_starter and has_tests and entry_file),
    }


def scan_lessons_folder(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []

    lessons = []
    for item in sorted(folder.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            detected = detect_lesson(item)
            if detected["is_valid"]:
                lessons.append(detected)
    return lessons


def build_pack_from_folder(
    lesson_dir: Path,
    output_path: Path,
    *,
    quiz_root: Path | None = None,
) -> Path:
    detected = detect_lesson(lesson_dir)
    if not detected["is_valid"]:
        raise ValueError(f"Lesson folder must have starter/ and tests/ with .py files: {lesson_dir}")

    manifest_file = lesson_dir / "manifest.json"
    if manifest_file.is_file():
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = DuckyManifest.from_dict(data)
    else:
        manifest = DuckyManifest(
            lesson_id=lesson_dir.name,
            title=detected["title"],
            entry_file=detected["entry_file"],
        )

    _validate_authoring_sources(lesson_dir, manifest)

    instructions_file = lesson_dir / "INSTRUCTIONS.md"
    if instructions_file.is_file() and not manifest.instructions:
        manifest.instructions = instructions_file.read_text(encoding="utf-8")

    quizzes_path = find_quiz_source(lesson_dir, quiz_root)
    if quizzes_path:
        from .quiz import validate_quiz_dict

        try:
            quiz_data = json.loads(quizzes_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Quiz source could not be read: {quizzes_path}: {exc}") from exc
        ok, errors, _warnings = validate_quiz_dict(quiz_data)
        if not ok:
            raise ValueError("Quiz validation failed: " + "; ".join(errors))

    return create_duckypack(
        starter_dir=lesson_dir / "starter",
        tests_dir=lesson_dir / "tests",
        out_path=output_path,
        manifest=manifest,
        source_dir=lesson_dir,
        quizzes_path=quizzes_path,
    )


def build_all_packs(teacher_folder: Path, output_dir: Path | None = None) -> list[dict]:
    if output_dir is None:
        output_dir = teacher_folder / "built_packs"
    output_dir.mkdir(parents=True, exist_ok=True)

    lessons = scan_lessons_folder(teacher_folder)
    results = []
    quiz_roots = [teacher_folder / "quizzes", teacher_folder.parent / "quizzes"]
    quiz_root = next((path for path in quiz_roots if path.is_dir()), None)

    for lesson in lessons:
        lesson_dir = lesson["dir_path"]
        pack_name = f"{lesson['dir_name']}.duckypack"
        out_path = output_dir / pack_name

        try:
            build_pack_from_folder(lesson_dir, out_path, quiz_root=quiz_root)
            results.append(
                {
                    "lesson_id": lesson["dir_name"],
                    "title": lesson["title"],
                    "pack_path": out_path,
                    "status": "built",
                }
            )
        except Exception as exc:
            results.append(
                {
                    "lesson_id": lesson["dir_name"],
                    "title": lesson["title"],
                    "pack_path": None,
                    "status": f"error: {exc}",
                }
            )

    return results
