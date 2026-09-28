"""Pack and assignment validation for Ducky.

Ensures assignments are complete and valid before they reach students.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from .pack import manifest_relative_path


class ValidationError:
    """A single validation issue."""

    def __init__(self, level: str, message: str, field: str = ""):
        self.level = level  # "error" or "warning"
        self.message = message
        self.field = field

    def __repr__(self) -> str:
        return f"[{self.level.upper()}] {self.message}"


class ValidationResult:
    """Aggregated validation result for a pack."""

    def __init__(self, pack_path: str = ""):
        self.pack_path = pack_path
        self.errors: list[ValidationError] = []
        self.warnings: list[ValidationError] = []

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, message: str, field: str = "") -> None:
        self.errors.append(ValidationError("error", message, field))

    def add_warning(self, message: str, field: str = "") -> None:
        self.warnings.append(ValidationError("warning", message, field))

    def summary(self) -> dict[str, Any]:
        return {
            "pack_path": self.pack_path,
            "valid": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [str(e) for e in self.errors],
            "warnings": [str(w) for w in self.warnings],
        }


def _has_test_function(source: str) -> bool:
    """Check if source code contains a test function or class."""
    return bool(re.search(r"def test_|class Test", source))


def validate_duckypack(pack_path: str | Path) -> ValidationResult:
    """Validate a .duckypack file for structural completeness.

    Checks:
    1. File is a valid ZIP
    2. manifest.json exists and is valid JSON
    3. manifest has required fields (lesson_id, title, entry_file)
    4. starter/ directory exists with at least one .py file
    5. tests/ directory exists with at least one test_*.py file
    6. entry_file exists in starter/
    7. Instructions are present (warning if missing)
    8. No path traversal in ZIP members
    9. Test files contain at least one test function/class
    """
    result = ValidationResult(str(pack_path))
    pack_path = Path(pack_path)

    if not pack_path.is_file():
        result.add_error(f"File not found: {pack_path}")
        return result

    # Check valid ZIP
    try:
        zf = zipfile.ZipFile(pack_path, "r")
    except zipfile.BadZipFile:
        result.add_error("File is not a valid ZIP archive")
        return result

    with zf:
        names = zf.namelist()

        # Check manifest.json
        if "manifest.json" not in names:
            result.add_error("Missing manifest.json")
            return result

        try:
            manifest_data = json.loads(zf.read("manifest.json"))
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
            result.add_error(f"Invalid manifest.json: {exc}")
            return result

        if not isinstance(manifest_data, dict):
            result.add_error("manifest.json must contain a JSON object")
            return result

        # Validate manifest fields
        for field in ["lesson_id", "title", "entry_file"]:
            if not manifest_data.get(field):
                result.add_error(f"manifest.json missing required field: {field}", field)
        if manifest_data.get("entry_file"):
            try:
                manifest_relative_path(str(manifest_data["entry_file"]), "entry_file")
            except ValueError as exc:
                result.add_error(str(exc), "entry_file")
        lesson_id = str(manifest_data.get("lesson_id", ""))
        if lesson_id and not re.fullmatch(r"[A-Za-z0-9_.-]+", lesson_id):
            result.add_error("lesson_id must contain only letters, numbers, '.', '_' or '-'.", "lesson_id")

        # Check for path traversal in any ZIP member
        seen_names: set[str] = set()
        for name in names:
            if name in seen_names:
                result.add_error(f"Duplicate ZIP member: {name}")
            seen_names.add(name)
            if ".." in name or name.startswith("/"):
                result.add_error(f"Unsafe path in ZIP: {name}")
            info = zf.getinfo(name)
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                result.add_error(f"Symlinks are not allowed in ZIP: {name}")

        # Check starter/ directory
        starter_files = [n for n in names if n.startswith("starter/") and not n.endswith("/")]
        if not starter_files:
            result.add_error("Missing starter/ directory or it is empty")
        else:
            py_files = [n for n in starter_files if n.endswith(".py")]
            if not py_files:
                result.add_error("starter/ contains no .py files")

            # Check entry_file exists in starter
            entry = manifest_data.get("entry_file", "")
            if entry:
                entry_path = f"starter/{entry}"
                if entry_path not in names:
                    result.add_error(f"Entry file '{entry}' not found in starter/")

        # Check tests/ directory
        test_files = [n for n in names if n.startswith("tests/") and n.endswith(".py")]
        if not test_files:
            result.add_error("Missing tests/ directory or it has no .py files")
        else:
            named_tests = [n for n in test_files if Path(n).name.startswith("test_")]
            if not named_tests:
                result.add_warning("No test files follow test_*.py naming convention")

            # Check that test files contain actual test functions
            for tf in test_files:
                try:
                    source = zf.read(tf).decode("utf-8")
                    if not _has_test_function(source):
                        result.add_warning(f"Test file '{tf}' contains no test functions or classes")
                except Exception:
                    result.add_warning(f"Could not read test file '{tf}'")

        # Check instructions (warning only)
        has_instructions = "INSTRUCTIONS.md" in names or manifest_data.get("instructions", "").strip() != ""
        if not has_instructions:
            result.add_warning("No instructions found (INSTRUCTIONS.md or manifest instructions)")

        # A quiz is student-facing assessment content. Invalid quiz data must
        # block distribution instead of silently falling back to practice.
        if "quizzes.json" in names:
            try:
                import json as _json

                from .quiz import validate_quiz_dict

                quiz_data = _json.loads(zf.read("quizzes.json"))
                _ok, _errors, _warnings = validate_quiz_dict(quiz_data)
                for _w in _warnings:
                    result.add_warning(f"Quiz: {_w}")
                if not _ok:
                    for _e in _errors:
                        result.add_error(f"Quiz invalid: {_e}", "quizzes.json")
            except Exception as exc:
                result.add_error(f"Quiz present but unreadable: {exc}", "quizzes.json")

    return result


def validate_pack_for_distribution(pack_path: str | Path) -> dict[str, Any]:
    """Validate a pack and return a result dict suitable for CLI output.

    Returns:
        {"ok": bool, "summary": dict, "human_output": str}
    """
    result = validate_duckypack(pack_path)
    summary = result.summary()

    lines = []
    lines.append(f"Validating: {summary['pack_path']}")

    if summary["errors"]:
        lines.append(f"\n  ERRORS ({summary['error_count']}):")
        for err in summary["errors"]:
            lines.append(f"    ✗ {err}")

    if summary["warnings"]:
        lines.append(f"\n  WARNINGS ({summary['warning_count']}):")
        for warn in summary["warnings"]:
            lines.append(f"    ! {warn}")

    if result.ok:
        lines.append("\n  ✓ Pack is valid and ready for distribution.")
    else:
        lines.append(f"\n  ✗ Pack has {summary['error_count']} error(s). Fix before distributing.")

    return {
        "ok": result.ok,
        "summary": summary,
        "human_output": "\n".join(lines),
    }
