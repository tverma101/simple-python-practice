"""Teacher CLI commands for Ducky."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import __version__
from ..classroom import ClassroomDB
from ..config import config_path, load_config, save_config
from ..dataset_builder import report_to_dataset_rows, write_dataset_jsonl
from ..lesson_builder import build_all_packs
from ..pack import DuckyManifest, create_duckypack, read_manifest
from ..paths import app_dir
from ..redactor import redact_text
from ..reports import ReportStore
from ..runner import TestFailure, TestRunReport
from ..server import DEFAULT_PORT, DuckyServer
from .shared import _get_active_class_id, _print_config

TEACHER_HELP = f"""Ducky Teacher v{__version__}

commands:
  serve                start teacher server
  classes              list classes
  add-class            create a class
  add-pack             add pack to server
  students             list connected students
  reports              view student submissions
  export               export data
  announce             send announcement
  scan                 scan folder and build packs
  import               import raw assignment ZIP or folder into .duckypack
  validate             validate .duckypack files before distribution
  pack                 create/inspect .duckypack files
  config               show/set config
  dataset              build and export dataset JSONL
  quiz                 author or validate lesson quizzes
  redact               redact sensitive info from text or file"""


# ── Teacher handlers ───────────────────────────────────────


def _handle_teacher_serve(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    llm_cfg = {}
    backend = config.get("llm_backend", "none")
    if backend != "none":
        llm_cfg = {
            "llm_enabled": True,
            "llm_backend": backend,
            "llm_model": config.get(f"{backend}_model", ""),
            "llm_url": config.get(f"{backend}_url", ""),
            "lmstudio_max_tokens": config.get("lmstudio_max_tokens", 2500),
        }
    server = DuckyServer(
        port=args.port,
        access_code=args.code,
        class_id=args.class_id,
        class_name=args.class_name,
        db=db,
        llm_config=llm_cfg,
        bind_host=str(getattr(args, "bind_host", "") or config.get("server_bind_host", "0.0.0.0")),
    )
    server.start()
    code = server.get_access_code()
    admin_token = server.get_admin_token()
    from ..network import get_local_ip
    server_url = server.get_url()
    local_ip = get_local_ip()
    # Persist the admin token so teacher CLI admin commands can authenticate.
    save_config({"teacher_admin_token": admin_token, "teacher_class_id": args.class_id})
    print(f"Ducky Teacher Server v{__version__}")
    print(f"  Class:     {args.class_name} ({args.class_id})")
    print(f"  Access:    {code}  (share with students)")
    print(f"  Admin:     {admin_token}  (teacher-only — keep secret)")
    print(f"  Listening: {server_url}")
    if backend != "none":
        print(f"  LLM:       {backend} / {config.get(f'{backend}_model', '')}")
    print("\nStudents connect with:")
    print(f'  ducky student connect --code {code} --name "Your Name" --url {server_url}')
    print(f"\n  Or tell students your IP: {local_ip}")
    print("\nPress Ctrl+C to stop.")
    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop()
    return 0


def _handle_teacher_classes(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        classes = db.list_classes()
        if not classes:
            print('No classes. Create one with: ducky teacher add-class --class-id <id> --name "Name"')
            return 0
        for c in classes:
            students = db.list_students(c["class_id"])
            packs = db.list_packs(c["class_id"])
            print(
                f"  {c['class_id']}: {c['name']} | code: {c['access_code']} | {len(students)} students, {len(packs)} packs"
            )
    finally:
        db.close()
    return 0


def _handle_teacher_add_class(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        code = args.code or None
        result = db.create_class(args.class_id, args.name, code)
        print(f"Created class: {result['class_id']} ({result['name']})")
        print(f"  Access code: {result['access_code']}")
    finally:
        db.close()
    return 0


def _handle_teacher_add_pack(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        class_id = _get_active_class_id(args, config)
        pack_path = Path(args.source)
        if not pack_path.exists():
            print(f"File not found: {args.source}")
            return 1

        from ..validator import validate_pack_for_distribution

        validation = validate_pack_for_distribution(pack_path)
        if not validation["ok"]:
            print(validation["human_output"])
            print("\nPack has validation errors. Fix them before distributing.")
            return 1

        pack_bytes = pack_path.read_bytes()
        instructions = ""
        try:
            manifest = read_manifest(pack_path)
            instructions = manifest.instructions
        except Exception:
            pass
        result = db.add_pack(class_id, args.lesson_id, args.title, pack_bytes, instructions=instructions)
        print(f"Added pack: {result['pack_id']} v{result['version']} ({args.title})")
    finally:
        db.close()
    return 0


def _handle_teacher_students(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        class_id = _get_active_class_id(args, config)
        students = db.list_students(class_id)
        if not students:
            print("No students connected yet.")
            return 0
        print(f"  {'Name':<20} {'Connected':<20} {'Last Seen':<20}")
        print(f"  {'─' * 20} {'─' * 20} {'─' * 20}")
        for s in students:
            print(f"  {s['name']:<20} {s['connected_at'][:19]:<20} {s['last_seen'][:19]:<20}")
    finally:
        db.close()
    return 0


def _handle_teacher_reports(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        class_id = _get_active_class_id(args, config)
        kind = getattr(args, "kind", None)
        submissions = db.list_submissions(class_id, pack_id=getattr(args, "pack", None), kind=kind)
        if not submissions:
            print("No submissions yet.")
            return 0
        for s in submissions:
            tag = s.get("kind", "submission")
            try:
                report = json.loads(s.get("report_json", "{}"))
            except Exception:
                report = {}
            if tag == "quiz":
                score = report.get("score_pct", "?")
                correct = report.get("correct", "?")
                total = report.get("total", "?")
                passed = "PASS" if report.get("passed") else "FAIL"
                print(f"  [QUIZ/{passed}] {s.get('student_name', '?')} — {report.get('quiz_id', '?')} — {correct}/{total} ({score}%) — {s['submitted_at'][:19]}")
            else:
                status = "PASS" if report.get("passed") else "FAIL"
                label = "PROG" if tag == "progress" else status
                lesson = report.get("lesson_id") or s.get("pack_id", "?")
                print(f"  [{label}] {s.get('student_name', '?')} — {lesson} — {s['submitted_at'][:19]}")
    finally:
        db.close()
    return 0


def _handle_teacher_export(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        class_id = _get_active_class_id(args, config)
        data = db.export_class_data(class_id)
        if not data:
            print("No data to export.")
            return 0
        out = Path(args.output)
        with open(out, "w", encoding="utf-8") as f:
            for row in data:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"Exported {len(data)} row(s) to: {out}")
    finally:
        db.close()
    return 0


def _handle_teacher_announce(args, config: dict[str, object]) -> int:
    db = ClassroomDB()
    try:
        class_id = _get_active_class_id(args, config)
        result = db.add_announcement(class_id, args.message)
        print(f"Announcement sent: {result['announcement_id']}")
    finally:
        db.close()
    return 0


def _handle_teacher_validate(args) -> int:
    from ..validator import validate_pack_for_distribution

    if args.pack:
        result = validate_pack_for_distribution(args.pack)
        print(result["human_output"])
        return 0 if result["ok"] else 1

    if args.all:
        from ..teacher_workspace import get_built_packs_dir

        packs_dir = get_built_packs_dir()
        packs = list(packs_dir.glob("*.duckypack")) if packs_dir.is_dir() else []
        if not packs:
            print("No packs found. Build packs first with: ducky teacher scan --source <folder>")
            return 0

        all_valid = True
        for p in packs:
            result = validate_pack_for_distribution(p)
            print(result["human_output"])
            print()
            if not result["ok"]:
                all_valid = False

        if all_valid:
            print(f"All {len(packs)} pack(s) are valid.")
        else:
            print("Some packs have errors. Fix them before distributing to students.")
        return 0 if all_valid else 1

    print("Usage: ducky teacher validate --pack <file> or --all")
    return 1


def _handle_scan(args) -> int:
    source = Path(args.source)
    if not source.is_dir():
        print(f"Not a directory: {source}")
        return 1
    output_dir = Path(args.output) if args.output else app_dir() / "packs"
    output_dir.mkdir(parents=True, exist_ok=True)
    results = build_all_packs(source, output_dir)
    if not results:
        print(f"No valid lesson folders found in {source}")
        return 1
    for result in results:
        if result["status"] == "built":
            print(f"  Built: {result['lesson_id']} -> {result['pack_path'].name}")
        else:
            print(f"  Error: {result['lesson_id']} -> {result['status']}")
    print(f"\nBuilt {len(results)} lesson pack(s) in {output_dir}")
    return 0


def _handle_teacher_dataset(args) -> int:
    store = ReportStore()
    reports_dir = store.paths["reports"]
    if args.dataset_cmd == "export":
        report_files = sorted(reports_dir.glob("*.json"))
        reports = []
        for rf in report_files:
            try:
                data = json.loads(rf.read_text(encoding="utf-8"))
                report = TestRunReport(**{k: v for k, v in data.items() if k != "failures"})
                report.failures = [TestFailure(**f) for f in data.get("failures", [])]
                reports.append(report)
            except Exception:
                continue
        if not reports:
            print("No valid reports to export.")
            return 1
        out = write_dataset_jsonl(reports, args.output)
        total = sum(len(report_to_dataset_rows(r)) for r in reports)
        print(f"Exported {total} row(s) from {len(reports)} report(s) to: {out}")
        return 0
    elif args.dataset_cmd == "export-last":
        reports = sorted(reports_dir.glob("*.json"), reverse=True)
        if not reports:
            print("No reports found.")
            return 1
        data = json.loads(reports[0].read_text(encoding="utf-8"))
        report = TestRunReport(**{k: v for k, v in data.items() if k != "failures"})
        report.failures = [TestFailure(**f) for f in data.get("failures", [])]
        out = write_dataset_jsonl([report], args.output)
        rows = report_to_dataset_rows(report)
        print(f"Exported {len(rows)} row(s) to: {out}")
        return 0
    elif args.dataset_cmd == "preview":
        path = Path(args.input)
        if not path.exists():
            print(f"File not found: {args.input}")
            return 1
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        for i, line in enumerate(lines[: args.limit], 1):
            try:
                row = json.loads(line)
                print(f"\n  Row {i}:")
                print(f"    Task:      {row.get('task')}")
                print(f"    Lesson:    {row.get('lesson_id')}")
                print(f"    Diagnosis: {row.get('test_failure', {}).get('diagnosis')}")
                hint = row.get("target", {}).get("hint", "")
                print(f"    Hint:      {hint[:100]}{'...' if len(hint) > 100 else ''}")
            except Exception:
                print(f"  Row {i}: (invalid)")
        if len(lines) > args.limit:
            print(f"\n  ... and {len(lines) - args.limit} more rows")
        return 0
    elif args.dataset_cmd == "stats":
        path = Path(args.input)
        if not path.exists():
            print(f"File not found: {args.input}")
            return 1
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        total = len(lines)
        diagnoses = {}
        for line in lines:
            try:
                row = json.loads(line)
                diag = row.get("test_failure", {}).get("diagnosis", "unknown")
                diagnoses[diag] = diagnoses.get(diag, 0) + 1
            except Exception:
                continue
        print(f"  Total rows: {total}")
        print("  Diagnosis breakdown:")
        for diag, count in sorted(diagnoses.items(), key=lambda x: -x[1]):
            print(f"    {diag}: {count}")
        return 0
    return 1


def _handle_teacher_redact(args) -> int:
    if args.input:
        text = Path(args.input).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    print(redact_text(text, strip_comments=args.strip_comments))
    return 0


def _handle_teacher_quiz(args) -> int:
    """Author or validate lesson quizzes."""
    from ..quiz import validate_quiz_dict

    if args.quiz_cmd == "validate":
        path = Path(args.file)
        if not path.is_file():
            print(f"File not found: {args.file}")
            return 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}")
            return 1
        ok, errors, warnings = validate_quiz_dict(data)
        if warnings:
            for w in warnings:
                print(f"  ! {w}")
        if not ok:
            for e in errors:
                print(f"  ✗ {e}")
            print("\nQuiz is invalid. Fix the errors above before distributing.")
            return 1
        print(f"✓ Quiz is valid ({len(data.get('questions', []))} question(s)).")
        return 0

    if args.quiz_cmd == "create":
        path = Path(args.file)
        if not path.is_file():
            print(f"Quiz file not found: {args.file}")
            return 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}")
            return 1
        ok, errors, _warnings = validate_quiz_dict(data)
        if not ok:
            for e in errors:
                print(f"  ✗ {e}")
            print("\nQuiz failed validation. Fix the errors above before saving.")
            return 1

        lesson_dir = Path(args.lesson)
        if not lesson_dir.is_dir():
            print(f"Lesson folder not found: {args.lesson}")
            return 1
        dest_dir = lesson_dir / "quizzes"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "quiz.json"
        dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"✓ Quiz saved to: {dest}")
        print("  Next build the pack (ducky teacher scan or ducky teacher pack create)")
        print("  so the quiz is bundled into the .duckypack for students.")
        return 0

    print("Usage: ducky teacher quiz create --lesson <dir> --file <quiz.json>")
    print("       ducky teacher quiz validate --file <quiz.json>")
    return 1


def _handle_teacher_config(args, config: dict[str, object]) -> int:
    if args.config_cmd == "show":
        print(f"Config path: {config_path()}")
        _print_config(load_config())
        return 0
    elif args.config_cmd == "set":
        updates = {
            "llm_backend": args.llm_backend,
            "ollama_model": args.ollama_model,
            "ollama_url": args.ollama_url,
            "lmstudio_model": args.lmstudio_model,
            "lmstudio_url": args.lmstudio_url,
            "lmstudio_max_tokens": args.lmstudio_max_tokens,
        }
        updates = {key: value for key, value in updates.items() if value is not None}
        if not updates:
            print("No config values provided.")
            return 1
        path = save_config(updates)
        print(f"Updated config: {path}")
        _print_config(load_config())
        return 0
    return 1


# ── Teacher CLI main ───────────────────────────────────────


def main_teacher(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ducky teacher",
        description=f"Ducky Teacher v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=TEACHER_HELP,
    )
    parser.add_argument("--version", action="version", version=f"ducky teacher {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve_p = sub.add_parser("serve", help="start teacher server")
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument("--code", help="9-digit access code (auto-generated if omitted)")
    serve_p.add_argument("--class-id", default="default")
    serve_p.add_argument("--class-name", default="My Class")
    serve_p.add_argument("--bind-host", help="interface to bind (defaults to configured local-network interface)")

    sub.add_parser("classes", help="list classes")

    add_class_p = sub.add_parser("add-class", help="create a new class")
    add_class_p.add_argument("--class-id", required=True)
    add_class_p.add_argument("--name", required=True)
    add_class_p.add_argument("--code", help="custom 9-digit code")

    add_pack_p = sub.add_parser("add-pack", help="add a .duckypack to the server")
    add_pack_p.add_argument("--source", required=True)
    add_pack_p.add_argument("--lesson-id", required=True)
    add_pack_p.add_argument("--title", required=True)
    add_pack_p.add_argument("--class-id")

    students_p = sub.add_parser("students", help="list connected students")
    students_p.add_argument("--class-id")

    reports_p = sub.add_parser("reports", help="view student submissions")
    reports_p.add_argument("--pack")
    reports_p.add_argument("--class-id")
    reports_p.add_argument(
        "--kind",
        choices=["submission", "progress", "quiz"],
        help="filter by submission kind (default: all)",
    )

    export_p = sub.add_parser("export", help="export class data as JSONL")
    export_p.add_argument("--output", required=True)
    export_p.add_argument("--class-id")

    announce_p = sub.add_parser("announce", help="send an announcement")
    announce_p.add_argument("message")
    announce_p.add_argument("--class-id")

    scan_p = sub.add_parser("scan", help="scan folder and build all lesson packs")
    scan_p.add_argument("--source", required=True)
    scan_p.add_argument("--output")

    validate_p = sub.add_parser("validate", help="validate .duckypack files before distribution")
    validate_p.add_argument("--pack", help="validate a single .duckypack file")
    validate_p.add_argument("--all", action="store_true", help="validate all packs in built_packs/")

    import_p = sub.add_parser("import", help="import raw assignment ZIP or folder into .duckypack")
    import_p.add_argument("--source", required=True, help="path to assignment ZIP or folder")
    import_p.add_argument("--output", help="output directory for .duckypack files")
    import_p.add_argument("--lesson-id", help="override lesson ID (default: folder name)")
    import_p.add_argument("--title", help="override title (default: humanized folder name)")

    pack_p = sub.add_parser("pack", help="create or inspect .duckypack files")
    pack_sub = pack_p.add_subparsers(dest="pack_cmd", required=True)
    pack_create = pack_sub.add_parser("create", help="create a .duckypack")
    pack_create.add_argument("--source", required=True)
    pack_create.add_argument("--output", required=True)
    pack_create.add_argument("--lesson-id", required=True)
    pack_create.add_argument("--title", required=True)
    pack_create.add_argument("--entry-file", required=True)
    pack_create.add_argument("--tags", default="")
    pack_inspect = pack_sub.add_parser("inspect", help="inspect a .duckypack")
    pack_inspect.add_argument("pack")

    config_p = sub.add_parser("config", help="show or set Ducky config")
    config_sub = config_p.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("show", help="show current config")
    config_set = config_sub.add_parser("set", help="update saved config")
    config_set.add_argument("--llm-backend")
    config_set.add_argument("--ollama-model")
    config_set.add_argument("--ollama-url")
    config_set.add_argument("--lmstudio-model")
    config_set.add_argument("--lmstudio-url")
    config_set.add_argument("--lmstudio-max-tokens", type=int)

    dataset_p = sub.add_parser("dataset", help="build and export dataset JSONL")
    dataset_sub = dataset_p.add_subparsers(dest="dataset_cmd", required=True)
    ds_export = dataset_sub.add_parser("export", help="export all reports to JSONL")
    ds_export.add_argument("--output", required=True)
    ds_export_last = dataset_sub.add_parser("export-last", help="export only the last report")
    ds_export_last.add_argument("--output", required=True)
    ds_preview = dataset_sub.add_parser("preview", help="preview dataset rows")
    ds_preview.add_argument("--input", required=True)
    ds_preview.add_argument("--limit", type=int, default=5)
    ds_stats = dataset_sub.add_parser("stats", help="show dataset statistics")
    ds_stats.add_argument("--input", required=True)

    redact_p = sub.add_parser("redact", help="redact sensitive info from text or a file")
    redact_p.add_argument("--input")
    redact_p.add_argument("--strip-comments", action="store_true")

    quiz_p = sub.add_parser("quiz", help="author or validate lesson quizzes")
    quiz_sub = quiz_p.add_subparsers(dest="quiz_cmd", required=True)
    quiz_create = quiz_sub.add_parser("create", help="validate and save a quiz into a lesson folder")
    quiz_create.add_argument("--lesson", required=True, help="path to the lesson folder")
    quiz_create.add_argument("--file", required=True, help="path to a quizzes.json file")
    quiz_validate = quiz_sub.add_parser("validate", help="validate a quizzes.json file")
    quiz_validate.add_argument("--file", required=True, help="path to a quizzes.json file")

    args = parser.parse_args(argv)
    config = load_config()

    if args.cmd == "serve":
        return _handle_teacher_serve(args, config)
    elif args.cmd == "classes":
        return _handle_teacher_classes(args, config)
    elif args.cmd == "add-class":
        return _handle_teacher_add_class(args, config)
    elif args.cmd == "add-pack":
        return _handle_teacher_add_pack(args, config)
    elif args.cmd == "students":
        return _handle_teacher_students(args, config)
    elif args.cmd == "reports":
        return _handle_teacher_reports(args, config)
    elif args.cmd == "export":
        return _handle_teacher_export(args, config)
    elif args.cmd == "announce":
        return _handle_teacher_announce(args, config)
    elif args.cmd == "scan":
        return _handle_scan(args)
    elif args.cmd == "import":
        from ..importer import import_assignment

        output = Path(args.output) if args.output else None
        result = import_assignment(
            args.source,
            output_dir=output,
            lesson_id=args.lesson_id or "",
            title=args.title or "",
        )
        if not result.get("ok"):
            print(f"Import failed: {result.get('error', 'unknown error')}")
            return 1
        if "pack_path" in result:
            print(f"Imported: {result['lesson_id']} -> {result['pack_path']}")
        elif "packs" in result:
            for p in result["packs"]:
                if p.get("ok"):
                    print(f"  Imported: {p['lesson_id']} -> {p['pack_path']}")
                else:
                    print(f"  Failed: {p.get('lesson_id', '?')} -> {p.get('error', '?')}")
            print(f"\nImported {result['count']} assignment(s).")
        for w in result.get("warnings", []):
            print(f"  Warning: {w}")
        return 0
    elif args.cmd == "pack":
        if args.pack_cmd == "create":
            source = Path(args.source)
            starter = source / "starter" if (source / "starter").is_dir() else source
            tests = source / "tests" if (source / "tests").is_dir() else source / "tests"
            manifest = DuckyManifest(
                lesson_id=args.lesson_id,
                title=args.title,
                entry_file=args.entry_file,
                concept_tags=[x.strip() for x in args.tags.split(",") if x.strip()],
            )
            result = create_duckypack(starter, tests, args.output, manifest)
            print(f"Created: {result}")
            return 0
        elif args.pack_cmd == "inspect":
            manifest = read_manifest(args.pack)
            data = manifest.to_dict()
            print(json.dumps(data, indent=2))
            return 0
    elif args.cmd == "config":
        return _handle_teacher_config(args, config)
    elif args.cmd == "dataset":
        return _handle_teacher_dataset(args)
    elif args.cmd == "redact":
        return _handle_teacher_redact(args)
    elif args.cmd == "quiz":
        return _handle_teacher_quiz(args)
    elif args.cmd == "validate":
        return _handle_teacher_validate(args)

    return 1
