"""Tests for ducky.redactor — comprehensive edge cases for AI slop patterns.

Targets common AI-generated code failures:
  1. None/empty input handling
  2. Unicode and emoji coexistence with PII
  3. Multiple adjacent PII items
  4. API key format variations
  5. Phone number format variations
  6. Path formats with special characters
  7. Comment stripping edge cases
  8. No-PII passthrough
"""

from __future__ import annotations

import unittest

from ducky.redactor import redact_text


class RedactorTests(unittest.TestCase):
    """Original test suite — preserved intact."""

    def test_redacts_common_private_bits(self):
        text = "email me at person@example.com from /Users/testuser/project with sk-abcdef1234567890"
        redacted = redact_text(text)
        self.assertNotIn("person@example.com", redacted)
        self.assertIn("[REDACTED_PATH]", redacted)
        self.assertNotIn("/Users/testuser", redacted)
        self.assertNotIn("sk-abcdef", redacted)

    def test_strips_comments(self):
        text = "# my password is secret\ndef f():\n    return 1\n"
        redacted = redact_text(text, strip_comments=True)
        self.assertIn("[REDACTED_COMMENT]", redacted)
        self.assertIn("def f", redacted)

    def test_redacts_phone_numbers(self):
        text = "Call me at 555-123-4567"
        redacted = redact_text(text)
        self.assertNotIn("555-123-4567", redacted)
        self.assertIn("[REDACTED_PHONE]", redacted)

    def test_redacts_windows_paths(self):
        text = "File at C:\\Users\\testuser\\file.txt"
        redacted = redact_text(text)
        self.assertNotIn("C:\\\\Users", redacted)
        self.assertIn("[REDACTED_PATH]", redacted)


