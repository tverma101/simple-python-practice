from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from ducky.editor import (
    get_setup_instructions,
    is_vscode_available,
    open_file,
    open_in_file_manager,
    open_in_finder,
    open_lesson,
    show_lesson_info,
)


def test_is_vscode_available_returns_bool():
    result = is_vscode_available()
    assert isinstance(result, bool)


def test_get_setup_instructions_returns_string():
    result = get_setup_instructions()
    assert isinstance(result, str)
    assert "code" in result.lower() or "open" in result.lower()


@patch("ducky.editor.shutil.which")
def test_is_vscode_available_found(mock_which):
    mock_which.return_value = "/usr/bin/code"
    assert is_vscode_available() is True


@patch("ducky.editor.shutil.which")
def test_is_vscode_available_not_found(mock_which):
    mock_which.return_value = None
    assert is_vscode_available() is False


@patch("ducky.editor.shutil.which")
@patch("ducky.editor.subprocess.Popen")
def test_open_lesson_no_vscode(mock_popen, mock_which, tmp_path):
    mock_which.return_value = None
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()
    (lesson_dir / "solution.py").write_text("pass\n")
    result = open_lesson(lesson_dir, "solution.py")
    assert result is False
    mock_popen.assert_not_called()


@patch("ducky.editor.shutil.which", return_value="/usr/bin/code")
@patch("ducky.editor.subprocess.Popen")
def test_open_lesson_with_vscode(mock_popen, mock_which, tmp_path):
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()
    (lesson_dir / "solution.py").write_text("pass\n")
    mock_popen.return_value = MagicMock()
    result = open_lesson(lesson_dir, "solution.py")
    assert result is True


@patch("ducky.editor.shutil.which", return_value="/usr/bin/code")
@patch("ducky.editor.subprocess.Popen")
def test_open_lesson_reuses_current_codespaces_window(mock_popen, mock_which, tmp_path, monkeypatch):
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()
    entry = lesson_dir / "solution.py"
    entry.write_text("pass\n")
    monkeypatch.setenv("CODESPACES", "true")

    assert open_lesson(lesson_dir, "solution.py") is True
    command = mock_popen.call_args[0][0]
    assert command[:3] == ["code", "--reuse-window", "--goto"]
    assert str(entry) in command
    assert str(lesson_dir) not in command


@patch("ducky.editor.is_vscode_available", return_value=True)
@patch("ducky.editor.subprocess.Popen")
def test_open_file_with_vscode(mock_popen, mock_vscode, tmp_path):
    f = tmp_path / "solution.py"
    f.write_text("pass\n")
    mock_popen.return_value = MagicMock()
    result = open_file(f)
    assert result is True


@patch("ducky.editor.shutil.which")
@patch("ducky.editor.subprocess.Popen")
def test_open_file_no_vscode(mock_popen, mock_which, tmp_path):
    mock_which.return_value = None
    f = tmp_path / "solution.py"
    f.write_text("pass\n")
    result = open_file(f)
    assert result is True
    mock_popen.assert_called_once()
    call_args = mock_popen.call_args[0][0]
    if sys.platform == "darwin":
        assert call_args[0] == "open"
    else:
        assert call_args[0] == "xdg-open"


@patch("ducky.editor.shutil.which", return_value="/usr/bin/code")
@patch("ducky.editor.subprocess.Popen")
def test_open_lesson_no_entry(mock_popen, mock_which, tmp_path):
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()
    (lesson_dir / "solution.py").write_text("pass\n")
    mock_popen.return_value = MagicMock()
    result = open_lesson(lesson_dir, "")
    assert result is True


@patch("ducky.editor.sys.platform", "darwin")
@patch("ducky.editor.subprocess.Popen")
def test_open_in_finder_macos(mock_popen, tmp_path):
    d = tmp_path / "testdir"
    d.mkdir()
    mock_popen.return_value = MagicMock()
    result = open_in_finder(d)
    assert result is True


@patch("ducky.editor.sys.platform", "linux")
def test_open_in_finder_non_macos():
    result = open_in_finder(Path("/tmp"))
    assert result is False


@patch("ducky.editor.sys.platform", "darwin")
@patch("ducky.editor.subprocess.Popen")
def test_open_in_finder_nonexistent_path(mock_popen):
    result = open_in_finder(Path("/nonexistent/path/12345"))
    assert result is False


@patch("ducky.editor.sys.platform", "darwin")
@patch("ducky.editor.subprocess.Popen")
def test_open_in_file_manager_macos(mock_popen, tmp_path):
    d = tmp_path / "testdir"
    d.mkdir()
    mock_popen.return_value = MagicMock()
    result = open_in_file_manager(d)
    assert result is True


@patch("ducky.editor.sys.platform", "win32")
def test_open_in_file_manager_windows(tmp_path):
    result = open_in_file_manager(tmp_path)
    assert result is False


@patch("ducky.editor.sys.platform", "darwin")
def test_show_lesson_info(capsys, tmp_path):
    d = tmp_path / "my_lesson"
    d.mkdir()
    (d / "solution.py").write_text("pass\n")
    (d / "tests").mkdir()
    (d / "tests" / "test_solution.py").write_text("def test_x(): pass\n")
    show_lesson_info(d, "solution.py", "My Lesson")
    captured = capsys.readouterr()
    assert "Your Working Directory" in captured.out
    assert str(d) in captured.out
    assert "solution.py" in captured.out
    assert "tests" in captured.out
    assert "Ducky installation:" in captured.out
    # Should show cd command for easy navigation
    assert "cd " in captured.out
    assert "Quick start" in captured.out


@patch("ducky.editor.sys.platform", "darwin")
def test_show_lesson_info_with_instructions(capsys, tmp_path):
    d = tmp_path / "my_lesson"
    d.mkdir()
    (d / "solution.py").write_text("pass\n")
    (d / "tests").mkdir()
    (d / "INSTRUCTIONS.md").write_text("# Instructions\nDo this work.")
    show_lesson_info(d, "solution.py", "My Lesson")
    captured = capsys.readouterr()
    assert "INSTRUCTIONS.md" in captured.out
    assert "read this first" in captured.out
