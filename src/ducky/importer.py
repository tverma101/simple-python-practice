from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

from .pack import DuckyManifest, create_duckypack


def _humanize_name(name: str) -> str:
    name = name.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return " ".join(w.capitalize() for w in name.split())


def _create_stub(file_path: Path, title: str) -> None:
    """Create an empty stub file for a 'write from scratch' assignment.

    The stub has a header comment and a main() pass, so tests can at least
    import the module without crashing. Students replace the entire file.
    """
    file_path.write_text(
        f"# {title}\n"
        f"# Complete all functions below.\n"
        f"# Do not copy the solution — write your own code.\n"
        f"\n"
        f"\n"
        f"def main():\n"
        f"    pass\n"
        f"\n"
        f"\n"
        f'if __name__ == "__main__":\n'
        f"    main()\n",
        encoding="utf-8",
    )


def _detect_assignment_structure(folder: Path) -> dict:
    """Detect the structure of a raw assignment folder.

    Handles common patterns:
      assignment/
        tests/test_*.py
        solution/*.py
        INSTRUCTIONS.md
      or:
        tests/test_*.py
        starter/*.py
        INSTRUCTIONS.md
      or flat:
        test_*.py
        solution.py
        README.md (or INSTRUCTIONS.md)
    """
    has_tests_dir = (folder / "tests").is_dir()
    has_starter_dir = (folder / "starter").is_dir()
    has_solution_dir = (folder / "solution").is_dir()
    has_instructions = (folder / "INSTRUCTIONS.md").is_file()
    has_readme = (folder / "README.md").is_file()

    tests_dir = folder / "tests" if has_tests_dir else folder
    starter_dir = None
    if has_starter_dir:
        starter_dir = folder / "starter"
    elif has_solution_dir:
        starter_dir = folder / "solution"

    test_files = list(tests_dir.glob("test_*.py")) if has_tests_dir else list(folder.glob("test_*.py"))
    entry_file = ""
    if starter_dir:
        py_files = sorted(starter_dir.glob("*.py"))
        if py_files:
            entry_file = py_files[0].name

    # Detect flat structure: find .py files that could be starter/solution
    flat_solution_file = None
    if not starter_dir and not has_tests_dir:
        # Look for non-test .py files in root
        py_files = [f for f in folder.glob("*.py") if not f.name.startswith("test_")]
        if py_files:
            # Prefer files named solution.py, answer.py, or just pick the first one
            for pf in py_files:
                if pf.name.lower() in ("solution.py", "answer.py", "main.py"):
                    flat_solution_file = pf
                    break
            if not flat_solution_file:
                flat_solution_file = py_files[0]

    return {
        "tests_dir": tests_dir,
        "starter_dir": starter_dir,
        "has_tests_dir": has_tests_dir,
        "has_starter_dir": has_starter_dir,
        "has_solution_dir": has_solution_dir,
        "has_instructions": has_instructions or has_readme,
        "has_readme": has_readme,
        "test_files": test_files,
        "entry_file": entry_file,
        "flat_solution_file": flat_solution_file,
    }


def import_assignment(
    source: str | Path,
    output_dir: str | Path | None = None,
    *,
    lesson_id: str = "",
    title: str = "",
    stub: bool = False,
) -> dict:
    """Import a raw assignment folder or ZIP into .duckypack format.

    Args:
        stub: If True, create an empty stub file instead of copying the solution
              into starter/. Use this when the zip contains an answer key that
              should not be given to students.

    Returns dict with keys: ok, pack_path, lesson_id, issues, warnings.
    """
    source = Path(source)
    tmp_dir = None
    warnings = []

    if source.is_file() and source.suffix == ".zip":
        tmp_dir = Path(tempfile.mkdtemp(prefix="ducky_import_"))
        with zipfile.ZipFile(source, "r") as zf:
            for member in zf.infolist():
                target = (Path(tmp_dir) / member.filename).resolve()
                try:
                    target.relative_to(Path(tmp_dir).resolve())
                except ValueError:
                    continue
                zf.extract(member, tmp_dir)
        source = tmp_dir

    if not source.is_dir():
        return {"ok": False, "error": f"Not a directory or ZIP: {source}"}

    subdirs = [d for d in sorted(source.iterdir()) if d.is_dir() and not d.name.startswith(".")]

    lessons_to_process = []
    if len(subdirs) == 1:
        lessons_to_process.append(subdirs[0])
    elif len(subdirs) > 1:
        for d in subdirs:
            structure = _detect_assignment_structure(d)
            if structure["test_files"]:
                lessons_to_process.append(d)
        if not lessons_to_process:
            lessons_to_process = subdirs
    else:
        structure = _detect_assignment_structure(source)
        if structure["test_files"]:
            lessons_to_process.append(source)

    if not lessons_to_process:
        return {"ok": False, "error": "No assignments found. Expected test files (test_*.py) inside."}

    if output_dir is None:
        from .teacher_workspace import get_built_packs_dir

        output_dir = get_built_packs_dir()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for lesson_folder in lessons_to_process:
        result = _build_one_lesson(
            lesson_folder, output_dir, lesson_id=lesson_id, title=title,
            is_temp_dir=tmp_dir is not None, stub=stub,
        )
        results.append(result)
        if result.get("warnings"):
            warnings.extend(result["warnings"])

    if len(results) == 1:
        r = results[0]
        return {
            "ok": r["ok"],
            "pack_path": r.get("pack_path"),
            "lesson_id": r.get("lesson_id", ""),
            "issues": r.get("issues", []),
            "warnings": warnings,
        }

    return {
        "ok": all(r["ok"] for r in results),
        "packs": results,
        "count": len(results),
        "warnings": warnings,
    }


