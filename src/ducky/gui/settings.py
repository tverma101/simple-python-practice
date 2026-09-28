"""Settings, tutor, and reports menus for the Ducky terminal UI."""

from __future__ import annotations

import json
import logging

from .. import __build_type__
from .widgets import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    clear,
    confirm,
    error,
    heading,
    info,
    prompt,
    record_progress,
    student_confirm,
    student_error,
    student_heading,
    student_info,
    student_prompt,
    student_success,
    student_warning,
    success,
    warning,
)

logger = logging.getLogger(__name__)

# The same settings workflows are useful in both distributions, but the
# student build should feel like the student app all the way through nested
# screens.  The full build keeps the established teacher-platform styling.
if __build_type__ != "full":
    heading = student_heading
    prompt = student_prompt
    confirm = student_confirm
    success = student_success
    error = student_error
    info = student_info
    warning = student_warning


class SettingsMenuMixin:
    """Mixin providing settings, tutor, and reports menus."""

    # ─── Settings ───────────────────────────────────────────

    def _settings_menu(self):
        while True:
            clear()
            heading("Settings")
            config = self._load_config()
            backend = config.get("llm_backend", "none")
            if backend != "none":
                model = config.get(f"{backend}_model", "")
                llm_status = f"{GREEN}ON{RESET} ({backend} / {model})"
            else:
                llm_status = f"{DIM}OFF{RESET}"
            teacher_url = config.get("teacher_url", "")
            teacher_status = f"{GREEN}{teacher_url}{RESET}" if teacher_url else f"{DIM}not connected{RESET}"

            print(f"  {BOLD}LLM:{RESET}              {llm_status}")
            print(f"  {BOLD}Teacher connection:{RESET} {teacher_status}")
            print(f"  {BOLD}Student name:{RESET}      {config.get('student_name', '') or DIM + 'not set' + RESET}")
            print(f"  {BOLD}App dir:{RESET}           {self.paths['base']}")
            print()
            print(f"  {CYAN}[1]{RESET}  Configure LLM backend")
            print(f"  {CYAN}[2]{RESET}  Show config file location")
            print(f"  {CYAN}[3]{RESET}  Disconnect from teacher")
            print(f"  {CYAN}[4]{RESET}  Check for app updates")
            print(f"  {CYAN}[0]{RESET}  Back")
            print()
            choice = prompt("Choose", "0")
            if choice == "1":
                self._settings_llm()
            elif choice == "2":
                config_path = self.paths["base"] / "config.json"
                print(f"\n  Config file: {config_path}")
                input("\n  Press Enter...")
            elif choice == "3":
                self._save_config({"teacher_url": "", "teacher_token": "", "student_name": ""})
                success("Disconnected from teacher server.")
                input("  Press Enter...")
            elif choice == "4":
                self._check_app_update()
            elif choice == "0":
                break

    def _check_app_update(self):
        from ..self_update import check_for_update, download_and_install

        info("Checking for app updates...")
        result = check_for_update()

        if result["error"]:
            error(result["error"])
            input("\n  Press Enter...")
            return

        if not result["update_available"]:
            success(f"You're up to date (v{result['current_version']})")
            input("\n  Press Enter...")
            return

        print(f"\n  {BOLD}Update available:{RESET} v{result['latest_version']}")
        print(f"  {BOLD}Current version:{RESET}  v{result['current_version']}")
        if result["release_notes"]:
            print(f"  {BOLD}Notes:{RESET} {result['release_notes']}")
        print()

        if confirm("Install update now?"):
            info("Downloading and installing...")
            install_result = download_and_install(
                result["download_url"],
                expected_sha256=result.get("sha256", ""),
            )
            if install_result["ok"]:
                success(install_result["message"])
            else:
                error(install_result["error"])
        input("\n  Press Enter...")

    def _settings_llm(self):
        clear()
        heading("Configure LLM Backend")
        config = self._load_config()
        backend = config.get("llm_backend", "none")
        print(f"  Current: {BOLD}{backend}{RESET}")
        if backend != "none":
            if backend in ("gemini", "openai"):
                print(f"  API Key: {config.get(f'{backend}_api_key', '')[:8]}...")
                print(f"  Model:   {config.get(f'{backend}_model', '')}")
            else:
                print(f"  URL:     {config.get(f'{backend}_url', '')}")
                print(f"  Model:   {config.get(f'{backend}_model', '')}")
                if backend == "lmstudio":
                    print(f"  Tokens:  {config.get('lmstudio_max_tokens', 2500)} (min 2500)")
        print()
        print(f"  {CYAN}[1]{RESET}  None (disabled)")
        print(f"  {CYAN}[2]{RESET}  Gemini (free tier — get a key at aistudio.google.com)")
        print(f"  {CYAN}[3]{RESET}  OpenAI (get a key at platform.openai.com)")
        print(f"  {CYAN}[4]{RESET}  Ollama (set URL, then pick model)")
        print(f"  {CYAN}[5]{RESET}  LM Studio (set URL, then pick model)")
        if backend == "ollama":
            print(f"  {CYAN}[6]{RESET}  Browse Ollama models")
        elif backend == "lmstudio":
            print(f"  {CYAN}[6]{RESET}  Browse LM Studio models")
        print(f"  {CYAN}[0]{RESET}  Back")
        print()
        choice = prompt("Choose", "0")
        if choice == "1":
            self._save_config({"llm_backend": "none"})
            success("LLM disabled.")
            input("  Press Enter...")
        elif choice == "2":
            self._configure_gemini(config)
        elif choice == "3":
            self._configure_openai(config)
        elif choice == "4":
            url = prompt("Ollama URL", config.get("ollama_url", "http://localhost:11434"))
            if url:
                self._save_config({"llm_backend": "ollama", "ollama_url": url})
                success(f"Ollama URL set to: {url}")
                input("  Press Enter...")
                from ..config import load_config

                self._browse_ollama_models(load_config())
            else:
                input("  Press Enter...")
        elif choice == "5":
            url = prompt("LM Studio URL", config.get("lmstudio_url", "http://localhost:1234"))
            if url:
                self._save_config({"llm_backend": "lmstudio", "lmstudio_url": url})
                success(f"LM Studio URL set to: {url}")
                input("  Press Enter...")
                from ..config import load_config

                self._browse_lmstudio_models(load_config())
            else:
                input("  Press Enter...")
        elif choice == "6" and backend == "ollama":
            self._browse_ollama_models(config)
        elif choice == "6" and backend == "lmstudio":
            self._browse_lmstudio_models(config)

    def _configure_gemini(self, config):
        """Configure Gemini API backend."""
        api_key = prompt("Gemini API key", config.get("gemini_api_key", ""))
        if not api_key:
            input("  Press Enter...")
            return

        model = prompt("Model", config.get("gemini_model", "gemini-2.5-flash"))
        self._save_config({
            "llm_backend": "gemini",
            "gemini_api_key": api_key,
            "gemini_model": model or "gemini-2.5-flash",
        })

        print(f"\n  {DIM}Testing Gemini...{RESET}")
        try:
            from ..llm_client import GeminiClient, HintRequest

            client = GeminiClient(api_key=api_key, model=model or "gemini-2.5-flash")
            test_req = HintRequest(
                lesson_id="test", title="Test",
                code_snippet="def add(a, b):\n    return a + b\n",
                diagnosis="wrong_value", expected=5, actual=3,
            )
            test = client.generate_hint(test_req)
            if test:
                success(f"Gemini is working! Hint: {test[:60]}...")
            else:
                error("Gemini returned no hint. Check your API key.")
        except Exception as exc:
            error(f"Could not connect to Gemini: {exc}")
        input("  Press Enter...")

    def _configure_openai(self, config):
        """Configure OpenAI API backend."""
        api_key = prompt("OpenAI API key", config.get("openai_api_key", ""))
        if not api_key:
            input("  Press Enter...")
            return

        base_url = prompt("API base URL", config.get("openai_base_url", "https://api.openai.com/v1"))
        model = prompt("Model", config.get("openai_model", "gpt-4o-mini"))
        self._save_config({
            "llm_backend": "openai",
            "openai_api_key": api_key,
            "openai_base_url": base_url or "https://api.openai.com/v1",
            "openai_model": model or "gpt-4o-mini",
        })

        print(f"\n  {DIM}Testing OpenAI...{RESET}")
        try:
            from ..llm_client import HintRequest, OpenAIClient

            client = OpenAIClient(
                api_key=api_key,
                model=model or "gpt-4o-mini",
                base_url=base_url or "https://api.openai.com/v1",
            )
            test_req = HintRequest(
                lesson_id="test", title="Test",
                code_snippet="def add(a, b):\n    return a + b\n",
                diagnosis="wrong_value", expected=5, actual=3,
            )
            test = client.generate_hint(test_req)
            if test:
                success(f"OpenAI is working! Hint: {test[:60]}...")
            else:
                error("OpenAI returned no hint. Check your API key.")
        except Exception as exc:
            error(f"Could not connect to OpenAI: {exc}")
        input("  Press Enter...")

    # ─── Tutor ──────────────────────────────────────────────

    def _tutor_menu(self):

        while True:
            clear()
            heading("Tutor — Diagnosis & Hints")
            if not self.last_report:
                info("Run tests in the Workspace tab first to see diagnosis here.")
            else:
                report = self.last_report
                status_color = GREEN if report.passed else RED
                print(f"  {BOLD}Last Run:{RESET} {status_color}{'PASS' if report.passed else 'FAIL'}{RESET}")
                print(f"  {BOLD}Lesson:{RESET} {report.lesson_id}")
                print(f"  {BOLD}Tests:{RESET} {report.tests_passed} passed / {report.tests_failed} failed")
                print()
                if report.failures:
                    for i, f in enumerate(report.failures, 1):
                        print(f"  {RED}Failure {i}:{RESET} {f.name}")
                        print(f"    Diagnosis: {YELLOW}{f.diagnosis}{RESET}")
                        print(f"    Expected:  {f.expected!r}")
                        print(f"    Actual:    {f.actual!r}")
                        if f.stdout:
                            print(f"    Stdout:    {DIM}{f.stdout.strip()}{RESET}")
                        print(f"    {GREEN}Hint:{RESET} {f.hint}")
                        print()
                else:
                    success("All tests passed! No failures to diagnose.")

            print(f"  {CYAN}[1]{RESET}  Re-run with Ollama for extra hints")
            print(f"  {CYAN}[0]{RESET}  Back")
            print()
            choice = prompt("Choose", "0")
            if choice == "1":
                if not self.current_pack:
                    error("No pack loaded.")
                    input("  Press Enter...")
                    continue
                model = prompt("Ollama model", self.ollama_model)
                self.ollama_model = model
                from ..llm_client import OllamaClient
                from ..reports import render_text_report
                from ..runner import run_pack
                from ..tutor import TutorEngine

                tutor_engine = TutorEngine(OllamaClient(model=model))
                info("Re-running with Ollama...")
                try:
                    report = run_pack(
                        self.current_pack,
                        student_file=self.current_submission,
                        tutor_engine=tutor_engine,
                    )
                    self.last_report = report
                    self.report_store.save(report)
                    record_progress(report)
                    print()
                    print(render_text_report(report))
                except Exception as exc:
                    logger.exception("Test run with Ollama failed")
                    error(f"Test run failed: {exc}")
                input("\n  Press Enter...")
            elif choice == "0":
                break

    # ─── Reports ────────────────────────────────────────────

    def _reports_menu(self):
        while True:
            clear()
            heading("Reports & Dataset Export")
            reports_dir = self.paths["reports"]
            report_count = len(list(reports_dir.glob("*.json")))
            print(f"  {BOLD}Saved reports:{RESET} {report_count}")
            print(f"  {BOLD}Reports dir:{RESET}  {reports_dir}")
            print(f"  {BOLD}Dataset dir:{RESET}  {self.paths['dataset']}")
            print()
            print(f"  {CYAN}[1]{RESET}  List recent reports")
            print(f"  {CYAN}[2]{RESET}  View a report")
            print(f"  {CYAN}[3]{RESET}  Export last run to dataset JSONL")
            print(f"  {CYAN}[4]{RESET}  Export all reports to dataset JSONL")
            print(f"  {CYAN}[5]{RESET}  Preview dataset rows")
            print(f"  {CYAN}[0]{RESET}  Back")
            print()
            choice = prompt("Choose", "0")
            if choice == "1":
                self._reports_list()
            elif choice == "2":
                self._reports_view()
            elif choice == "3":
                self._reports_export_last()
            elif choice == "4":
                self._reports_export_all()
            elif choice == "5":
                self._reports_preview()
            elif choice == "0":
                break

    def _reports_list(self):
        heading("Recent Reports")
        reports_dir = self.paths["reports"]
        reports = sorted(reports_dir.glob("*.json"), reverse=True)[:20]
        if not reports:
            info("No reports found.")
        else:
            for i, r in enumerate(reports, 1):
                try:
                    data = json.loads(r.read_text(encoding="utf-8"))
                    status = GREEN + "PASS" + RESET if data.get("passed") else RED + "FAIL" + RESET
                    print(f"  {CYAN}[{i}]{RESET}  {status}  {data.get('lesson_id', '?')}  {r.stem}")
                except Exception:
                    print(f"  {CYAN}[{i}]{RESET}  {r.name}  {DIM}(unreadable){RESET}")
        input("\n  Press Enter...")

    def _reports_view(self):
        heading("View Report")
        reports_dir = self.paths["reports"]
        reports = sorted(reports_dir.glob("*.json"), reverse=True)[:20]
        if not reports:
            info("No reports found.")
            input("  Press Enter...")
            return
        for i, r in enumerate(reports, 1):
            print(f"  {CYAN}[{i}]{RESET}  {r.stem}")
        print(f"  {CYAN}[0]{RESET}  Back")
        choice = prompt("Report number", "0")
        if choice == "0":
            input("\n  Press Enter...")
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(reports):
                data = json.loads(reports[idx].read_text(encoding="utf-8"))
                print()
                print(json.dumps(data, indent=2))
            else:
                error("Invalid selection.")
        except (ValueError, IndexError):
            error("Invalid input.")
        input("\n  Press Enter...")

    def _reports_export_last(self):
        from ..dataset_builder import report_to_dataset_rows, write_dataset_jsonl

        heading("Export Last Run")
        if not self.last_report:
            info("No last run. Run tests first.")
            input("  Press Enter...")
            return
        out = self.paths["dataset"] / "last_run_export.jsonl"
        try:
            write_dataset_jsonl([self.last_report], out)
            rows = report_to_dataset_rows(self.last_report)
            success(f"Exported {len(rows)} row(s) to: {out}")
        except Exception as exc:
            logger.exception("Failed to write dataset file")
            error(f"Failed to write dataset file: {exc}")
        input("  Press Enter...")

    def _reports_export_all(self):
        from ..dataset_builder import report_to_dataset_rows, write_dataset_jsonl
        from ..runner import TestFailure, TestRunReport

        heading("Export All Reports")
        reports_dir = self.paths["reports"]
        report_files = sorted(reports_dir.glob("*.json"))
        if not report_files:
            info("No reports to export.")
            input("  Press Enter...")
            return

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
            info("No valid reports found.")
            input("  Press Enter...")
            return

        out = self.paths["dataset"] / "all_reports_export.jsonl"
        try:
            write_dataset_jsonl(reports, out)
            total_rows = sum(len(report_to_dataset_rows(r)) for r in reports)
            success(f"Exported {total_rows} row(s) from {len(reports)} report(s) to: {out}")
        except Exception as exc:
            logger.exception("Failed to write dataset file")
            error(f"Failed to write dataset file: {exc}")
        input("  Press Enter...")

    def _reports_preview(self):
        heading("Dataset Preview")
        out = self.paths["dataset"] / "last_run_export.jsonl"
        if not out.exists():
            info("No dataset file found. Export first.")
            input("  Press Enter...")
            return
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        for i, line in enumerate(lines[:10], 1):
            try:
                row = json.loads(line)
                print(f"  {CYAN}Row {i}:{RESET}")
                print(f"    Task: {row.get('task')}")
                print(f"    Lesson: {row.get('lesson_id')}")
                print(f"    Diagnosis: {row.get('test_failure', {}).get('diagnosis')}")
                print(f"    Hint: {row.get('target', {}).get('hint', '')[:80]}...")
                print()
            except Exception:
                print(f"  {DIM}(invalid row){RESET}")
        if len(lines) > 10:
            print(f"  {DIM}... and {len(lines) - 10} more rows{RESET}")
        input("\n  Press Enter...")

    # ─── Model browsing (student-facing) ───────────────────────
    # These live here (not in gui/teacher.py) because the Settings menu is
    # part of the student build, and gui/teacher.py is stripped from it.

    def _browse_ollama_models(self, config: dict):
        from ..llm_client import OllamaClient

        oll_url = config.get("ollama_url", "http://localhost:11434")
        client = OllamaClient(base_url=oll_url)

        print(f"\n  Fetching models from {oll_url}...")
        models = client.list_models()

        if not models:
            error("No models found. Is Ollama running?")
            input("  Press Enter...")
            return

        current_model = config.get("ollama_model", "")

        print(f"\n  {BOLD}Available Ollama Models:{RESET}\n")
        for i, m in enumerate(models, 1):
            current = f" {GREEN}<-- current{RESET}" if m["key"] == current_model else ""
            size = f"  {m['size_gb']}" if m.get("size_gb") else ""
            print(f"  {CYAN}[{i}]{RESET}  {m['name']}{size}{current}")

        print(f"\n  {CYAN}[0]{RESET}  Back")
        print()
        choice = prompt("Choose model number", "0")

        if choice == "0":
            return

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                selected = models[idx]
                self._save_config({"llm_backend": "ollama", "ollama_model": selected["key"]})
                success(f"Selected: {selected['name']}")
            else:
                error("Invalid selection")
        except ValueError:
            error("Invalid input")
        input("  Press Enter...")

    def _browse_lmstudio_models(self, config: dict):
        from ..llm_client import LMStudioClient

        lm_url = config.get("lmstudio_url", "http://localhost:1234")
        client = LMStudioClient(base_url=lm_url)

        print(f"\n  Fetching models from {lm_url}...")
        models = client.list_models()

        if not models:
            error("No models found. Is LM Studio running with the server enabled?")
            input("  Press Enter...")
            return

        current_model = config.get("lmstudio_model", "")

        print(f"\n  {BOLD}Available LM Studio Models:{RESET}\n")
        for i, m in enumerate(models, 1):
            status = f"{GREEN}[LOADED]{RESET}" if m["loaded"] else f"{DIM}[available]{RESET}"
            current = f" {GREEN}<-- current{RESET}" if m["key"] == current_model else ""
            print(f"  {CYAN}[{i}]{RESET}  {m['name']}")
            print(f"       {m['key']}  {m['size_gb']}  {status}{current}")

        print(f"\n  {CYAN}[l]{RESET}  Load a model (sets context length)")
        print(f"  {CYAN}[0]{RESET}  Back")
        print()
        choice = prompt("Choose model number", "0")

        if choice == "0":
            return

        if choice.lower() == "l":
            print("\n  Available models to load:")
            for i, m in enumerate(models, 1):
                loaded_tag = f" {GREEN}[LOADED]{RESET}" if m["loaded"] else ""
                print(f"  {CYAN}[{i}]{RESET}  {m['name']} ({m['key']}){loaded_tag}")
            print()
            load_choice = prompt("Choose model to load", "1")
            try:
                idx = int(load_choice) - 1
                if 0 <= idx < len(models):
                    model_key = models[idx]["key"]
                    ctx_len = prompt("Context length (2048-32768)", "4096")
                    try:
                        ctx_val = int(ctx_len)
                    except ValueError:
                        ctx_val = 4096
                    print(f"\n  Loading {models[idx]['name']} with context {ctx_val}...")
                    result = client.load_model(model_key, context_length=ctx_val)
                    if result.get("status") == "loaded":
                        self._save_config({"llm_backend": "lmstudio", "lmstudio_model": model_key})
                        success(f"Loaded: {model_key} ({result.get('load_time_seconds', 0):.1f}s)")
                    else:
                        error(f"Load failed: {result.get('error', 'unknown error')}")
                else:
                    error("Invalid selection")
            except (ValueError, IndexError):
                error("Invalid selection")
            input("  Press Enter...")
            return

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                selected = models[idx]
                self._save_config({"llm_backend": "lmstudio", "lmstudio_model": selected["key"]})
                success(f"Selected: {selected['name']} ({selected['key']})")
                if not selected["loaded"]:
                    warning("Model not loaded in LM Studio. Load it in LM Studio or use [l] to load.")
            else:
                error("Invalid selection")
        except ValueError:
            error("Invalid input")
        input("  Press Enter...")
