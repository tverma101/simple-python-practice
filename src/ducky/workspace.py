from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .pack import read_manifest, unpack_duckypack
from .paths import app_dir

# Legacy hardcoded path — kept for backward-compatible migration
_LEGACY_WORKSPACE = Path.home() / "ducky-workspace"

# Old default path — used for backward compat detection
_OLD_DEFAULT = app_dir() / "workspace"


def get_workspace_dir(base: Path | None = None) -> Path:
    """Return the configured workspace directory.

    Resolution order:
    1. ``DUCKY_WORKSPACE`` environment variable
    2. ``workspace_dir`` key in config.json
    3. Legacy: ``~/ducky-workspace`` (if it exists)
    4. Old default: ``~/.ducky/workspace`` (backward compat — only if it has content)
    5. Default: ``<cwd>/assignments`` — visible alongside the student's code
    """
    # 1. Environment variable takes highest priority
    env_ws = os.environ.get("DUCKY_WORKSPACE")
    if env_ws:
        return Path(env_ws).expanduser().resolve()

    # 2. Config file key
    from .config import load_config as _load_config

    config = _load_config(base)
    cfg_ws = config.get("workspace_dir", "")
    if cfg_ws:
        return Path(cfg_ws).expanduser().resolve()

    # 3. Backward-compat: if legacy dir already exists, use it
    try:
        if _LEGACY_WORKSPACE.is_dir():
            return _LEGACY_WORKSPACE
    except OSError:
        pass

    # 4. Backward-compat: if old default (~/.ducky/workspace/) has lesson
    #    directories, keep using it so existing users aren't disrupted.
    try:
        if _OLD_DEFAULT.is_dir() and any(
            d.is_dir() for d in _OLD_DEFAULT.iterdir()
        ):
            return _OLD_DEFAULT
    except OSError:
        pass

    # 5. Default: current working directory / assignments
    #    This puts lessons alongside the user's code where they can find them,
    #    rather than in a hidden ~/.ducky/workspace/ directory.
    return Path.cwd() / "assignments"


def get_student_repo_dir(repo_root: Path | None = None) -> Path:
    """Return the visible repository folder used for student lesson files.

    Codespaces launches may still have a legacy ``workspace_dir`` configured
    under ``~/.ducky``.  Student lesson files should nevertheless be visible
    in the repository so the editor's Explorer and git diff show the work.
    ``DUCKY_STUDENT_REPO`` provides an explicit override for unusual layouts;
    otherwise the nearest git worktree (or the current directory) is used.
    """
    if repo_root is not None:
        return Path(repo_root).expanduser().resolve()

    env_root = os.environ.get("DUCKY_STUDENT_REPO")
    if env_root:
        return Path(env_root).expanduser().resolve()

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def get_student_assignments_dir(repo_root: Path | None = None) -> Path:
    """Return the visible ``assignments/`` folder for a student repository."""
    return get_student_repo_dir(repo_root) / "assignments"


def _lesson_dir_for_id(lesson_id: str) -> Path:
    """Find an existing lesson, preferring the visible student copy.

    Older installs may still have a lesson under ``~/.ducky/workspace``. Once
    a repository copy exists, that is the copy the student edits and the UI
    must report. Keeping the legacy fallback preserves older lessons until
    they are opened again.
    """
    visible = get_student_assignments_dir() / lesson_id
    if visible.is_dir():
        return visible
    configured = get_workspace_dir() / lesson_id
    if configured.is_dir():
        return configured
    return configured


# ── Lesson auto-detection from workspace directories ────────────────

# Prefix used for lesson IDs generated from detected directories so they
# never collide with pack-based lesson_id values.
_DETECTED_LESSON_PREFIX = "workspace."