def _build_one_lesson(
    lesson_folder: Path,
    output_dir: Path,
    *,
    lesson_id: str = "",
    title: str = "",
    is_temp_dir: bool = False,
    stub: bool = False,
) -> dict:
    structure = _detect_assignment_structure(lesson_folder)
    issues = []
    warnings = []

    folder_name = lesson_folder.name
    if not lesson_id:
        lesson_id = folder_name
    if not title:
        title = _humanize_name(folder_name)

    if not structure["test_files"]:
        return {
            "ok": False,
            "lesson_id": lesson_id,
            "error": f"No test files (test_*.py) found in {lesson_folder.name}",
            "issues": ["No test files found"],
        }

    # Handle flat structure: create tests/ and solution/ directories
    if not structure["starter_dir"] and structure["flat_solution_file"]:
        flat_file = structure["flat_solution_file"]

        # Create tests/ directory
        tests_dir = lesson_folder / "tests"
        tests_dir.mkdir(exist_ok=True)
        for tf in structure["test_files"]:
            import shutil

            shutil.copy2(tf, tests_dir / tf.name)

        # Create solution/ directory
        solution_dir = lesson_folder / "solution"
        solution_dir.mkdir(exist_ok=True)

        if stub:
            # Create empty stub — students write everything from scratch
            _create_stub(solution_dir / flat_file.name, title or folder_name)
            warnings.append(
                f"Created empty stub for '{flat_file.name}' (solution excluded — students write from scratch)"
            )
            issues.append("Created tests/ and empty stub from flat files")
        else:
            shutil.copy2(flat_file, solution_dir / flat_file.name)
            warnings.append("Detected flat structure. Creating tests/ and solution/ directories.")
            issues.append("Created tests/ and solution/ from flat files")

        structure["tests_dir"] = tests_dir
        structure["starter_dir"] = solution_dir
        structure["entry_file"] = flat_file.name
        structure["has_tests_dir"] = True

    if not structure["starter_dir"]:
        if not is_temp_dir:
            warnings.append(
                f"No starter/ or solution/ found in {folder_name}. Skipping stub creation (source is not a temp dir)."
            )
            return {
                "ok": False,
                "lesson_id": lesson_id,
                "error": f"No starter/solution directory found in {folder_name}",
                "issues": ["No starter/ or solution/ directory found"],
                "warnings": warnings,
            }
        warnings.append(f"No starter/ or solution/ found in {folder_name}. Creating empty starter/ with stubs.")
        stub_dir = lesson_folder / "starter"
        stub_dir.mkdir(exist_ok=True)
        entry_py = "solution.py"
        stub_file = stub_dir / entry_py
        if not stub_file.exists():
            stub_file.write_text(
                f"# {title}\n# Complete the function below.\n\ndef main():\n    pass\n",
                encoding="utf-8",
            )
        structure["starter_dir"] = stub_dir
        structure["entry_file"] = entry_py
        issues.append("Created starter/ with empty stub (replace with student code)")
    elif not structure["entry_file"]:
        py_files = sorted(structure["starter_dir"].glob("*.py"))
        if py_files:
            structure["entry_file"] = py_files[0].name
        else:
            stub_file = structure["starter_dir"] / "solution.py"
            stub_file.write_text(
                f"# {title}\n# Complete the function below.\n\ndef main():\n    pass\n",
                encoding="utf-8",
            )
            structure["entry_file"] = "solution.py"
            warnings.append("starter/ was empty. Created solution.py stub.")

    if not structure["has_instructions"]:
        instructions_file = lesson_folder / "INSTRUCTIONS.md"
        if not instructions_file.exists():
            warnings.append("No INSTRUCTIONS.md found. You should add one.")

    # Pick up an authored quizzes.json if present (shipped to students inside
    # the pack). Reuses the same source_dir fallback as INSTRUCTIONS.md.
    quizzes_file = lesson_folder / "quizzes.json"
    quizzes_path = quizzes_file if quizzes_file.is_file() else None

    entry_file = structure["entry_file"]
    starter_dir = structure["starter_dir"]
    tests_dir = structure["tests_dir"]

    if not (starter_dir / entry_file).exists():
        return {
            "ok": False,
            "lesson_id": lesson_id,
            "error": f"Entry file '{entry_file}' not found in {starter_dir.name}/",
            "issues": [f"Entry file not found: {entry_file}"],
        }

    manifest = DuckyManifest(
        lesson_id=lesson_id,
        title=title,
        entry_file=entry_file,
    )

    # Check for INSTRUCTIONS.md first, then README.md
    instructions_file = lesson_folder / "INSTRUCTIONS.md"
    if instructions_file.is_file():
        manifest.instructions = instructions_file.read_text(encoding="utf-8")
    else:
        readme_file = lesson_folder / "README.md"
        if readme_file.is_file():
            manifest.instructions = readme_file.read_text(encoding="utf-8")
            warnings.append("Used README.md as instructions")

    pack_name = f"{lesson_id}.duckypack"
    out_path = output_dir / pack_name

    try:
        pack_path = create_duckypack(
            starter_dir=starter_dir,
            tests_dir=tests_dir,
            out_path=out_path,
            manifest=manifest,
            source_dir=lesson_folder,
            quizzes_path=quizzes_path,
        )

        # Validate the created pack
        from .validator import validate_duckypack

        validation = validate_duckypack(pack_path)
        if not validation.ok:
            return {
                "ok": False,
                "lesson_id": lesson_id,
                "error": f"Pack validation failed: {'; '.join(str(e) for e in validation.errors)}",
                "issues": [str(e) for e in validation.errors],
                "warnings": warnings + [str(w) for w in validation.warnings],
            }
        warnings.extend([str(w) for w in validation.warnings])

        return {
            "ok": True,
            "pack_path": pack_path,
            "lesson_id": lesson_id,
            "title": title,
            "issues": issues,
            "warnings": warnings,
        }
    except Exception as exc:
        return {
            "ok": False,
            "lesson_id": lesson_id,
            "error": str(exc),
            "issues": issues + [str(exc)],
            "warnings": warnings,
        }


