from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .paths import app_dir, ensure_app_dirs

DEFAULT_CONFIG: dict[str, Any] = {
    "llm_backend": "none",
    "ollama_model": "qwen2.5-coder:1.5b",
    "ollama_url": "http://localhost:11434",
    "lmstudio_model": "default",
    "lmstudio_url": "http://localhost:1234",
    "lmstudio_max_tokens": 2500,
    "teacher_submit_url": "",
    "teacher_fetch_url": "",
    "teacher_token": "",
    "teacher_access_code": "",
    "teacher_admin_token": "",
    "teacher_class_id": "",
    "seen_announcement_ids": [],
    "teacher_upload_all": False,
    "student_name": "",
    "teacher_url": "",
    "server_port": 8420,
    "server_bind_host": "0.0.0.0",
    "connection_timeout_seconds": 10,
    "sync_retry_interval_seconds": 60,
    "teacher_backup_path": "",
    "auto_update": False,
    "update_interval_seconds": 300,
    "auto_update_app": True,
    "r2_base_url": "https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev",
    "auto_sync_progress": False,
    "workspace_dir": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "openai_base_url": "https://api.openai.com/v1",
    "github_owner": "",
    "github_repo": "",
    "github_api_override_base": "",
}

_CONFIG_LOCK = threading.RLock()


def config_path(base: Path | None = None) -> Path:
    return (base or app_dir()) / "config.json"


def load_config(base: Path | None = None) -> dict[str, Any]:
    path = config_path(base)
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        return config

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return config

    if isinstance(loaded, dict):
        for key, value in loaded.items():
            if value is not None:
                default_val = DEFAULT_CONFIG.get(key)
                if (
                    isinstance(default_val, int)
                    and not isinstance(default_val, bool)
                    and not isinstance(value, bool)
                    and isinstance(value, str)
                ):
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        pass
                config[key] = value
    return config


def save_config(updates: dict[str, Any], base: Path | None = None) -> Path:
    with _CONFIG_LOCK:
        dirs = ensure_app_dirs(base)
        path = dirs["base"] / "config.json"
        current = load_config(base)
        for key, value in updates.items():
            if value is not None:
                current[key] = value
        # Use a unique sibling temp file plus fsync. A shared ``config.tmp``
        # allowed background sync and the TUI to overwrite each other's temp
        # file and occasionally raise FileNotFoundError during replace.
        fd, temporary = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return path


def build_llm_client(config: dict[str, Any] | None = None) -> Any:
    from .llm_client import GeminiClient, LMStudioClient, NoLLMClient, OllamaClient, OpenAIClient

    if config is None:
        config = load_config()

    backend = config.get("llm_backend", "none")
    if backend == "ollama":
        return OllamaClient(
            model=config.get("ollama_model", "qwen2.5-coder:1.5b"),
            base_url=config.get("ollama_url", "http://localhost:11434"),
        )
    elif backend == "lmstudio":
        return LMStudioClient(
            model=config.get("lmstudio_model", "default"),
            base_url=config.get("lmstudio_url", "http://localhost:1234"),
            max_tokens=config.get("lmstudio_max_tokens", 2500),
        )
    elif backend == "gemini":
        return GeminiClient(
            api_key=config.get("gemini_api_key", ""),
            model=config.get("gemini_model", "gemini-2.5-flash"),
        )
    elif backend == "openai":
        return OpenAIClient(
            api_key=config.get("openai_api_key", ""),
            model=config.get("openai_model", "gpt-4o-mini"),
            base_url=config.get("openai_base_url", "https://api.openai.com/v1"),
        )
    return NoLLMClient()


def get_student_config() -> dict[str, Any]:
    config = load_config()
    return {
        "teacher_url": config.get("teacher_url", ""),
        "teacher_token": config.get("teacher_token", ""),
        "student_name": config.get("student_name", ""),
        "connection_timeout_seconds": config.get("connection_timeout_seconds", 10),
        "sync_retry_interval_seconds": config.get("sync_retry_interval_seconds", 60),
        "auto_update": config.get("auto_update", False),
        "update_interval_seconds": config.get("update_interval_seconds", 300),
        "llm_backend": config.get("llm_backend", "none"),
        "auto_update_app": config.get("auto_update_app", True),
        "auto_sync_progress": config.get("auto_sync_progress", False),
    }
