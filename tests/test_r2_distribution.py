"""Regression coverage for the public student release contract."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

import upload_to_r2


def _student_archive(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pyproject.toml", "[project]\nname='ducky_student'\n")
        archive.writestr("src/ducky/__init__.py", "__build_type__ = 'student'\n")
        archive.writestr("src/ducky/gui/student.py", "")


def _release_manifest(wheel: Path, archive: Path) -> dict:
    base = "https://pub.example/releases/v0.5.2-build123"
    return {
        "build_type": "student",
        "version": "0.5.2",
        "download_url": f"{base}/{archive.name}",
        "zip_url": f"{base}/{archive.name}",
        "wheel_url": f"{base}/{wheel.name}",
        "versioned_zip_url": f"{base}/{archive.name}",
        "versioned_wheel_url": f"{base}/{wheel.name}",
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }


def test_release_manifest_requires_immutable_versioned_urls(tmp_path):
    wheel = tmp_path / "ducky_student-0.5.2-py3-none-any.whl"
    archive = tmp_path / "ducky_student-v0.5.2.zip"
    wheel.write_bytes(b"wheel")
    _student_archive(archive)
    manifest = _release_manifest(wheel, archive)

    upload_to_r2._validate_release_manifest(
        manifest, wheel, archive, "0.5.2", "https://pub.example"
    )

    manifest["download_url"] = "https://pub.example/student-latest.zip"
    with pytest.raises(SystemExit, match="unique immutable"):
        upload_to_r2._validate_release_manifest(
            manifest, wheel, archive, "0.5.2", "https://pub.example"
        )


def test_release_validation_rejects_teacher_module_leak(tmp_path):
    wheel = tmp_path / "ducky_student-0.5.2-py3-none-any.whl"
    archive = tmp_path / "ducky_student-v0.5.2.zip"
    wheel.write_bytes(b"wheel")
    _student_archive(archive)
    with zipfile.ZipFile(archive, "a") as archive_zip:
        archive_zip.writestr("src/ducky/server.py", "teacher server")
    manifest = _release_manifest(wheel, archive)

    with pytest.raises(SystemExit, match="teacher-only"):
        upload_to_r2._validate_release_manifest(
            manifest, wheel, archive, "0.5.2", "https://pub.example"
        )


def test_dry_run_upload_does_not_need_r2_credentials(tmp_path, capsys):
    source = tmp_path / "student-latest.zip"
    source.write_bytes(b"release")

    assert upload_to_r2.upload_file(
        source,
        "student-latest.zip",
        "",
        "",
        dry_run=True,
        cache_control="public, max-age=31536000, immutable",
    )
    assert "[dry-run] student-latest.zip" in capsys.readouterr().out