def _slugify_dir(name: str) -> str:
    """Convert a directory name to a safe lesson_id suffix.

    ``"My Lesson (final)"`` → ``"my_lesson_final"``
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = slug.strip("_")
    return slug if slug else "unnamed"


def scan_workspace_for_lessons() -> list[dict]:
    """Scan workspace subdirectories for lesson-like directories.

    A directory is considered a lesson if it has a ``tests/`` subdirectory
    with at least one ``.py`` file.

    Returns a list of dicts with the same shape as
    :func:`list_available_lessons` entries, plus ``detected: True`` and
    ``pack_file: None``.
    """
    ws = get_workspace_dir()
    try:
        if not ws.is_dir():
            return []
        entries = sorted(ws.iterdir())
    except OSError:
        return []

    detected: list[dict] = []

    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        tests_dir = entry / "tests"
        if not tests_dir.is_dir():
            continue
        test_files = list(tests_dir.glob("*.py"))
        if not test_files:
            continue

        lesson_id = _DETECTED_LESSON_PREFIX + _slugify_dir(entry.name)
        title = entry.name.replace("_", " ").replace("-", " ").title()
        title = re.sub(r"\s+", " ", title).strip()

        # Pick the first .py file in the root (outside tests/) as entry
        entry_files = sorted(
            p for p in entry.glob("*.py")
            if p.parent == entry and not p.name.startswith(".")
        )
        entry_file = entry_files[0].name if entry_files else ""

        meta = _read_meta(entry)
        status = meta.get("status", "not_started")

        detected.append({
            "pack_file": None,
            "lesson_id": lesson_id,
            "title": title,
            "entry_file": entry_file,
            "instructions": (entry / "INSTRUCTIONS.md").read_text(encoding="utf-8")
            if (entry / "INSTRUCTIONS.md").is_file() else "",
            "status": status,
            "workspace_dir": entry,
            "detected": True,
            "lesson_dir": entry,
            "has_quizzes": (
                (entry / "quizzes.json").is_file()
                or any((entry / "quizzes").glob("*.json"))
            ),
        })

    return detected


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta_path(lesson_dir: Path) -> Path:
    return lesson_dir / ".ducky-meta.json"


def _read_meta(lesson_dir: Path) -> dict:
    mp = _meta_path(lesson_dir)
    if mp.is_file():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_meta(lesson_dir: Path, meta: dict) -> None:
    _meta_path(lesson_dir).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def pack_has_quiz(pack_path: str | Path) -> bool:
    """Return True only when ``pack_path`` actually ships a loadable quiz.

    The manifest ``quizzes`` flag alone is unreliable: a pack can declare
    ``quizzes: true`` yet ship no usable ``quizzes.json``, in which case the
    student falls back to the platform practice quiz (see load_student_quiz).
    Basing the menu's "quiz available" marker on the flag would advertise a
    teacher quiz that isn't really there. This checks the embedded quiz
    directly so the marker is truthful.
    """
    try:
        from .quiz import load_quiz_from_pack

        return load_quiz_from_pack(Path(pack_path)) is not None
    except Exception:
        return False


def list_available_lessons() -> list[dict]:
    packs_dir = app_dir() / "packs"
    app_packs_available = packs_dir.is_dir()

    seen: set[str] = set()
    lessons: list[dict] = []

    for pack_file in sorted(packs_dir.glob("*.duckypack")):
        try:
            manifest = read_manifest(pack_file)
        except Exception:
            continue
        seen.add(manifest.lesson_id)

        lesson_dir = _lesson_dir_for_id(manifest.lesson_id)
        status = "not_started"
        if lesson_dir.is_dir():
            meta = _read_meta(lesson_dir)
            status = meta.get("status", "in_progress")

        lessons.append(
            {
                "pack_file": pack_file,
                "lesson_id": manifest.lesson_id,
                "title": manifest.title,
                "entry_file": manifest.entry_file,
                "instructions": manifest.instructions,
                "status": status,
                "workspace_dir": lesson_dir if lesson_dir.is_dir() else None,
                "has_quizzes": pack_has_quiz(pack_file),
                "quiz_declared": bool(manifest.quizzes),
            }
        )

    # Fallback 0: scan the configured workspace_dir (e.g. the assignments/
    # folder `ducky init` creates and tells students to drop .duckypack files
    # into). Without this, packs placed there are never discovered — the menu
    # only scanned app_dir()/packs. See init_workspace.persist workspace_dir.
    ws_dir = get_workspace_dir()
    if ws_dir.is_dir() and ws_dir != packs_dir:
        for pack_file in sorted(ws_dir.glob("*.duckypack")):
            try:
                manifest = read_manifest(pack_file)
            except Exception:
                continue
            if manifest.lesson_id in seen:
                continue
            seen.add(manifest.lesson_id)

            lesson_dir = _lesson_dir_for_id(manifest.lesson_id)
            status = "not_started"
            if lesson_dir.is_dir():
                meta = _read_meta(lesson_dir)
                status = meta.get("status", "in_progress")

            lessons.append(
                {
                    "pack_file": pack_file,
                    "lesson_id": manifest.lesson_id,
                    "title": manifest.title,
                    "entry_file": manifest.entry_file,
                    "instructions": manifest.instructions,
                    "status": status,
                    "workspace_dir": lesson_dir if lesson_dir.is_dir() else None,
                    "has_quizzes": pack_has_quiz(pack_file),
                    "quiz_declared": bool(manifest.quizzes),
                }
            )

    # Fallback 1: also scan examples/packs/ from the repo (for running from source)
    examples_packs = Path(__file__).resolve().parent.parent.parent / "examples" / "packs"
    if app_packs_available and examples_packs.is_dir():
        for pack_file in sorted(examples_packs.glob("*.duckypack")):
            try:
                manifest = read_manifest(pack_file)
            except Exception:
                continue
            if manifest.lesson_id in seen:
                continue
            seen.add(manifest.lesson_id)

            lesson_dir = _lesson_dir_for_id(manifest.lesson_id)
            status = "not_started"
            if lesson_dir.is_dir():
                meta = _read_meta(lesson_dir)
                status = meta.get("status", "in_progress")

            lessons.append(
                {
                    "pack_file": pack_file,
                    "lesson_id": manifest.lesson_id,
                    "title": manifest.title,
                    "entry_file": manifest.entry_file,
                    "instructions": manifest.instructions,
                    "status": status,
                    "workspace_dir": lesson_dir if lesson_dir.is_dir() else None,
                    "has_quizzes": pack_has_quiz(pack_file),
                    "quiz_declared": bool(manifest.quizzes),
                }
            )

    # Fallback 2: scan teacher built_packs (so imported ZIPs are visible to students)
    teacher_packs = app_dir() / "teacher" / "workspace" / "built_packs"
    if teacher_packs.is_dir():
        for pack_file in sorted(teacher_packs.glob("*.duckypack")):
            try:
                manifest = read_manifest(pack_file)
            except Exception:
                continue
            if manifest.lesson_id in seen:
                continue
            seen.add(manifest.lesson_id)

            lesson_dir = _lesson_dir_for_id(manifest.lesson_id)
            status = "not_started"
            if lesson_dir.is_dir():
                meta = _read_meta(lesson_dir)
                status = meta.get("status", "in_progress")

            lessons.append(
                {
                    "pack_file": pack_file,
                    "lesson_id": manifest.lesson_id,
                    "title": manifest.title,
                    "entry_file": manifest.entry_file,
                    "instructions": manifest.instructions,
                    "status": status,
                    "workspace_dir": lesson_dir if lesson_dir.is_dir() else None,
                    "has_quizzes": pack_has_quiz(pack_file),
                    "quiz_declared": bool(manifest.quizzes),
                }
            )

    # Append auto-detected workspace lessons (deduplicated by name)
    detected = scan_workspace_for_lessons()
    for dl in detected:
        # Deduplicate: skip if a pack with the same dir name already covered it
        dir_name = Path(dl["workspace_dir"]).name if dl["workspace_dir"] else ""
        already_covered = any(
            Path(ls.get("workspace_dir") or "").name == dir_name
            for ls in lessons
        )
        if not already_covered and dl["lesson_id"] not in seen:
            seen.add(dl["lesson_id"])
            lessons.append(dl)

    return lessons


def _write_student_runner(lesson_dir: Path, test_command: str) -> None:
    """Add a portable one-command test runner to a visible lesson folder."""
    runner = lesson_dir / "run_tests.py"
    runner.write_text(
        """#!/usr/bin/env python3
