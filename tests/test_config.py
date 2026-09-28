"""Tests for ducky.config — comprehensive edge cases for AI slop patterns.

Targets common AI-generated code failures:
  1. Partial config file with missing keys
  2. Extra unknown keys in config
  3. Type coercion for various config values
  4. Unicode config values
  5. Config file in non-existent directory
  6. Empty JSON object as config
  7. Build LLM client with various backends
  8. Save with quirky key names
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ducky.config import DEFAULT_CONFIG, build_llm_client, load_config, save_config
from ducky.llm_client import LMStudioClient, NoLLMClient, OllamaClient


class ConfigTests(unittest.TestCase):
    """Original test suite — preserved intact."""

    def test_load_config_creates_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = load_config(base)
            for key, value in DEFAULT_CONFIG.items():
                self.assertIn(key, result)
                self.assertEqual(result[key], value)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            updates = {"student_name": "TestStudent", "llm_backend": "ollama", "server_port": 9999}
            save_config(updates, base)
            loaded = load_config(base)
            self.assertEqual(loaded["student_name"], "TestStudent")
            self.assertEqual(loaded["llm_backend"], "ollama")
            self.assertEqual(loaded["server_port"], 9999)

    def test_load_config_with_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "config.json"
            config_path.write_text("NOT JSON {{{", encoding="utf-8")
            result = load_config(base)
            for key, value in DEFAULT_CONFIG.items():
                self.assertIn(key, result)
                self.assertEqual(result[key], value)

    def test_load_config_with_non_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "config.json"
            config_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            result = load_config(base)
            for key, value in DEFAULT_CONFIG.items():
                self.assertIn(key, result)
                self.assertEqual(result[key], value)

    def test_build_llm_client_none_backend(self):
        client = build_llm_client({"llm_backend": "none"})
        self.assertIsInstance(client, NoLLMClient)

    def test_build_llm_client_default(self):
        client = build_llm_client({})
        self.assertIsInstance(client, NoLLMClient)

    def test_build_llm_client_ollama(self):
        config = {"llm_backend": "ollama", "ollama_model": "test-model", "ollama_url": "http://localhost:11434"}
        client = build_llm_client(config)
        self.assertIsInstance(client, OllamaClient)
        self.assertEqual(client.model, "test-model")
        self.assertEqual(client.base_url, "http://localhost:11434")

    def test_build_llm_client_lmstudio(self):
        config = {
            "llm_backend": "lmstudio",
            "lmstudio_model": "my-model",
            "lmstudio_url": "http://localhost:1234",
            "lmstudio_max_tokens": 1000,
        }
        client = build_llm_client(config)
        self.assertIsInstance(client, LMStudioClient)
        self.assertEqual(client.model, "my-model")
        self.assertEqual(client.max_tokens, 1000)

    def test_config_type_coercion(self):
        config = {
            "llm_backend": "lmstudio",
            "lmstudio_model": "m",
            "lmstudio_url": "http://x",
            "lmstudio_max_tokens": "2500",
        }
        client = build_llm_client(config)
        self.assertIsInstance(client, LMStudioClient)
        self.assertEqual(client.max_tokens, "2500")

    def test_save_preserves_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            save_config({"student_name": "Alice"}, base)
            save_config({"llm_backend": "ollama"}, base)
            loaded = load_config(base)
            self.assertEqual(loaded["student_name"], "Alice")
            self.assertEqual(loaded["llm_backend"], "ollama")

    def test_load_config_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nonexistent"
            result = load_config(base)
            self.assertEqual(result, DEFAULT_CONFIG)


class ConfigPathTests(unittest.TestCase):
    def test_uses_base_if_provided(self):
        from ducky.config import config_path

        p = config_path(Path("/custom/base"))
        self.assertEqual(p, Path("/custom/base/config.json"))


class GetStudentConfigTests(unittest.TestCase):
    def test_returns_expected_keys(self):
        from ducky.config import get_student_config

        result = get_student_config()
        expected_keys = {
            "teacher_url",
            "teacher_token",
            "student_name",
            "auto_update",
            "update_interval_seconds",
            "llm_backend",
        }
        self.assertTrue(expected_keys.issubset(set(result.keys())))


# ═══════════════════════════════════════════════════════════════════════════
# AI Slop Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class ConfigEdgeCaseTests(unittest.TestCase):
    """Edge case tests for config module."""

    def test_partial_config_merges_with_defaults(self):
        """Config with only some keys should merge with defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Write a partial config
            (base / "config.json").write_text(json.dumps({"student_name": "Alice"}), encoding="utf-8")
            result = load_config(base)
            self.assertEqual(result["student_name"], "Alice")
            # All other keys should still be from defaults
            for key, value in DEFAULT_CONFIG.items():
                if key != "student_name":
                    self.assertEqual(result[key], value)

    def test_extra_unknown_keys_preserved(self):
        """Extra keys not in DEFAULT_CONFIG should be preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(json.dumps({"extra_key": "extra_value"}), encoding="utf-8")
            result = load_config(base)
            self.assertIn("extra_key", result)
            self.assertEqual(result["extra_key"], "extra_value")

    def test_empty_json_object(self):
        """Empty JSON object should merge with defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text("{}", encoding="utf-8")
            result = load_config(base)
            for key, value in DEFAULT_CONFIG.items():
                self.assertEqual(result[key], value)

    def test_unicode_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            updates = {"student_name": "Zoë Müller-Sánchez"}
            save_config(updates, base)
            loaded = load_config(base)
            self.assertEqual(loaded["student_name"], "Zoë Müller-Sánchez")

    def test_boolean_config_values(self):
        """Boolean config values should be preserved as-is."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(json.dumps({"auto_update": False, "show_hints": True}), encoding="utf-8")
            result = load_config(base)
            self.assertFalse(result["auto_update"])
            self.assertTrue(result["show_hints"])

    def test_numeric_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(json.dumps({"server_port": 8080, "max_retries": 3}), encoding="utf-8")
            result = load_config(base)
            self.assertEqual(result["server_port"], 8080)
            self.assertEqual(result["max_retries"], 3)

    def test_save_with_nonexistent_dir_creates(self):
        """Save should create the base directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "new_dir" / "nested"
            save_config({"student_name": "test"}, base)
            self.assertTrue((base / "config.json").exists())
            loaded = load_config(base)
            self.assertEqual(loaded["student_name"], "test")

    def test_save_overrides_existing(self):
        """Saving the same key twice should override."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            save_config({"student_name": "Alice"}, base)
            save_config({"student_name": "Bob"}, base)
            loaded = load_config(base)
            self.assertEqual(loaded["student_name"], "Bob")

    def test_long_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            long_student_name = "A" * 10000
            save_config({"student_name": long_student_name}, base)
            loaded = load_config(base)
            self.assertEqual(loaded["student_name"], long_student_name)

    def test_save_does_not_affect_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            save_config({"key_a": "a", "key_b": "b"}, base)
            save_config({"key_a": "modified"}, base)
            loaded = load_config(base)
            self.assertEqual(loaded["key_a"], "modified")
            self.assertEqual(loaded["key_b"], "b")


class BuildLLMClientEdgeCaseTests(unittest.TestCase):
    """Edge cases for build_llm_client."""

    def test_ollama_no_model(self):
        client = build_llm_client({"llm_backend": "ollama"})
        self.assertIsInstance(client, OllamaClient)

    def test_ollama_custom_url(self):
        client = build_llm_client({"llm_backend": "ollama", "ollama_url": "http://custom:8080"})
        self.assertIsInstance(client, OllamaClient)
        self.assertEqual(client.base_url, "http://custom:8080")

    def test_lmstudio_custom_max_tokens(self):
        client = build_llm_client({"llm_backend": "lmstudio", "lmstudio_model": "m", "lmstudio_url": "http://x:1234", "lmstudio_max_tokens": 500})
        self.assertIsInstance(client, LMStudioClient)
        self.assertEqual(client.max_tokens, 500)

    def test_lmstudio_max_tokens_none(self):
        client = build_llm_client({"llm_backend": "lmstudio", "lmstudio_model": "m", "lmstudio_url": "http://x:1234"})
        self.assertIsInstance(client, LMStudioClient)
        self.assertIsNotNone(client)

    def test_unknown_backend_returns_none(self):
        client = build_llm_client({"llm_backend": "nonexistent-backend"})
        self.assertIsInstance(client, NoLLMClient)

    def test_empty_backend_string(self):
        client = build_llm_client({"llm_backend": ""})
        self.assertIsInstance(client, NoLLMClient)


class ConfigPathEdgeCaseTests(unittest.TestCase):
    def test_config_path_empty_base(self):
        from ducky.config import config_path

        p = config_path(Path(""))
        self.assertIsNotNone(p)
        self.assertTrue(str(p).endswith("config.json"))


if __name__ == "__main__":
    unittest.main()