def scan_downloads_for_zips() -> list[dict]:
    """Scan ~/Downloads for ZIP files that look like assignment packages.

    Returns list of dicts with keys: path, name, size_mb, has_tests, has_solution, file_count.
    """
    downloads_dir = Path.home() / "Downloads"
    if not downloads_dir.is_dir():
        return []

    results = []
    for zip_file in sorted(downloads_dir.glob("*.zip")):
        try:
            with zipfile.ZipFile(zip_file, "r") as zf:
                names = zf.namelist()
                file_count = len(names)

                # Check for test files
                has_tests = any(name.endswith(".py") and "test" in Path(name).name.lower() for name in names)

                # Check for solution/starter files (non-test .py files)
                has_solution = any(
                    name.endswith(".py")
                    and not Path(name).name.startswith("test_")
                    and not Path(name).name.startswith("test.")
                    for name in names
                )

                # Also check for test_*.py pattern (more specific)
                has_test_files = any(Path(name).name.startswith("test_") and name.endswith(".py") for name in names)

                if has_tests or has_test_files:
                    size_mb = zip_file.stat().st_size / (1024 * 1024)
                    results.append(
                        {
                            "path": zip_file,
                            "name": zip_file.name,
                            "size_mb": round(size_mb, 2),
                            "has_tests": has_tests or has_test_files,
                            "has_solution": has_solution,
                            "file_count": file_count,
                        }
                    )
        except (zipfile.BadZipFile, OSError):
            continue

    return results
