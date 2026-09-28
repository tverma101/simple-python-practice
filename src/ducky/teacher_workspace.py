from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .lesson_builder import build_all_packs, detect_lesson, scan_lessons_folder
from .pack import read_manifest
from .paths import app_dir

TEACHER_WORKSPACE = app_dir() / "teacher" / "workspace"
REPO_ASSIGNMENTS = Path(__file__).parent.parent.parent / "assignments"


def ensure_teacher_workspace() -> Path:
    TEACHER_WORKSPACE.mkdir(parents=True, exist_ok=True)
    (TEACHER_WORKSPACE / "lessons").mkdir(exist_ok=True)
    (TEACHER_WORKSPACE / "built_packs").mkdir(exist_ok=True)
    return TEACHER_WORKSPACE


def get_lessons_dir() -> Path:
    d = ensure_teacher_workspace() / "lessons"
    d.mkdir(exist_ok=True)
    return d


def get_built_packs_dir() -> Path:
    d = ensure_teacher_workspace() / "built_packs"
    d.mkdir(exist_ok=True)
    return d


def get_repo_assignments_dir() -> Path:
    return REPO_ASSIGNMENTS


def create_blank_assignment(name: str, title: str = "") -> dict:
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / name

    if lesson_dir.exists():
        return {"ok": False, "error": f"Folder already exists: {name}"}

    if not name.replace("_", "").replace("-", "").isalnum():
        return {"ok": False, "error": "Name must contain only letters, numbers, underscores, or hyphens"}

    temp_dir: Path | None = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{name}.", suffix=".creating", dir=lessons_dir))
        (temp_dir / "starter").mkdir()
        (temp_dir / "tests").mkdir()

        (temp_dir / "starter" / "solution.py").write_text(
            f"# {title or name}\n# Complete the function below.\n\n"
            "def main():\n"
            "    raise NotImplementedError(\"Implement this assignment before running it.\")\n",
            encoding="utf-8",
        )
        (temp_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "pack_version": "1.0",
                    "lesson_id": name,
                    "title": title or name,
                    "language": "python",
                    "entry_file": "solution.py",
                    "test_command": "python -m pytest tests/",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (temp_dir / "tests" / "test_solution.py").write_text(
            "from solution import main\n\n\ndef test_main():\n    result = main()\n    assert result is not None\n",
            encoding="utf-8",
        )
        (temp_dir / "INSTRUCTIONS.md").write_text(
            f"# {title or name}\n\n## Goal\nDescribe what the student should do.\n\n"
            f"## Instructions\n\nWrite your instructions here.\n\n"
            f"## Example\n\n```python\n# Show expected input/output\n```\n",
            encoding="utf-8",
        )
        temp_dir.rename(lesson_dir)
        temp_dir = None
        return {"ok": True, "path": lesson_dir}
    except Exception as exc:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return {"ok": False, "error": str(exc)}


def delete_assignment(name: str) -> dict:
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / name

    if not lesson_dir.exists():
        return {"ok": False, "error": f"Not found: {name}"}

    try:
        shutil.rmtree(lesson_dir)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_lesson_issues(lesson_dir: Path) -> list[str]:
    issues = []
    dir_name = lesson_dir.name

    if not lesson_dir.is_dir():
        return [f"Not a directory: {dir_name}"]

    starter = lesson_dir / "starter"
    tests = lesson_dir / "tests"
    instructions = lesson_dir / "INSTRUCTIONS.md"

    if not starter.is_dir():
        issues.append("Missing: starter/ folder")
    else:
        py_files = list(starter.glob("*.py"))
        if not py_files:
            issues.append("Empty: starter/ has no .py files")
        elif len(py_files) > 1:
            names = [f.name for f in py_files]
            issues.append(f"Ambiguous: starter/ has multiple .py files: {', '.join(names)}")

    if not tests.is_dir():
        issues.append("Missing: tests/ folder")
    else:
        test_files = list(tests.glob("*.py"))
        if not test_files:
            issues.append("Empty: tests/ has no .py files")

    if not instructions.is_file():
        issues.append("Missing: INSTRUCTIONS.md")

    return issues


def validate_lesson(name: str) -> dict:
    lessons_dir = get_lessons_dir()
    lesson_dir = lessons_dir / name

    if not lesson_dir.is_dir():
        return {"valid": False, "name": name, "issues": [f"Folder not found: {name}"], "path": lesson_dir}

    issues = get_lesson_issues(lesson_dir)
    detected = detect_lesson(lesson_dir)
    return {
        "valid": len(issues) == 0 and detected.get("is_valid", False),
        "name": name,
        "issues": issues,
        "detected": detected,
        "path": lesson_dir,
    }


def list_teacher_lessons() -> list[dict]:
    lessons_dir = get_lessons_dir()
    results = []
    if not lessons_dir.is_dir():
        return results
    try:
        for item in sorted(lessons_dir.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                detected = detect_lesson(item)
                issues = get_lesson_issues(item)
                detected["issues"] = issues
                detected["has_issues"] = len(issues) > 0
                results.append(detected)
    except OSError:
        pass
    return results


def scan_teacher_lessons() -> list[dict]:
    lessons_dir = get_lessons_dir()
    return scan_lessons_folder(lessons_dir)


def build_all_teacher_packs() -> list[dict]:
    lessons_dir = get_lessons_dir()
    output_dir = get_built_packs_dir()
    return build_all_packs(lessons_dir, output_dir)


def list_built_packs() -> list[dict]:
    packs_dir = get_built_packs_dir()
    results = []
    if not packs_dir.is_dir():
        return results
    try:
        for pack_file in sorted(packs_dir.glob("*.duckypack")):
            try:
                manifest = read_manifest(pack_file)
                results.append(
                    {
                        "pack_file": pack_file,
                        "lesson_id": manifest.lesson_id,
                        "title": manifest.title,
                        "entry_file": manifest.entry_file,
                    }
                )
            except Exception:
                continue
    except OSError:
        pass
    return results


def _dir_fingerprint(path: Path) -> str:
    parts = []
    for f in sorted(path.rglob("*")):
        if f.is_file():
            rel = f.relative_to(path)
            st = f.stat()
            # Include both size and mtime so a content change that preserves
            # size (e.g. replacing one identifier with another of equal length)
            # is still detected as a change.
            parts.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
    if not parts:
        return ""
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _read_sync_meta(lessons_dir: Path) -> dict:
    meta_path = lessons_dir / ".sync-meta.json"
    if meta_path.is_file():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"fingerprints": {}, "last_synced": None}


def _write_sync_meta(lessons_dir: Path, meta: dict) -> None:
    meta_path = lessons_dir / ".sync-meta.json"
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sync_from_repo(*, delete_removed: bool = False) -> dict:
    repo = get_repo_assignments_dir()
    if not repo.is_dir():
        return {"ok": False, "error": f"Repo assignments folder not found: {repo}"}

    lessons_dir = get_lessons_dir()
    lessons_dir.mkdir(parents=True, exist_ok=True)
    sync_meta = _read_sync_meta(lessons_dir)
    fingerprints = sync_meta.get("fingerprints", {})

    synced = 0
    updated = 0
    skipped = 0
    deleted = 0
    errors = []
    repo_lesson_names = set()

    try:
        for item in sorted(repo.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue

            repo_lesson_names.add(item.name)
            dest = lessons_dir / item.name

            try:
                new_fp = _dir_fingerprint(item)
            except Exception as exc:
                errors.append(f"{item.name}: fingerprint failed: {exc}")
                continue

            old_fp = fingerprints.get(item.name)

            if dest.exists() and old_fp == new_fp:
                skipped += 1
                continue

            try:
                if dest.exists():
                    shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                    updated += 1
                else:
                    shutil.copytree(item, dest)
                    synced += 1
                fingerprints[item.name] = new_fp
            except Exception as exc:
                errors.append(f"{item.name}: {exc}")

        if delete_removed:
            for lesson_name in list(fingerprints.keys()):
                if lesson_name not in repo_lesson_names:
                    dest = lessons_dir / lesson_name
                    if dest.exists():
                        try:
                            shutil.rmtree(dest)
                            deleted += 1
                        except Exception as exc:
                            errors.append(f"delete {lesson_name}: {exc}")
                    del fingerprints[lesson_name]

        sync_meta["fingerprints"] = fingerprints
        sync_meta["last_synced"] = datetime.now(timezone.utc).isoformat()
        _write_sync_meta(lessons_dir, sync_meta)

    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "synced": synced,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
        "errors": errors,
    }


def get_teacher_workspace_status() -> dict:
    lessons = list_teacher_lessons()
    packs = list_built_packs()
    valid = sum(1 for ls in lessons if not ls.get("has_issues", True))

    repo = get_repo_assignments_dir()
    repo_exists = repo.is_dir()
    repo_count = 0
    if repo_exists:
        try:
            repo_count = sum(1 for d in repo.iterdir() if d.is_dir() and not d.name.startswith("."))
        except OSError:
            pass

    return {
        "workspace_dir": TEACHER_WORKSPACE,
        "lessons_dir": get_lessons_dir(),
        "packs_dir": get_built_packs_dir(),
        "repo_dir": repo,
        "repo_exists": repo_exists,
        "repo_lessons": repo_count,
        "total_lessons": len(lessons),
        "valid_lessons": valid,
        "lessons_with_issues": len(lessons) - valid,
        "total_packs": len(packs),
    }
