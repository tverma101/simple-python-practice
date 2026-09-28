"""Tests for the pack validator module."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from ducky.validator import (
    ValidationResult,
    validate_duckypack,
    validate_pack_for_distribution,
)


def _make_test_pack(
    tmp_path: Path,
    *,
    include_starter: bool = True,
    include_tests: bool = True,
    include_instructions: bool = True,
    include_entry: bool = True,
    test_has_function: bool = True,
) -> Path:
    """Helper to create a test .duckypack file."""
    pack_path = tmp_path / "test.duckypack"

    manifest = {
        "lesson_id": "test_lesson",
        "title": "Test Lesson",
        "entry_file": "solution.py",
        "instructions": "Test instructions" if include_instructions else "",
    }

    starter_content = "def main():\n    pass\n"
    test_content = "def test_main():\n    assert True\n" if test_has_function else "x = 1\n"

    with zipfile.ZipFile(pack_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        if include_starter:
            zf.writestr("starter/solution.py", starter_content)
        if include_tests:
            zf.writestr("tests/test_solution.py", test_content)
        if include_instructions:
            zf.writestr("INSTRUCTIONS.md", "# Instructions\n")

    return pack_path


class TestValidationResult:
    def test_ok_when_no_errors(self):
        result = ValidationResult("test.duckypack")
        assert result.ok is True

    def test_not_ok_with_errors(self):
        result = ValidationResult("test.duckypack")
        result.add_error("Something is wrong")
        assert result.ok is False

    def test_warnings_dont_affect_ok(self):
        result = ValidationResult("test.duckypack")
        result.add_warning("Something might be wrong")
        assert result.ok is True

    def test_summary_format(self):
        result = ValidationResult("test.duckypack")
        result.add_error("error msg")
        result.add_warning("warning msg")
        summary = result.summary()
        assert summary["valid"] is False
        assert summary["error_count"] == 1
        assert summary["warning_count"] == 1
        assert any("error msg" in e for e in summary["errors"])
        assert any("warning msg" in w for w in summary["warnings"])


class TestValidateDuckypack:
    def test_valid_pack(self, tmp_path):
        pack = _make_test_pack(tmp_path)
        result = validate_duckypack(pack)
        assert result.ok is True

    def test_file_not_found(self, tmp_path):
        result = validate_duckypack(tmp_path / "nonexistent.duckypack")
        assert result.ok is False
        assert "File not found" in result.errors[0].message

    def test_invalid_zip(self, tmp_path):
        bad_zip = tmp_path / "bad.duckypack"
        bad_zip.write_text("not a zip file")
        result = validate_duckypack(bad_zip)
        assert result.ok is False
        assert "not a valid ZIP" in result.errors[0].message

    def test_missing_manifest(self, tmp_path):
        pack_path = tmp_path / "no_manifest.duckypack"
        with zipfile.ZipFile(pack_path, "w") as zf:
            zf.writestr("starter/solution.py", "pass")
            zf.writestr("tests/test_solution.py", "def test_pass(): assert True")
        result = validate_duckypack(pack_path)
        assert result.ok is False
        assert "Missing manifest.json" in result.errors[0].message

    def test_missing_lesson_id(self, tmp_path):
        pack_path = tmp_path / "no_id.duckypack"
        manifest = {"title": "Test", "entry_file": "solution.py"}
        with zipfile.ZipFile(pack_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("starter/solution.py", "pass")
            zf.writestr("tests/test_solution.py", "def test_pass(): assert True")
        result = validate_duckypack(pack_path)
        assert result.ok is False
        assert any("lesson_id" in e.message for e in result.errors)

    def test_missing_starter(self, tmp_path):
        pack_path = tmp_path / "no_starter.duckypack"
        manifest = {"lesson_id": "test", "title": "Test", "entry_file": "solution.py"}
        with zipfile.ZipFile(pack_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("tests/test_solution.py", "def test_pass(): assert True")
        result = validate_duckypack(pack_path)
        assert result.ok is False
        assert any("starter" in e.message for e in result.errors)

    def test_missing_tests(self, tmp_path):
        pack_path = tmp_path / "no_tests.duckypack"
        manifest = {"lesson_id": "test", "title": "Test", "entry_file": "solution.py"}
        with zipfile.ZipFile(pack_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("starter/solution.py", "pass")
        result = validate_duckypack(pack_path)
        assert result.ok is False
        assert any("tests" in e.message for e in result.errors)

    def test_missing_entry_file(self, tmp_path):
        pack_path = tmp_path / "no_entry.duckypack"
        manifest = {"lesson_id": "test", "title": "Test", "entry_file": "missing.py"}
        with zipfile.ZipFile(pack_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("starter/solution.py", "pass")
            zf.writestr("tests/test_solution.py", "def test_pass(): assert True")
        result = validate_duckypack(pack_path)
        assert result.ok is False
        assert any("Entry file" in e.message for e in result.errors)

    def test_no_instructions_warning(self, tmp_path):
        pack = _make_test_pack(tmp_path, include_instructions=False)
        result = validate_duckypack(pack)
        assert result.ok is True  # warnings don't fail
        assert any("instructions" in w.message.lower() for w in result.warnings)

    def test_path_traversal_fails(self, tmp_path):
        pack_path = tmp_path / "traversal.duckypack"
        manifest = {"lesson_id": "test", "title": "Test", "entry_file": "solution.py"}
        with zipfile.ZipFile(pack_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("starter/solution.py", "pass")
            zf.writestr("tests/test_solution.py", "def test_pass(): assert True")
            zf.writestr("../../../etc/passwd", "bad")
        result = validate_duckypack(pack_path)
        assert result.ok is False
        assert any("Unsafe path" in e.message for e in result.errors)

    def test_test_file_no_functions_warning(self, tmp_path):
        pack = _make_test_pack(tmp_path, test_has_function=False)
        result = validate_duckypack(pack)
        assert result.ok is True
        assert any("no test functions" in w.message.lower() for w in result.warnings)


class TestValidatePackForDistribution:
    def test_valid_pack(self, tmp_path):
        pack = _make_test_pack(tmp_path)
        result = validate_pack_for_distribution(pack)
        assert result["ok"] is True
        assert "valid" in result["human_output"].lower()

    def test_invalid_pack(self, tmp_path):
        pack_path = tmp_path / "bad.duckypack"
        pack_path.write_text("not a zip")
        result = validate_pack_for_distribution(pack_path)
        assert result["ok"] is False
        assert "ERROR" in result["human_output"]
