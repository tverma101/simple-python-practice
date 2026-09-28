"""Shared helpers for the Ducky CLI."""

from __future__ import annotations

import json

from ..config import DEFAULT_CONFIG, save_config
from ..llm_client import OllamaClient
from ..runner import TestRunReport
from ..tutor import TutorEngine


def _record_progress(report: TestRunReport) -> None:
    from ..progress import ProgressTracker

    diagnosis = report.failures[0].diagnosis if report.failures else ""
    tracker = ProgressTracker()
    try:
        tracker.record_attempt(report.lesson_id, report.passed, diagnosis)
    finally:
        tracker.close()


def _make_tutor_engine(
    config: dict[str, object],
    *,
    ollama_model: str | None = None,
    ollama_url: str | None = None,
    no_llm: bool = False,
) -> TutorEngine | None:
    if no_llm:
        return None
    if ollama_model:
        base_url = ollama_url or str(config.get("ollama_url", DEFAULT_CONFIG["ollama_url"]))
        return TutorEngine(OllamaClient(model=ollama_model, base_url=base_url))
    backend = str(config.get("llm_backend", DEFAULT_CONFIG["llm_backend"]))
    if backend == "ollama":
        model = str(config.get("ollama_model", DEFAULT_CONFIG["ollama_model"]))
        base_url = str(config.get("ollama_url", DEFAULT_CONFIG["ollama_url"]))
        return TutorEngine(OllamaClient(model=model, base_url=base_url))
    elif backend == "gemini":
        from ..llm_client import GeminiClient
        api_key = str(config.get("gemini_api_key", ""))
        model = str(config.get("gemini_model", "gemini-2.5-flash"))
        return TutorEngine(GeminiClient(api_key=api_key, model=model))
    elif backend == "openai":
        from ..llm_client import OpenAIClient
        api_key = str(config.get("openai_api_key", ""))
        model = str(config.get("openai_model", "gpt-4o-mini"))
        base_url = str(config.get("openai_base_url", "https://api.openai.com/v1"))
        return TutorEngine(OpenAIClient(api_key=api_key, model=model, base_url=base_url))
    return None


def _print_config(config: dict[str, object]) -> None:
    print(json.dumps(config, indent=2, sort_keys=True))


def _get_active_class_id(args, config: dict[str, object]) -> str:
    class_id = getattr(args, "class_id", None)
    if class_id:
        return class_id
    return config.get("active_class_id", "default")


def _discover_lmstudio_models(url: str) -> list[dict] | None:
    """Fetch available LM Studio models. Returns None on failure."""
    from ..llm_client import LMStudioClient

    try:
        client = LMStudioClient(base_url=url)
        models = client.list_models()
        return models if models else None
    except Exception:
        return None


def _discover_ollama_models(url: str) -> list[dict] | None:
    """Fetch available Ollama models. Returns None on failure."""
    from ..llm_client import OllamaClient

    try:
        client = OllamaClient(base_url=url)
        models = client.list_models()
        return models if models else None
    except Exception:
        return None


def _pick_model(models: list[dict], label: str, current: str = "") -> str | None:
    """Display numbered model list and return selected model key.

    Returns None when the user chooses to enter a model name manually.
    """
    print(f"\n  Available {label} models:\n")
    for i, m in enumerate(models, 1):
        name = m.get("name", m.get("key", "unknown"))
        key = m.get("key", "")
        size = f"  ({m['size_gb']})" if m.get("size_gb") else ""
        loaded = "  [LOADED]" if m.get("loaded") else ""
        current_mark = "  <-- current" if key == current else ""
        print(f"    [{i}]  {name}{size}{loaded}{current_mark}")
    print("    [0]  Enter model name manually")
    print()
    choice = input("  Select model: ").strip()
    if choice == "0":
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            return models[idx].get("key", "")
    except ValueError:
        pass
    print("  Invalid selection.")
    return None


def _handle_hint() -> int:
    """Show hint for the most recent test failure."""
    import json

    from ..reports import ReportStore

    store = ReportStore()
    reports_dir = store.paths["reports"]
    reports = sorted(reports_dir.glob("*.json"), reverse=True)

    if not reports:
        print("No test reports found. Run tests first with: ducky test")
        return 1

    latest = reports[0]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        print("Could not read the latest report.")
        return 1

    failures = data.get("failures", [])
    if not failures:
        if data.get("passed"):
            print("✓ All tests passed! No hints needed.")
            return 0
        print("No failure details in the latest report.")
        return 1

    for i, failure in enumerate(failures, 1):
        hint = failure.get("hint", "")
        diagnosis = failure.get("diagnosis", "unknown")
        name = failure.get("name", "unknown")

        if hint:
            print(f"\n  [{i}] {name} ({diagnosis})")
            print(f"  {hint}\n")
        else:
            # Generate a basic hint from the diagnosis
            from ..diagnostics import hint_for_diagnosis

            print(f"\n  [{i}] {name} ({diagnosis})")
            print(f"  {hint_for_diagnosis(diagnosis)}\n")

    print("Run 'ducky test' to try again after fixing your code.")
    return 0