# ═══════════════════════════════════════════════════════════════════════════
# AI Slop Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class RedactorEdgeCaseTests(unittest.TestCase):
    """Edge case tests for redact_text covering AI slop patterns."""

    # ── Null / empty input ───────────────────────────

    def test_none_text(self):
        """None input should not crash."""
        result = redact_text(None)  # type: ignore[arg-type]
        self.assertIsNone(result)

    def test_empty_string(self):
        result = redact_text("")
        self.assertEqual(result, "")

    def test_whitespace_only(self):
        result = redact_text("   \n  \t  ")
        self.assertEqual(result, "   \n  \t  ")

    # ── No PII in text ──────────────────────────────

    def test_no_pii_unchanged(self):
        text = "def hello():\n    print('Hello, world!')\n"
        result = redact_text(text)
        self.assertEqual(result, text)

    def test_code_with_builtin_keywords(self):
        """Keywords like 'pass' and 'return' should not trigger redaction."""
        text = "def process():\n    pass\n"
        result = redact_text(text)
        self.assertEqual(result, text)

    # ── Email edge cases ────────────────────────────

    def test_email_at_start(self):
        text = "admin@example.com is the admin email."
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)
        self.assertNotIn("admin@example.com", result)

    def test_email_at_end(self):
        text = "Contact: support@example.com"
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)

    def test_multiple_emails(self):
        text = "a@b.com and c@d.com and e@f.com"
        result = redact_text(text)
        self.assertEqual(result.count("[REDACTED_EMAIL]"), 3)

    def test_email_in_parentheses(self):
        text = "Email (user@example.com) for help."
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)
        self.assertNotIn("user@example.com", result)

    def test_email_with_plus(self):
        text = "Email: user+tag@example.com"
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)

    # ── Path edge cases ─────────────────────────────

    def test_multiple_paths(self):
        text = "/Users/a/proj and /Users/b/other and /private/tmp/x"
        result = redact_text(text)
        self.assertNotIn("/Users/a", result)
        self.assertNotIn("/Users/b", result)
        self.assertNotIn("/private/tmp", result)

    def test_path_with_spaces(self):
        text = "Located at /Users/testuser/My Project/file.py"
        result = redact_text(text)
        self.assertIn("[REDACTED_PATH]", result)

    def test_path_with_unicode(self):
        text = "Chemin: /Users/testuser/résumé/project"
        result = redact_text(text)
        self.assertIn("[REDACTED_PATH]", result)

    def test_var_folder_path(self):
        text = "Temp at /var/folders/abc123/T/"
        result = redact_text(text)
        self.assertIn("[REDACTED_PATH]", result)

    # ── Secret / API key edge cases ─────────────────

    def test_api_key_format(self):
        text = 'api_key = "sk-abcdefghijklmnop12345678"'
        result = redact_text(text)
        self.assertNotIn("sk-abcdefghijklmnop12345678", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_api_key_with_single_quotes(self):
        text = "token = 'ghp_abcdefghijklmnop1234567890abcdefghijklmno'"
        result = redact_text(text)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_api_key_mixed_case(self):
        text = "API_KEY=sk-abcdef1234567890abcdef12"
        result = redact_text(text)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_multiple_secrets(self):
        text = "sk-abc123def456ghi789jkl012 and sk-xyz789uvw012rst345"
        result = redact_text(text)
        self.assertIn("[REDACTED_SECRET]", result)

    # ── Phone number edge cases ─────────────────────

    def test_phone_with_country_code(self):
        text = "Call +1-555-123-4567"
        result = redact_text(text)
        self.assertIn("[REDACTED_PHONE]", result)

    def test_phone_parenthesized_area_code(self):
        text = "Call (555) 123-4567"
        result = redact_text(text)
        self.assertIn("[REDACTED_PHONE]", result)

    def test_phone_with_dots(self):
        text = "Call 555.123.4567"
        result = redact_text(text)
        self.assertIn("[REDACTED_PHONE]", result)

    # ── Mixed PII ───────────────────────────────────

    def test_mixed_pii_all_redacted(self):
        text = "User: user@example.com, phone 555-123-4567, at /Users/test/code"
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)
        self.assertIn("[REDACTED_PHONE]", result)
        self.assertIn("[REDACTED_PATH]", result)

    # ── Comment stripping edge cases ────────────────

    def test_strip_comments_false_noop(self):
        """Default strip_comments=False should not strip comments."""
        text = "# password=secret\ndef f():\n    pass\n"
        result = redact_text(text)
        self.assertNotIn("[REDACTED_COMMENT]", result)
        self.assertIn("# password=secret", result)

    def test_no_sensitive_comments(self):
        """Comments without sensitive keywords should remain unchanged."""
        text = "# This is a normal comment\ndef f():\n    pass\n"
        result = redact_text(text, strip_comments=True)
        self.assertIn("# This is a normal comment", result)

    def test_various_sensitive_keywords(self):
        """Comments containing password, key, token, secret, api, credential."""
        for keyword in ["password", "key", "token", "secret", "api", "credential"]:
            text = f"# my {keyword} is secret"
            result = redact_text(text, strip_comments=True)
            self.assertIn("[REDACTED_COMMENT]", result)
            self.assertNotIn(keyword, result)

    # ── Already redacted ────────────────────────────

    def test_already_redacted_no_double(self):
        """Text that already contains [REDACTED_*] should not double-redact."""
        text = "My email is [REDACTED_EMAIL] and my key is [REDACTED_SECRET]"
        result = redact_text(text)
        self.assertEqual(result, text)

    # ── Unicode / emoji ─────────────────────────────

    def test_unicode_with_email(self):
        text = "résumé: user@example.com"
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)
        self.assertIn("résumé", result)

    def test_emoji_with_pii(self):
        text = "Send to 📧 user@example.com 📞 555-123-4567"
        result = redact_text(text)
        self.assertIn("[REDACTED_EMAIL]", result)
        self.assertIn("[REDACTED_PHONE]", result)
        self.assertIn("📧", result)
        self.assertIn("📞", result)

    # ── Boundary / special chars ────────────────────

    def test_private_in_identifier(self):
        """Words containing 'private' should not be falsely redacted."""
        text = "def private_method():\n    pass\n"
        result = redact_text(text)
        self.assertEqual(result, text)

    def test_home_in_comment(self):
        """The word 'home' in a comment should not trigger path redaction."""
        text = "# work from home\n"
        result = redact_text(text)
        self.assertNotIn("[REDACTED_PATH]", result)


if __name__ == "__main__":
    unittest.main()
