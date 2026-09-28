"""Main CLI dispatcher for Ducky."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from .. import __version__
from ..config import config_path, load_config, save_config
from ..pack import DuckyManifest, create_duckypack, read_manifest
from ..reports import ReportStore, render_text_report
from ..runner import run_pack
from .shared import _handle_hint, _handle_open, _handle_setup_llm, _make_tutor_engine, _print_config, _record_progress
from .student_commands import (
    _handle_instructions,
    _handle_quick_test,
    _handle_quiz,
    main_student,
)
from .teacher_commands import (
    _handle_scan,
    _handle_teacher_dataset,
    _handle_teacher_redact,
    main_teacher,
)

# ═══════════════════════════════════════════════════════════════════════
# Unified check handler (supports both "check" and old "student --pack")
# ═══════════════════════════════════════════════════════════════════════


def _handle_check(args, config: dict[str, object]) -> int:
    pack_path = args.pack if hasattr(args, "pack") and args.pack else getattr(args, "student_pack", None)
    submission_path = (
        args.submission
        if hasattr(args, "submission") and args.submission
        else getattr(args, "student_submission", None)
    )
    if not pack_path or not submission_path:
        print("--pack and --submission are required.")
        return 1
    save_report = getattr(args, "save_report", False)
    do_submit = getattr(args, "submit", False)
    do_fetch = getattr(args, "fetch", False)
    fetch_output = getattr(args, "fetch_output", None)
    student_name = getattr(args, "student_name", "")
    teacher_submit_url = getattr(args, "teacher_submit_url", None)
    teacher_fetch_url = getattr(args, "teacher_fetch_url", None)

    engine = _make_tutor_engine(config)
    report = run_pack(pack_path, student_file=submission_path, tutor_engine=engine, timeout_seconds=15)
    store = ReportStore()
    store.save(report)

    _record_progress(report)

    if save_report:
        print(f"Report saved: {store.paths['reports']}")

    print(render_text_report(report))

    if report.passed:
        print("\nAll tests passed! Great work.")
    else:
        print("\nTests failed. Edit your code and try again.")

    if do_submit and teacher_submit_url:
        from ..teacher_backend import TeacherBackendClient

        client = TeacherBackendClient(
            submit_url=teacher_submit_url,
            fetch_url=teacher_fetch_url or "",
        )
        raw_code = Path(submission_path).read_text(encoding="utf-8") if Path(submission_path).exists() else ""
        report.raw_code = raw_code
        try:
            result = client.submit_report(report, student_name=student_name, include_raw=True)
            print(f"Submitted: {result}")
        except Exception as exc:
            print(f"Submit failed: {exc}")

    if do_fetch and teacher_fetch_url and fetch_output:
        from ..teacher_backend import TeacherBackendClient

        client = TeacherBackendClient(
            submit_url=teacher_submit_url or "",
            fetch_url=teacher_fetch_url,
        )
        try:
            result = client.fetch_file(output_path=fetch_output)
            print(f"Fetched: {result}")
        except Exception as exc:
            print(f"Fetch failed: {exc}")

    return 0 if report.passed else 1


# ═══════════════════════════════════════════════════════════════════════
# Main dispatcher
# ═══════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # --- Auto-update on every launch ---
    try:
        from .. import __build_type__
        from ..self_update import apply_pending_update, auto_update_background

        # 1. Apply any pending update from a previous launch
        applied = apply_pending_update()
        if applied and applied.get("ok"):
            print(f"  ✓ Ducky updated to v{applied.get('version', '?')}.")

        # 2. Spawn background check for next update (teacher-only;
        #    student build handles updates at TUI entry).
        if __build_type__ == "full":
            threading.Thread(target=auto_update_background, daemon=True).start()
    except Exception:
        pass  # Never block CLI startup on update failure

    if not argv or argv[0] in {"gui", "app", "tui", "shell"}:
        from ..gui import main as tui_main

        tui_main()
        return 0

    if argv[0] == "student":
        student_new_commands = {
            "lessons",
            "start",
            "test",
            "connect",
            "update",
            "submit",
            "status",
            "progress",
            "sync-progress",
            "instructions",
            "self-update",
            "report",
            "config",
            "quiz",
            "--help",
            "-h",
        }
        if len(argv) <= 1 or argv[1] in student_new_commands:
            return main_student(argv[1:] if len(argv) > 1 else [])
        # Fall through to unified parser for old-style "student --pack" flags
    elif argv[0] == "teacher":
        return main_teacher(argv[1:])

    # Unified parser: supports both old-style and new-style commands
    parser = argparse.ArgumentParser(
        prog="ducky",
        description=f"Ducky v{__version__} — local-first coding tutor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""version: {__version__}

usage:
  ducky                         open interactive TUI
  ducky test                    run tests on current lesson
  ducky instructions            view current lesson instructions
  ducky student <command>       student commands (new)
  ducky teacher <command>       teacher commands (new)

legacy (still supported):
  ducky check                   quick grading
  ducky student --pack ...      submit/fetch (old)
  ducky config set              update config
""",
    )
    parser.add_argument("--version", action="version", version=f"ducky {__version__}")

    sub = parser.add_subparsers(dest="cmd")

    # The practice server. Registered first so `ducky web --help` is discoverable;
    # it used to have no subcommand at all, so the site had no way to start.
    #
    # Guarded: the student build strips this module along with classroom.py, and
    # an unguarded import here took down the entire CLI in a student install, so
    # `ducky --version` and the TUI failed on a server command they never wanted.
    run_practice = None
    try:
        from .practice import add_parser as add_practice_parsers
        from .practice import run as run_practice

        add_practice_parsers(sub)
    except ImportError:  # student build: no practice server
        pass

    # check (student quick grading)
    check_p = sub.add_parser("check", help="quick grading")
    check_p.add_argument("--pack", required=True)
    check_p.add_argument("--submission", required=True)
    check_p.add_argument("--save-report", action="store_true")
    check_p.add_argument("--submit", action="store_true")
    check_p.add_argument("--fetch", action="store_true")
    check_p.add_argument("--fetch-output")
    check_p.add_argument("--student-name", default="")
    check_p.add_argument("--teacher-submit-url")
    check_p.add_argument("--teacher-fetch-url")
    check_p.add_argument("--teacher-code")
    check_p.add_argument("--teacher-upload-all", action=argparse.BooleanOptionalAction, default=None)

    # student (old-style with --pack flags)
    student_old = sub.add_parser("student", help="student commands")
    student_old.add_argument("--pack", dest="student_pack")
    student_old.add_argument("--submission", dest="student_submission")
    student_old.add_argument("--save-report", action="store_true")
    student_old.add_argument("--submit", action="store_true")
    student_old.add_argument("--fetch", action="store_true")
    student_old.add_argument("--fetch-output")
    student_old.add_argument("--student-name", default="")
    student_old.add_argument("--teacher-submit-url")
    student_old.add_argument("--teacher-fetch-url")
    student_old.add_argument("--teacher-code")
    student_old.add_argument("--teacher-upload-all", action=argparse.BooleanOptionalAction, default=None)

    # pack
    pack_p = sub.add_parser("pack")
    pack_sub = pack_p.add_subparsers(dest="pack_cmd")
    pack_create = pack_sub.add_parser("create")
    pack_create.add_argument("--source", required=True)
    pack_create.add_argument("--output", required=True)
    pack_create.add_argument("--lesson-id", required=True)
    pack_create.add_argument("--title", required=True)
    pack_create.add_argument("--entry-file", required=True)
    pack_create.add_argument("--tags", default="")
    pack_inspect = pack_sub.add_parser("inspect")
    pack_inspect.add_argument("pack")

    # report
    report_p = sub.add_parser("report")
    report_sub = report_p.add_subparsers(dest="report_cmd")
    report_list = report_sub.add_parser("list")
    report_list.add_argument("--limit", type=int, default=10)
    report_view = report_sub.add_parser("view")
    report_view.add_argument("report_id", nargs="?")
    report_sub.add_parser("latest")

    # dataset
    dataset_p = sub.add_parser("dataset")
    dataset_sub = dataset_p.add_subparsers(dest="dataset_cmd")
    ds_export = dataset_sub.add_parser("export")
    ds_export.add_argument("--output", required=True)
    ds_export_last = dataset_sub.add_parser("export-last")
    ds_export_last.add_argument("--output", required=True)
    ds_preview = dataset_sub.add_parser("preview")
    ds_preview.add_argument("--input", required=True)
    ds_preview.add_argument("--limit", type=int, default=5)
    ds_stats = dataset_sub.add_parser("stats")
    ds_stats.add_argument("--input", required=True)

    # redact
    redact_p = sub.add_parser("redact")
    redact_p.add_argument("--input")
    redact_p.add_argument("--strip-comments", action="store_true")

    # config
    config_p = sub.add_parser("config")
    config_sub = config_p.add_subparsers(dest="config_cmd")
    config_sub.add_parser("show")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("--llm-backend")
    config_set.add_argument("--ollama-model")
    config_set.add_argument("--ollama-url")
    config_set.add_argument("--lmstudio-model")
    config_set.add_argument("--lmstudio-url")
    config_set.add_argument("--lmstudio-max-tokens", type=int)
    config_set.add_argument("--teacher-url")
    config_set.add_argument("--teacher-token")
    config_set.add_argument("--student-name")
    config_set.add_argument("--teacher-submit-url")
    config_set.add_argument("--teacher-fetch-url")
    config_set.add_argument("--teacher-code")
    config_set.add_argument("--teacher-upload-all", action=argparse.BooleanOptionalAction, default=None)

    # scan
    scan_p = sub.add_parser("scan")
    scan_p.add_argument("--source", required=True)
    scan_p.add_argument("--output")

    # progress
    progress_p = sub.add_parser("progress")
    progress_p.add_argument("--lesson")

    # test (convenience alias for "student test")
    test_p = sub.add_parser("test", help="run tests on current lesson")
    test_p.add_argument("--lesson", help="lesson ID (auto-detects current if omitted)")

    # instructions (convenience alias for viewing current lesson instructions)
    sub.add_parser("instructions", help="view current lesson instructions")

    # init (initialize a new workspace)
    init_p = sub.add_parser("init", help="initialize a Ducky workspace")
    init_p.add_argument("directory", nargs="?", default=".", help="directory to initialize (default: current)")

    # import (import assignment into workspace)
    import_p = sub.add_parser("import", help="import assignment into workspace")
    import_p.add_argument("source", help="path to .duckypack, .zip, or assignment folder")
    import_p.add_argument("--dest", help="output directory for .duckypack (default: teacher built_packs)")
    import_p.add_argument("--no-starter", action="store_true",
                          help="create empty stub instead of copying solution into starter/")

    # quiz (teacher-assigned when configured, otherwise platform practice)
    quiz_p = sub.add_parser("quiz", help="take a teacher-assigned or platform practice quiz")
    quiz_p.add_argument("--lesson", help="lesson ID (auto-detects current if omitted)")
    quiz_p.add_argument("--status", action="store_true", help="show stored quiz stats and exit")

    # hint (show hint for last failure)
    sub.add_parser("hint", help="show hint for the most recent test failure")

    # open (open current lesson in editor)
    sub.add_parser("open", help="open current lesson in editor")

    # setup-llm (interactive LLM setup)
    sub.add_parser("setup-llm", help="interactive LLM setup wizard")

    args = parser.parse_args(argv)
    config = load_config()

    if not args.cmd:
        return 1

    if args.cmd in ("web", "access-codes"):
        if run_practice is None:
            print(
                "This build has no practice server. It ships in the teacher build.",
                file=sys.stderr,
            )
            return 2
        return run_practice(args)

    # check
    if args.cmd == "check":
        return _handle_check(args, config)

    # student (old-style with --pack flags)
    if args.cmd == "student" and args.student_pack:
        return _handle_check(args, config)

    # pack
    if args.cmd == "pack":
        if args.pack_cmd == "create":
            from ..lesson_builder import find_quiz_source

            source = Path(args.source)
            starter = source / "starter" if (source / "starter").is_dir() else source
            tests = source / "tests" if (source / "tests").is_dir() else source / "tests"
            manifest = DuckyManifest(
                lesson_id=args.lesson_id,
                title=args.title,
                entry_file=args.entry_file,
                concept_tags=[x.strip() for x in args.tags.split(",") if x.strip()],
            )
            result = create_duckypack(
                starter,
                tests,
                args.output,
                manifest,
                source_dir=source,
                quizzes_path=find_quiz_source(source),
            )
            print(f"Created: {result}")
            return 0
        elif args.pack_cmd == "inspect":
            manifest = read_manifest(args.pack)
            data = manifest.to_dict()
            print(json.dumps(data, indent=2))
            return 0
        return 1

    # report
    if args.cmd == "report":
        store = ReportStore()
        reports_dir = store.paths["reports"]
        if args.report_cmd == "list":
            reports = sorted(reports_dir.glob("*.json"), reverse=True)[: args.limit]
            if not reports:
                print("No reports found.")
                return 0
            for r in reports:
                try:
                    data = json.loads(r.read_text(encoding="utf-8"))
                    status = "PASS" if data.get("passed") else "FAIL"
                    print(f"  [{status}] {data.get('lesson_id', '?')} — {r.stem}")
                except Exception:
                    print(f"  [????] {r.name}")
            return 0
        elif args.report_cmd == "view":
            if args.report_id:
                path = reports_dir / f"{args.report_id}.json"
                if not path.exists():
                    print(f"Report not found: {args.report_id}")
                    return 1
            else:
                reports = sorted(reports_dir.glob("*.json"), reverse=True)
                if not reports:
                    print("No reports found.")
                    return 0
                path = reports[0]
            data = json.loads(path.read_text(encoding="utf-8"))
            print(json.dumps(data, indent=2))
            return 0
        elif args.report_cmd == "latest":
            reports = sorted(reports_dir.glob("*.json"), reverse=True)
            if not reports:
                print("No reports found.")
                return 0
            data = json.loads(reports[0].read_text(encoding="utf-8"))
            print(json.dumps(data, indent=2))
            return 0

    # dataset
    if args.cmd == "dataset":
        return _handle_teacher_dataset(args)

    # redact
    if args.cmd == "redact":
        return _handle_teacher_redact(args)

    # config
    if args.cmd == "config":
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
                "teacher_url": args.teacher_url,
                "teacher_token": args.teacher_token,
                "student_name": args.student_name,
                "teacher_submit_url": args.teacher_submit_url,
                "teacher_fetch_url": args.teacher_fetch_url,
                "teacher_access_code": args.teacher_code,
                "teacher_upload_all": args.teacher_upload_all,
            }
            if args.teacher_code:
                updates["teacher_token"] = args.teacher_code
            updates = {k: v for k, v in updates.items() if v is not None}
            if not updates:
                print("No config values provided.")
                return 1
            path = save_config(updates)
            print(f"Updated config: {path}")
            _print_config(load_config())
            return 0

    # scan
    if args.cmd == "scan":
        return _handle_scan(args)

    # progress
    if args.cmd == "progress":
        from ..progress import ProgressTracker

        tracker = ProgressTracker()
        if args.lesson:
            stats = tracker.get_lesson_stats(args.lesson)
            print(f"  Lesson: {args.lesson}")
            print(f"    Total:  {stats['total']}")
            print(f"    Passed: {stats['passed']}")
            print(f"    Failed: {stats['failed']}")
        else:
            print("  No lesson specified. Use --lesson <id>.")
        tracker.close()
        return 0

    # test (convenience alias)
    if args.cmd == "test":
        return _handle_quick_test(args)

    # instructions (convenience alias)
    if args.cmd == "instructions":
        return _handle_instructions(args)

    # init
    if args.cmd == "init":
        from ..init_workspace import init_workspace

        target = Path(args.directory).resolve()
        result = init_workspace(target)
        print(result["message"])
        return 0 if result["ok"] else 1

    # import
    if args.cmd == "import":
        import shutil

        from ..importer import import_assignment

        source = Path(args.source)
        dest = Path(args.dest) if args.dest else None

        # If it's already a .duckypack, just copy it
        if source.is_file() and source.suffix == ".duckypack":
            if dest is None:
                from ..teacher_workspace import get_built_packs_dir

                dest = get_built_packs_dir()
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / source.name
            shutil.copy2(source, target)
            print(f"Copied pack: {target}")
            return 0

        result = import_assignment(args.source, dest, stub=getattr(args, 'no_starter', False))
        if not result.get("ok"):
            print(f"Error: {result.get('error', 'Import failed')}")
            return 1
        if "packs" in result:
            print(f"Imported {result['count']} pack(s):")
            for p in result["packs"]:
                print(f"  {p.get('lesson_id', '?')} → {p.get('pack_path', '?')}")
        else:
            print(f"Imported: {result.get('pack_path', '?')}")
        for w in result.get("warnings", []):
            print(f"  Warning: {w}")
        return 0

    # hint
    if args.cmd == "hint":
        return _handle_hint()

    # quiz
    if args.cmd == "quiz":
        return _handle_quiz(args)

    # open
    if args.cmd == "open":
        return _handle_open()

    # setup-llm
    if args.cmd == "setup-llm":
        return _handle_setup_llm(config)

    return 1