def _handle_open() -> int:
    """Open the current lesson in the editor."""
    from ..editor import is_vscode_available, open_lesson
    from ..workflows import resolve_current_lesson

    resolved = resolve_current_lesson()
    if resolved is None:
        return 1

    if is_vscode_available():
        ok = open_lesson(resolved["lesson_dir"], resolved["entry_file"])
        if ok:
            print(f"Opened {resolved['title']} in VS Code.")
            return 0

    # Fallback: show the file path
    print(f"Lesson: {resolved['title']}")
    print(f"  Code:  {resolved['entry_path']}")
    print(f"  Dir:   {resolved['lesson_dir']}")
    return 0


def _handle_setup_llm(config: dict) -> int:
    """Interactive LLM setup wizard."""
    print("\n  Ducky LLM Setup\n")
    print("  Ducky works great with rule-based hints (no LLM needed).")
    print("  Optionally, add a cloud API key for smarter hints.\n")
    print("  Cloud options:")
    print("    1) Gemini (free tier — get a key at https://aistudio.google.com/apikey)")
    print("    2) OpenAI (gpt-4o-mini — get a key at https://platform.openai.com/api-keys)")
    print("  Local options:")
    print("    3) Ollama (free, runs locally)")
    print("    4) LM Studio (free, runs locally)")
    print("    5) None — rule-based hints only (recommended for most students)")
    print()

    choice = input("  Choice [1-5]: ").strip()

    updates: dict[str, object] = {}

    if choice == "1":
        updates["llm_backend"] = "gemini"
        api_key = input("  Gemini API key: ").strip()
        if api_key:
            updates["gemini_api_key"] = api_key
        else:
            print("  No API key provided. Using rule-based hints.")
            updates["llm_backend"] = "none"
            path = save_config(updates)
            print(f"\n  Config saved: {path}")
            return 0

        model = input("  Model [gemini-2.5-flash]: ").strip()
        if model:
            updates["gemini_model"] = model

        print("\n  Testing Gemini...")
        try:
            from ..llm_client import GeminiClient, HintRequest

            client = GeminiClient(
                api_key=str(updates.get("gemini_api_key", "")),
                model=str(updates.get("gemini_model", "gemini-2.5-flash")),
            )
            test_req = HintRequest(
                lesson_id="test",
                title="Test",
                code_snippet="def add(a, b):\n    return a + b\n",
                diagnosis="wrong_value",
                expected=5,
                actual=3,
            )
            test = client.generate_hint(test_req)
            if test:
                print(f"  ✓ Gemini is working! Hint: {test[:80]}...")
            else:
                print("  ⚠ Gemini returned no hint. Check your API key.")
        except Exception as exc:
            print(f"  ⚠ Warning: Could not connect to Gemini: {exc}")
            print("    You can still use rule-based hints.")

    elif choice == "2":
        updates["llm_backend"] = "openai"
        api_key = input("  OpenAI API key: ").strip()
        if api_key:
            updates["openai_api_key"] = api_key
        else:
            print("  No API key provided. Using rule-based hints.")
            updates["llm_backend"] = "none"
            path = save_config(updates)
            print(f"\n  Config saved: {path}")
            return 0

        base_url = input("  API base URL [https://api.openai.com/v1]: ").strip()
        if base_url:
            updates["openai_base_url"] = base_url

        model = input("  Model [gpt-4o-mini]: ").strip()
        if model:
            updates["openai_model"] = model

        print("\n  Testing OpenAI...")
        try:
            from ..llm_client import HintRequest, OpenAIClient

            client = OpenAIClient(
                api_key=str(updates.get("openai_api_key", "")),
                model=str(updates.get("openai_model", "gpt-4o-mini")),
                base_url=str(updates.get("openai_base_url", "https://api.openai.com/v1")),
            )
            test_req = HintRequest(
                lesson_id="test",
                title="Test",
                code_snippet="def add(a, b):\n    return a + b\n",
                diagnosis="wrong_value",
                expected=5,
                actual=3,
            )
            test = client.generate_hint(test_req)
            if test:
                print(f"  ✓ OpenAI is working! Hint: {test[:80]}...")
            else:
                print("  ⚠ OpenAI returned no hint. Check your API key.")
        except Exception as exc:
            print(f"  ⚠ Warning: Could not connect to OpenAI: {exc}")
            print("    You can still use rule-based hints.")

    elif choice == "3":
        updates["llm_backend"] = "ollama"
        url = input("  Ollama URL [http://localhost:11434]: ").strip() or "http://localhost:11434"
        updates["ollama_url"] = url

        print(f"\n  Connecting to {url}...")
        models = _discover_ollama_models(url)
        if models:
            current = config.get("ollama_model", "")
            selected = _pick_model(models, "Ollama", current)
            if selected:
                updates["ollama_model"] = selected
            else:
                model = input("  Model name [qwen2.5-coder:1.5b]: ").strip()
                if model:
                    updates["ollama_model"] = model
        else:
            print("  Could not fetch models. Is Ollama running?")
            model = input("  Model name [qwen2.5-coder:1.5b]: ").strip()
            if model:
                updates["ollama_model"] = model

        print("\n  Testing Ollama...")
        try:
            from ..llm_client import HintRequest, OllamaClient

            client = OllamaClient(
                model=str(updates.get("ollama_model", "qwen2.5-coder:1.5b")),
                base_url=str(updates.get("ollama_url", "http://localhost:11434")),
            )
            test_req = HintRequest(
                lesson_id="test",
                title="Test",
                code_snippet="def add(a, b):\n    return a + b\n",
                diagnosis="wrong_value",
                expected=5,
                actual=3,
            )
            test = client.generate_hint(test_req)
            print(f"  ✓ Ollama is working! Hint: {test[:80]}..." if test else "  ⚠ No response from Ollama")
        except Exception as exc:
            print(f"  ⚠ Warning: Could not connect to Ollama: {exc}")

    elif choice == "4":
        updates["llm_backend"] = "lmstudio"
        url = input("  LM Studio URL [http://localhost:1234]: ").strip() or "http://localhost:1234"
        updates["lmstudio_url"] = url

        print(f"\n  Connecting to {url}...")
        models = _discover_lmstudio_models(url)
        if models:
            current = config.get("lmstudio_model", "")
            selected = _pick_model(models, "LM Studio", current)
            if selected:
                updates["lmstudio_model"] = selected
            else:
                model = input("  Model name [default]: ").strip()
                if model:
                    updates["lmstudio_model"] = model
        else:
            print("  Could not fetch models. Is LM Studio running with the server enabled?")
            model = input("  Model name [default]: ").strip()
            if model:
                updates["lmstudio_model"] = model

        # Offer to load model into LM Studio with context_length
        model_key = str(updates.get("lmstudio_model", ""))
        if models and model_key:
            matched = [m for m in models if m["key"] == model_key]
            if matched and not matched[0].get("loaded"):
                print(f"\n  Model '{matched[0]['name']}' is not loaded in LM Studio.")
                load_it = input("  Load it now? [Y/n]: ").strip().lower()
                if load_it != "n":
                    ctx_str = input("  Context length (2048-32768) [8192]: ").strip() or "8192"
                    try:
                        ctx_len = max(2048, min(32768, int(ctx_str)))
                    except ValueError:
                        ctx_len = 8192
                    from ..llm_client import LMStudioClient

                    loader = LMStudioClient(base_url=url)
                    print(f"  Loading {model_key} with context {ctx_len}...")
                    result = loader.load_model(model_key, context_length=ctx_len)
                    if result.get("status") == "loaded":
                        print(f"  ✓ Loaded in {result.get('load_time_seconds', 0):.1f}s")
                    else:
                        print(f"  ⚠ Load failed: {result.get('error', 'unknown error')}")

        # Set output token limit
        current_tokens = config.get("lmstudio_max_tokens", 2500)
        tokens_str = input(f"\n  Max output tokens (min 2500) [{current_tokens}]: ").strip()
        if tokens_str:
            try:
                tokens_val = max(2500, int(tokens_str))
                updates["lmstudio_max_tokens"] = tokens_val
            except ValueError:
                pass

        print("\n  Testing LM Studio...")
        try:
            from ..llm_client import LMStudioClient

            client = LMStudioClient(
                model=str(updates.get("lmstudio_model", "default")),
                base_url=str(updates.get("lmstudio_url", "http://localhost:1234")),
                max_tokens=int(updates.get("lmstudio_max_tokens", current_tokens)),
            )
            test_req = HintRequest(
                lesson_id="test",
                title="Test",
                code_snippet="def add(a, b):\n    return a + b\n",
                diagnosis="wrong_value",
                expected=5,
                actual=3,
            )
            test = client.generate_hint(test_req)
            print("  ✓ LM Studio is working!" if test else "  ⚠ No response from LM Studio")
        except Exception as exc:
            print(f"  ⚠ Warning: Could not connect to LM Studio: {exc}")

    elif choice == "5":
        updates["llm_backend"] = "none"
        print("  ✓ Using rule-based hints only (no LLM). This works great!")
    else:
        print("  Invalid choice.")
        return 1

    if updates:
        path = save_config(updates)
        print(f"\n  Config saved: {path}")
    print("  Run 'ducky config set --llm-backend none' to disable LLM hints.")
    return 0