\"\"\"Run this lesson's tests from any terminal or Codespaces editor.\"\"\"

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COMMAND = %r

if __name__ == \"__main__\":
    raise SystemExit(subprocess.call([*COMMAND], cwd=ROOT))
""" % (test_command.split() or [sys.executable, "-m", "pytest", "tests"]),
        encoding="utf-8",
    )


def _write_student_files(lesson_dir: Path, manifest) -> None:
    """Create editor-friendly aliases and helpers without hiding pack files."""
    instructions = lesson_dir / "INSTRUCTIONS.md"
    readable = lesson_dir / "instructions.md"
    if instructions.is_file() and not readable.is_file():
        shutil.copy2(instructions, readable)
    elif readable.is_file() and not instructions.is_file():
        shutil.copy2(readable, instructions)
    elif not instructions.is_file() and not readable.is_file():
        readable.write_text(
            f"# {manifest.title}\n\n"
            "Edit the starter code, then run `python run_tests.py` from this folder.\n",
            encoding="utf-8",
        )
    _write_student_runner(lesson_dir, manifest.test_command)


_TEACHER_OWNED_FILES = {
    "INSTRUCTIONS.md",
    "instructions.md",
    "manifest.json",
    "quizzes.json",
    "run_tests.py",
    ".ducky-meta.json",
}


def _is_student_source_file(relative_path: Path) -> bool:
    """Return whether a deployed lesson file belongs to the student's work.

    Packs contain both student-owned starter files and teacher-owned tests and
    instructions.  Keeping this rule in one place makes lesson refreshes safe:
    teacher changes can replace tests/instructions while edited starter files
    remain under the student's control.
    """
    if not relative_path.parts:
        return False
    if relative_path.parts[0] in {"tests", ".ducky-source", ".ducky-conflicts"}:
        return False
    return relative_path.name not in _TEACHER_OWNED_FILES


def _snapshot_student_source(lesson_dir: Path) -> None:
    """Record the teacher-provided starter baseline for future 3-way updates."""
    snapshot = lesson_dir / ".ducky-source"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.mkdir(parents=True, exist_ok=True)
    for path in lesson_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(lesson_dir)
        if not _is_student_source_file(relative):
            continue
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _same_file(left: Path, right: Path) -> bool:
    """Compare files without treating a missing or unreadable file as equal."""
    if not left.is_file() or not right.is_file():
        return False
    try:
        return hashlib.sha256(left.read_bytes()).digest() == hashlib.sha256(right.read_bytes()).digest()
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    """Return a file fingerprint, or an empty string when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def refresh_lesson_from_pack(
    pack_path: Path | str,
    destination_root: Path | None = None,
) -> dict:
    """Apply a teacher pack update to an existing student lesson safely.

    The current lesson is moved to a timestamped backup before the replacement
    becomes visible.  Teacher-owned files (tests, instructions, quiz, and
    manifest) come from the new pack.  Starter files are merged against the
    original teacher snapshot: unchanged files receive teacher updates, while
    edited files stay with the student and the incoming version is saved under
    ``.ducky-conflicts/``.

    Returns a small UI-friendly result containing ``updated``, ``backup_path``
    and any relative paths that need review.
    """
    pack_path = Path(pack_path)
    if not pack_path.is_file():
        raise FileNotFoundError(f"Pack file not found: {pack_path}")
    manifest = read_manifest(pack_path)
    root = Path(destination_root).expanduser().resolve() if destination_root else get_workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    lesson_dir = root / manifest.lesson_id

    if not lesson_dir.is_dir():
        created = start_lesson(pack_path, destination_root=root)
        return {
            "updated": False,
            "created": True,
            "lesson_dir": created,
            "backup_path": None,
            "conflicts": [],
        }

    old_meta = _read_meta(lesson_dir)
    old_snapshot = lesson_dir / ".ducky-source"
    work_root = Path(tempfile.mkdtemp(prefix=f".ducky-{manifest.lesson_id}-", dir=root))
    incoming = work_root / manifest.lesson_id
    backup_root = root / ".ducky-backups" / manifest.lesson_id
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    conflicts: list[str] = []

    try:
        start_lesson(pack_path, destination_root=work_root)

        # Merge only student-owned files.  With no baseline (older installs),
        # preserve all recognizable student source files rather than guessing
        # that an edit is safe to overwrite.
        for current in lesson_dir.rglob("*"):
            if not current.is_file():
                continue
            relative = current.relative_to(lesson_dir)
            if not _is_student_source_file(relative):
                continue
            baseline = old_snapshot / relative
            incoming_file = incoming / relative
            changed = not old_snapshot.is_dir() or not baseline.is_file() or not _same_file(current, baseline)
            if not changed:
                continue
            incoming_changed = incoming_file.is_file() and not _same_file(current, incoming_file)
            incoming_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, incoming_file)
            if incoming_changed:
                conflict_path = incoming / ".ducky-conflicts" / backup_path.name / relative
                conflict_path.parent.mkdir(parents=True, exist_ok=True)
                # The incoming teacher copy was overwritten above; recover it
                # from the freshly unpacked source before recording the conflict.
                # The temporary unpack was the only untouched copy, so read the
                # teacher version directly from the archive.
                with zipfile.ZipFile(pack_path) as archive:
                    member = f"starter/{relative.as_posix()}"
                    try:
                        conflict_path.write_bytes(archive.read(member))
                    except KeyError:
                        conflict_path.write_bytes(incoming_file.read_bytes())
                conflicts.append(relative.as_posix())

        new_meta = _read_meta(incoming)
        new_meta.update({
            "status": old_meta.get("status", "in_progress"),
            "started_at": old_meta.get("started_at", new_meta.get("started_at")),
            "completed_at": None,
            "last_test_passed": False,
            "teacher_update_at": _now_iso(),
            "teacher_update_conflicts": conflicts,
        })
        _write_meta(incoming, new_meta)

        # Keep the old lesson recoverable and make the directory swap visible
        # as one rename sequence.  If the second rename fails, restore it.
        lesson_dir.rename(backup_path)
        try:
            incoming.rename(lesson_dir)
        except Exception:
            backup_path.rename(lesson_dir)
            raise
        return {
            "updated": True,
            "created": False,
            "lesson_dir": lesson_dir,
            "backup_path": backup_path,
            "conflicts": conflicts,
        }
    except Exception:
        shutil.rmtree(work_root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def start_lesson(pack_path: Path, destination_root: Path | None = None) -> Path:
    pack_path = Path(pack_path)
    if not pack_path.is_file():
        raise FileNotFoundError(f"Pack file not found: {pack_path}")

    manifest = read_manifest(pack_path)
    root = Path(destination_root).expanduser().resolve() if destination_root else get_workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    lesson_dir = root / manifest.lesson_id

    if lesson_dir.is_dir():
        meta = _read_meta(lesson_dir)
        # A visible repository lesson is the student's working copy. Never
        # erase edits merely because the GUI was opened again. If the pack was
        # replaced by background sync, merge the teacher update first.
        if meta.get("lesson_id") == manifest.lesson_id:
            current_pack_hash = _sha256_file(pack_path)
            if (
                current_pack_hash
                and meta.get("pack_sha256")
                and meta.get("pack_sha256") != current_pack_hash
            ):
                refreshed = refresh_lesson_from_pack(pack_path, destination_root=root)
                return refreshed["lesson_dir"]
            _write_student_files(lesson_dir, manifest)
            return lesson_dir
        if meta.get("status") == "completed":
            return lesson_dir
        # An older install or a hand-created assignment may not have Ducky
        # metadata. It is still user data: never delete it just because a pack
        # uses the same folder name. Make the collision explicit so the student
        # can move/rename the folder or choose the correct pack.
        raise FileExistsError(
            f"Lesson folder already exists without matching Ducky metadata: {lesson_dir}. "
            "Existing files were left untouched."
        )
    if lesson_dir.exists():
        raise FileExistsError(
            f"Cannot start lesson because the destination is not a folder: {lesson_dir}"
        )

    try:
        manifest, extracted = unpack_duckypack(pack_path, lesson_dir)
    except Exception as exc:
        if lesson_dir.is_dir():
            shutil.rmtree(lesson_dir)
        raise RuntimeError(f"Failed to unpack lesson: {exc}") from exc

    tests_src = lesson_dir / "tests"
    tests_dest = lesson_dir / "_tests_copy"
    if tests_src.is_dir():
        shutil.copytree(tests_src, tests_dest)
        shutil.rmtree(tests_src)

    try:
        starter_dir = lesson_dir / "starter"
        if starter_dir.is_dir():
            for item in starter_dir.iterdir():
                dest = lesson_dir / item.name
                if item.is_file():
                    shutil.copy2(item, dest)
                elif item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
            shutil.rmtree(starter_dir)
    except Exception:
        # Restore tests directory if starter copy fails — prevents orphaned state
        if tests_dest.is_dir():
            shutil.copytree(tests_dest, lesson_dir / "tests")
        raise

    if tests_dest.is_dir():
        shutil.move(str(tests_dest), str(lesson_dir / "tests"))

    # Ensure INSTRUCTIONS.md is available as a file in the lesson directory
    instructions_file = lesson_dir / "INSTRUCTIONS.md"
    if not instructions_file.is_file() and manifest.instructions:
        instructions_file.write_text(manifest.instructions, encoding="utf-8")

    if destination_root is not None:
        _write_student_files(lesson_dir, manifest)

    _snapshot_student_source(lesson_dir)

    meta = {
        "lesson_id": manifest.lesson_id,
        "title": manifest.title,
        "entry_file": manifest.entry_file,
        "pack_path": str(pack_path),
        "pack_sha256": _sha256_file(pack_path),
        "status": "in_progress",
        "started_at": _now_iso(),
        "completed_at": None,
        "last_test_passed": False,
    }
    _write_meta(lesson_dir, meta)

    return lesson_dir


def get_current_lesson() -> dict | None:
    workspace_dirs = [get_workspace_dir()]
    visible_workspace = get_student_assignments_dir()
    if visible_workspace not in workspace_dirs:
        workspace_dirs.append(visible_workspace)
    if not any(path.is_dir() for path in workspace_dirs):
        return None

    best = None
    best_mtime = 0
    for workspace_dir in workspace_dirs:
        if not workspace_dir.is_dir():
            continue
        for lesson_dir in workspace_dir.iterdir():
            if not lesson_dir.is_dir():
                continue
            mp = _meta_path(lesson_dir)
            if mp.is_file():
                mtime = mp.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = lesson_dir

    if best is None:
        # Fallback: scan workspace for detected lesson directories
        detected = scan_workspace_for_lessons()
        if len(detected) == 1:
            return detected[0]
        if not detected:
            return None
        # Multiple detected lessons — return the most recently modified
        latest = max(
            detected,
            key=lambda d: d["lesson_dir"].stat().st_mtime
            if d["lesson_dir"].is_dir() else 0,
        )
        return latest

    meta = _read_meta(best)
    return {
        "lesson_dir": best,
        "lesson_id": meta.get("lesson_id", best.name),
        "title": meta.get("title", best.name),
        "entry_file": meta.get("entry_file", ""),
        "status": meta.get("status", "in_progress"),
    }


def get_lesson_dir(lesson_id: str) -> Path | None:
    lesson_dir = _lesson_dir_for_id(lesson_id)
    if lesson_dir.is_dir() and _meta_path(lesson_dir).is_file():
        return lesson_dir
    return None


def get_lesson_entry_path(lesson_id: str) -> Path | None:
    lesson_dir = get_lesson_dir(lesson_id)
    if lesson_dir is None:
        return None
    meta = _read_meta(lesson_dir)
    entry = meta.get("entry_file", "")
    if entry:
        p = lesson_dir / entry
        if p.is_file():
            return p
    return None


def get_lesson_instructions(lesson_id: str) -> str | None:
    lesson_dir = get_lesson_dir(lesson_id)
    if lesson_dir is None:
        return None

    instructions_file = lesson_dir / "INSTRUCTIONS.md"
    if instructions_file.is_file():
        return instructions_file.read_text(encoding="utf-8")

    pack_path_str = _read_meta(lesson_dir).get("pack_path", "")
    if pack_path_str:
        try:
            manifest = read_manifest(pack_path_str)
            if manifest.instructions:
                return manifest.instructions
        except Exception:
            pass

    return None


def mark_completed(lesson_id: str) -> None:
    lesson_dir = get_lesson_dir(lesson_id)
    if lesson_dir is None:
        return
    meta = _read_meta(lesson_dir)
    meta["status"] = "completed"
    meta["completed_at"] = _now_iso()
    meta["last_test_passed"] = True
    _write_meta(lesson_dir, meta)


def mark_test_result(lesson_id: str, passed: bool) -> None:
    lesson_dir = get_lesson_dir(lesson_id)
    if lesson_dir is None:
        return
    meta = _read_meta(lesson_dir)
    meta["last_test_passed"] = passed
    if passed:
        meta["status"] = "completed"
        meta["completed_at"] = _now_iso()
    _write_meta(lesson_dir, meta)


def get_progress() -> dict:
    lessons = list_available_lessons()
    total = len(lessons)
    completed = sum(1 for ls in lessons if ls["status"] == "completed")
    in_progress = sum(1 for ls in lessons if ls["status"] == "in_progress")
    return {
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
        "not_started": total - completed - in_progress,
    }
