"""Small, shared services for teacher-side assignment authoring."""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lesson_builder import (
    _validate_authoring_sources,
    detect_lesson,
    find_quiz_sources,
)
from .pack import DuckyManifest
from .quiz import validate_quiz_dict


@dataclass
class QuizDatabaseImport:
    """Validated questions read from a teacher-owned SQLite question bank."""

    questions: list[dict[str, Any]] = field(default_factory=list)
    table: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.questions) and not self.errors

    @property
    def quiz_count(self) -> int:
        return len(self.questions)


_QUESTION_TABLE_NAMES = ("questions", "quiz_questions", "question_bank", "items")
_COLUMN_ALIASES = {
    "id": ("id", "question_id", "uuid", "key"),
    "type": ("type", "question_type", "kind", "format"),
    "prompt": ("prompt", "question", "question_text", "text", "body"),
    "options": ("options", "choices", "answers", "options_json", "choices_json"),
    "correct": ("correct", "answer", "correct_answer", "expected", "solution"),
    "code": ("code", "code_snippet", "snippet", "source_code"),
    "explanation": ("explanation", "rationale", "feedback"),
    "concept": ("concept", "topic", "category", "concept_tag"),
    "difficulty": ("difficulty", "level"),
}
_OPTION_COLUMN_ALIASES = (
    ("option_a", "choice_a", "answer_a", "option1", "choice1"),
    ("option_b", "choice_b", "answer_b", "option2", "choice2"),
    ("option_c", "choice_c", "answer_c", "option3", "choice3"),
    ("option_d", "choice_d", "answer_d", "option4", "choice4"),
    ("option_e", "choice_e", "answer_e", "option5", "choice5"),
)


def _sqlite_identifier(name: str) -> str:
    """Quote an identifier after it has come from SQLite metadata."""
    return '"' + name.replace('"', '""') + '"'


def _first_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    by_lower = {column.lower(): column for column in columns}
    return next((by_lower[name] for name in aliases if name in by_lower), None)


def _decode_database_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith(("[", "{", '"')):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return value


def _database_options(value: Any) -> list[str]:
    value = _decode_database_value(value)
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        for separator in ("\n", "|", "||", ";"):
            if separator in value:
                return [item.strip() for item in value.split(separator) if item.strip()]
        return [value] if value else []
    return []


def _database_question_type(raw: Any, *, options: list[str], code: str, correct: Any) -> str:
    token = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"mcq", "choice", "multiplechoice", "multiple_choice"}:
        return "multiple_choice"
    if token in {"tf", "boolean", "truefalse", "true_false"}:
        return "true_false"
    if token in {"output", "predict", "predict_output", "code_output"}:
        return "predict_output"
    if options:
        return "multiple_choice"
    if code:
        return "predict_output"
    if isinstance(correct, bool) or str(correct).strip().lower() in {
        "true", "false", "yes", "no", "1", "0"
    }:
        return "true_false"
    return "predict_output"


def _stable_question_id(value: Any, index: int, prompt: str) -> str:
    raw = str(value or "").strip()
    if raw:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_")
        if safe:
            return safe[:80]
    import hashlib

    digest = hashlib.sha1(f"{index}:{prompt}".encode()).hexdigest()[:10]
    return f"imported_{index}_{digest}"


