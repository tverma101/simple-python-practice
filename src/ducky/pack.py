from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def manifest_relative_path(value: str, field_name: str = "path") -> Path:
    text = str(value).replace("\\", "/").strip()
    path = Path(text)
    if not text or path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must be a relative path inside the lesson pack")
    return path


@dataclass
class DuckyManifest:
    lesson_id: str
    title: str
    entry_file: str
    language: str = "python"
    pack_version: str = "1.0"
    test_command: str = "python -m pytest tests/"
    concept_tags: list[str] = field(default_factory=list)
    diagnostic_cases: list[dict[str, Any]] = field(default_factory=list)
    instructions: str = ""
    quizzes: bool = False

    def __post_init__(self) -> None:
        self.lesson_id = str(self.lesson_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.lesson_id):
            raise ValueError(
                "lesson_id must contain only letters, numbers, '.', '_' or '-'."
            )
        if not str(self.title).strip():
            raise ValueError("title must not be empty")
        self.entry_file = manifest_relative_path(self.entry_file, "entry_file").as_posix()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DuckyManifest:
        if not isinstance(data, dict):
            raise ValueError("manifest must be a JSON object")
        required = ["lesson_id", "title", "entry_file"]
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(f"manifest missing required field(s): {', '.join(missing)}")
        entry_file = manifest_relative_path(str(data["entry_file"]), "entry_file").as_posix()
        concept_tags = data.get("concept_tags", [])
        if not isinstance(concept_tags, list):
            raise ValueError("concept_tags must be a list")
        diagnostic_cases = data.get("diagnostic_cases", [])
        if not isinstance(diagnostic_cases, list):
            raise ValueError("diagnostic_cases must be a list")
        return cls(
            lesson_id=str(data["lesson_id"]),
            title=str(data["title"]),
            entry_file=entry_file,
            language=str(data.get("language", "python")),
            pack_version=str(data.get("pack_version", "1.0")),
            test_command=str(data.get("test_command", "python -m pytest tests/")),
            concept_tags=[str(tag) for tag in concept_tags],
            diagnostic_cases=diagnostic_cases,
            instructions=str(data.get("instructions", "")),
            quizzes=bool(data.get("quizzes", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "pack_version": self.pack_version,
            "lesson_id": self.lesson_id,
            "title": self.title,
            "language": self.language,
            "entry_file": self.entry_file,
            "test_command": self.test_command,
            "concept_tags": self.concept_tags,
            "diagnostic_cases": self.diagnostic_cases,
        }
        if self.instructions:
            result["instructions"] = self.instructions
        if self.quizzes:
            result["quizzes"] = True
        return result


def _safe_arcname(base: Path, file_path: Path, prefix: str) -> str:
    rel = file_path.relative_to(base)
    if any(part in {"..", ""} for part in rel.parts):
        raise ValueError(f"unsafe path in pack: {file_path}")
    return str(Path(prefix) / rel).replace("\\", "/")


def create_duckypack(
    starter_dir: str | Path,
    tests_dir: str | Path,
    out_path: str | Path,
    manifest: DuckyManifest,
    source_dir: str | Path | None = None,
    quizzes_path: str | Path | None = None,
) -> Path:
    starter_dir = Path(starter_dir).resolve()
    tests_dir = Path(tests_dir).resolve()
    out_path = Path(out_path).resolve()

    if not starter_dir.is_dir():
        raise FileNotFoundError(f"starter directory not found: {starter_dir}")
    if not tests_dir.is_dir():
        raise FileNotFoundError(f"tests directory not found: {tests_dir}")
    entry_file = manifest_relative_path(manifest.entry_file, "entry_file")
    if not (starter_dir / entry_file).is_file():
        raise FileNotFoundError(f"entry_file not found in starter dir: {manifest.entry_file}")

    if not manifest.instructions and source_dir:
        src = Path(source_dir).resolve()
        instructions_file = src / "INSTRUCTIONS.md"
        if instructions_file.is_file():
            manifest.instructions = instructions_file.read_text(encoding="utf-8")

    # quizzes.json is authored teacher content shipped to students; it is
    # embedded verbatim when provided (validated upstream by quiz.validate_quiz_dict).
    quizzes_bytes: bytes | None = None
    if quizzes_path is not None:
        quizzes_file = Path(quizzes_path).resolve()
        if quizzes_file.is_file():
            quizzes_bytes = quizzes_file.read_bytes()
            manifest.quizzes = True

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{out_path.name}.", suffix=".tmp", dir=out_path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest.to_dict(), indent=2))
            for file_path in starter_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, _safe_arcname(starter_dir, file_path, "starter"))
            for file_path in tests_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, _safe_arcname(tests_dir, file_path, "tests"))
            if source_dir:
                instructions_file = Path(source_dir).resolve() / "INSTRUCTIONS.md"
                if instructions_file.is_file():
                    zf.write(instructions_file, "INSTRUCTIONS.md")
            if quizzes_bytes is not None:
                zf.writestr("quizzes.json", quizzes_bytes)
        Path(temporary).replace(out_path)
    except Exception:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return out_path


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    seen_names: set[str] = set()
    for member in zf.infolist():
        name = member.filename.replace("\\", "/")
        if name in seen_names:
            raise ValueError(f"duplicate zip member: {member.filename}")
        seen_names.add(name)
        mode = (member.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError(f"symlinks are not allowed in lesson packs: {member.filename}")
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest)
        except ValueError as exc:
            raise ValueError(f"unsafe zip member: {member.filename}") from exc
    zf.extractall(dest)


def unpack_duckypack(pack_path: str | Path, dest_dir: str | Path) -> tuple[DuckyManifest, Path]:
    pack_path = Path(pack_path).resolve()
    dest_dir = Path(dest_dir).resolve()
    if not pack_path.is_file():
        raise FileNotFoundError(f"duckypack not found: {pack_path}")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)

    # Extract and validate beside the destination first. A truncated or corrupt
    # teacher download must never erase an existing student lesson directory.
    staging = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}.", suffix=".unpack", dir=dest_dir.parent))
    backup_root: Path | None = None
    installed = False
    try:
        with zipfile.ZipFile(pack_path, "r") as zf:
            if "manifest.json" not in zf.namelist():
                raise ValueError("duckypack missing manifest.json")
            _safe_extract(zf, staging)
        manifest_data = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        manifest = DuckyManifest.from_dict(manifest_data)

        if dest_dir.exists():
            backup_root = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}.", suffix=".old", dir=dest_dir.parent))
            dest_dir.rename(backup_root / "previous")
        staging.rename(dest_dir)
        installed = True
    except Exception:
        if installed and dest_dir.exists():
            if dest_dir.is_dir() and not dest_dir.is_symlink():
                shutil.rmtree(dest_dir, ignore_errors=True)
            else:
                dest_dir.unlink(missing_ok=True)
        if backup_root is not None and (backup_root / "previous").exists():
            (backup_root / "previous").rename(dest_dir)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)
    return manifest, dest_dir


def read_manifest(pack_path: str | Path) -> DuckyManifest:
    with zipfile.ZipFile(pack_path, "r") as zf:
        data = json.loads(zf.read("manifest.json").decode("utf-8"))
    return DuckyManifest.from_dict(data)