def load_quiz_questions_from_database(
    database: str | Path,
    *,
    table: str | None = None,
    limit: int | None = None,
) -> QuizDatabaseImport:
    """Read a common SQLite question-bank layout without modifying the DB.

    The importer accepts a table named ``questions``, ``quiz_questions``,
    ``question_bank`` or ``items`` by default. Column names are matched using
    common aliases, so a Python quiz database can use ``question``/``answer``
    or ``prompt``/``correct``. JSON arrays, newline/pipe-separated choices,
    and explicit question types are supported.
    """
    path = Path(database).expanduser().resolve()
    result = QuizDatabaseImport(source=str(path))
    if not path.is_file():
        result.errors.append(f"Question database not found: {path}")
        return result
    if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".database"}:
        result.warnings.append("The file does not use a typical SQLite extension; it will still be checked as SQLite.")

    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables = [str(row[0]) for row in table_rows]
            table_by_lower = {candidate.lower(): candidate for candidate in tables}
            selected = table_by_lower.get(table.lower()) if isinstance(table, str) else None
            if selected is None:
                selected = next(
                    (table_by_lower[candidate] for candidate in _QUESTION_TABLE_NAMES if candidate in table_by_lower),
                    None,
                )
                if selected is None:
                    matches = []
                    for candidate in tables:
                        columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_sqlite_identifier(candidate)})")]
                        if _first_column(columns, _COLUMN_ALIASES["prompt"]):
                            matches.append(candidate)
                    if len(matches) == 1:
                        selected = matches[0]
            if not selected:
                available = ", ".join(tables) or "none"
                result.errors.append(f"No question table found. Available tables: {available}")
                return result
            if selected not in tables:
                result.errors.append(f"Question table not found: {selected}")
                return result
            result.table = selected
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_sqlite_identifier(selected)})")]
            prompt_column = _first_column(columns, _COLUMN_ALIASES["prompt"])
            if not prompt_column:
                result.errors.append(f"Table '{selected}' needs a prompt/question/text column.")
                return result
            mapping = {key: _first_column(columns, aliases) for key, aliases in _COLUMN_ALIASES.items()}
            option_columns = [_first_column(columns, aliases) for aliases in _OPTION_COLUMN_ALIASES]
            sql = f"SELECT * FROM {_sqlite_identifier(selected)}"
            if limit is not None:
                if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                    result.errors.append("Import limit must be at least 1.")
                    return result
                sql += f" LIMIT {int(limit)}"
            rows = connection.execute(sql).fetchall()
            seen_ids: set[str] = set()
            for index, row in enumerate(rows, 1):
                raw = {key: row[columns.index(column)] for key, column in mapping.items() if column}
                prompt = str(raw.get("prompt") or "").strip()
                if not prompt:
                    result.warnings.append(f"Skipped row {index}: empty question text.")
                    continue
                options = _database_options(raw.get("options"))
                correct_value = _decode_database_value(raw.get("correct"))
                if not options:
                    options = [
                        str(row[columns.index(column)])
                        for column in option_columns
                        if column and row[columns.index(column)] is not None
                        and str(row[columns.index(column)]).strip()
                    ]
                code = str(raw.get("code") or "")
                qtype = _database_question_type(raw.get("type"), options=options, code=code, correct=correct_value)
                if qtype == "multiple_choice" and len(options) < 2:
                    result.errors.append(f"Row {index}: multiple-choice question needs at least two options.")
                    continue
                if qtype == "multiple_choice":
                    correct_token = str(correct_value).strip()
                    if correct_token.isdigit() and 1 <= int(correct_token) <= len(options):
                        correct_value = options[int(correct_token) - 1]
                    elif len(correct_token) == 1 and correct_token.upper() in "ABCDE":
                        option_index = ord(correct_token.upper()) - ord("A")
                        if option_index < len(options):
                            correct_value = options[option_index]
                    if str(correct_value).strip().lower() not in {option.strip().lower() for option in options}:
                        result.errors.append(f"Row {index}: correct answer does not match an option.")
                        continue
                if qtype == "true_false":
                    token = str(correct_value).strip().lower()
                    if token in {"yes", "y", "1"}:
                        correct_value = "true"
                    elif token in {"no", "n", "0"}:
                        correct_value = "false"
                    elif token not in {"true", "false"}:
                        result.errors.append(f"Row {index}: true/false answer is invalid.")
                        continue
                question_id = _stable_question_id(raw.get("id"), index, prompt)
                base_id = question_id
                suffix = 2
                while question_id in seen_ids:
                    question_id = f"{base_id}_{suffix}"
                    suffix += 1
                seen_ids.add(question_id)
                question = {
                    "id": question_id,
                    "type": qtype,
                    "prompt": prompt,
                    "options": options,
                    "code": code,
                    "correct": "" if correct_value is None else str(correct_value),
                    "explanation": str(raw.get("explanation") or ""),
                    "concept": str(raw.get("concept") or ""),
                }
                if raw.get("difficulty") is not None:
                    try:
                        question["difficulty"] = int(raw["difficulty"])
                    except (TypeError, ValueError):
                        result.warnings.append(f"Row {index}: ignored non-numeric difficulty.")
                result.questions.append(question)
    except sqlite3.DatabaseError as exc:
        result.errors.append(f"Could not read SQLite question database: {exc}")
    except OSError as exc:
        result.errors.append(f"Could not open question database: {exc}")

    if not result.questions and not result.errors:
        result.errors.append("No usable quiz questions were found.")
    return result


def atomic_write_json(path: str | Path, data: Any) -> Path:
    """Replace a JSON file atomically, preserving the old file on failure."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(destination)
    except Exception:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def load_quiz_source(path: str | Path, *, lesson_id: str = "") -> tuple[dict[str, Any] | None, list[str]]:
    """Load and validate a teacher quiz source without raising UI errors."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"Could not read quiz: {exc}"]
    if not isinstance(data, dict):
        return None, ["Quiz root must be a JSON object."]
    if lesson_id and not data.get("lesson_id"):
        data["lesson_id"] = lesson_id
    ok, errors, _warnings = validate_quiz_dict(data)
    return (data if ok else None), errors


def preview_lesson(lesson_dir: str | Path, *, quiz_root: Path | None = None) -> dict[str, Any]:
    """Return human-friendly lesson metadata and all authoring issues."""
    path = Path(lesson_dir).expanduser().resolve()
    detected = detect_lesson(path)
    issues: list[str] = []
    warnings: list[str] = []
    manifest: DuckyManifest | None = None
    manifest_path = path / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = DuckyManifest.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            issues.append(f"Manifest: could not read manifest.json: {exc}")

    if not detected["is_valid"]:
        issues.append("Lesson needs a starter/ folder, tests/ folder, and starter Python file.")
    starter_files = sorted((path / "starter").glob("*.py")) if (path / "starter").is_dir() else []
    test_files = sorted((path / "tests").rglob("*.py")) if (path / "tests").is_dir() else []
    if len(starter_files) > 1:
        issues.append("starter/ contains multiple Python files; choose one entry file in manifest.json.")
    if not test_files:
        issues.append("tests/ has no test_*.py files.")

    if manifest is not None:
        entry_path = path / "starter" / manifest.entry_file
        if not entry_path.is_file():
            issues.append(f"Manifest: entry file '{manifest.entry_file}' is missing from starter/.")
        try:
            _validate_authoring_sources(path, manifest)
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            issues.append(f"Code: {exc}")

    quiz_sources = find_quiz_sources(path, quiz_root)
    quiz_path = quiz_sources[0] if quiz_sources else None
    if len(quiz_sources) > 1:
        warnings.append(
            "Multiple quiz sources found; the lesson file takes precedence. "
            f"Using {quiz_path.name}."
        )
    quiz_data = None
    if quiz_path:
        quiz_data, quiz_errors = load_quiz_source(quiz_path, lesson_id=path.name)
        issues.extend(f"Quiz: {error}" for error in quiz_errors)

    return {
        "path": path,
        "title": manifest.title if manifest is not None else detected.get("title", path.name),
        "lesson_id": manifest.lesson_id if manifest is not None else path.name,
        "entry_file": manifest.entry_file if manifest is not None else detected.get("entry_file", ""),
        "starter_files": len(starter_files),
        "test_files": len(test_files),
        "quiz_path": quiz_path,
        "quiz_questions": len(quiz_data.get("questions", [])) if quiz_data else 0,
        "quiz_time_limit_seconds": int(quiz_data.get("time_limit_seconds", 0) or 0) if quiz_data else 0,
        "issues": issues,
        "warnings": warnings,
        "ok": not issues,
    }


def _literal(value: str, field: str) -> str:
    """Validate a Python literal and return normalized source text."""
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid Python literal: {exc}") from exc
    return repr(parsed)


def _atomic_write_text(path: str | Path, content: str) -> Path:
    """Write generated teacher content without exposing a partial file."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(destination)
    except Exception:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def add_function_test_case(
    lesson_dir: str | Path,
    *,
    function_name: str,
    args: str,
    expected: str,
    name: str,
    description: str = "",
    hidden: bool = False,
) -> Path:
    """Append one deterministic function test to the lesson test module."""
    lesson = Path(lesson_dir).expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z_]\w*", function_name):
        raise ValueError("Function name must be a valid Python identifier")
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        raise ValueError("Test name must be a valid Python identifier")
    if not (lesson / "starter").is_dir() or not (lesson / "tests").is_dir():
        raise ValueError("Lesson must have starter/ and tests/ folders first")
    entry_files = sorted((lesson / "starter").glob("*.py"))
    if len(entry_files) != 1:
        raise ValueError("Lesson must have exactly one starter Python entry file")
    destination = lesson / "tests" / "test_cases.py"
    existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
    for test_file in sorted((lesson / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise ValueError(
                f"Cannot add a test while '{test_file.name}' is invalid: {exc}"
            ) from exc
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == f"test_{name}"
            for node in ast.walk(tree)
        ):
            raise ValueError(f"A test named '{name}' already exists")
    args_source = args.strip() or "()"
    try:
        parsed_args = ast.literal_eval(args_source)
    except (SyntaxError, TypeError, ValueError) as exc:
        raise ValueError(
            "Arguments must be a valid Python tuple or list, such as (1, 2)"
        ) from exc
    if not isinstance(parsed_args, (tuple, list)):
        raise ValueError("Arguments must be a Python tuple or list, such as (1, 2)")
    args_source = repr(tuple(parsed_args))
    expected_source = _literal(expected.strip(), "Expected value")
    import_name = entry_files[0].stem
    header = f"from {import_name} import {function_name}\n\n"
    if not existing:
        existing = header
    elif not existing.startswith(header):
        existing = header + existing
    marker = "# hidden test\n" if hidden else ""
    doc = repr(description.strip())
    block = (
        f"\n{marker}def test_{name}():\n"
        f"    {doc}\n"
        f"    assert {function_name}(*{args_source}) == {expected_source}\n"
    )
    return _atomic_write_text(destination, existing.rstrip() + block + "\n")
