from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
POSIX_PATH_RE = re.compile(
    r"/(?:(?:Users|home|private|tmp|var/(?:folders|tmp?))/(?:[^\s'\"]+))"
)
WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]+")
SECRET_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*=\s*['\"][^'\"]+['\"]|token\s*=\s*['\"][^'\"]+['\"])")
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]\d{4}\b")
COMMENT_RE = re.compile(r"^\s*#.*(?:password|key|token|secret|api|credential).*$", re.IGNORECASE | re.MULTILINE)


def redact_text(text: str, *, strip_comments: bool = False) -> str:
    if not text:
        return text
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = POSIX_PATH_RE.sub("[REDACTED_PATH]", text)
    text = WIN_PATH_RE.sub("[REDACTED_PATH]", text)
    text = SECRET_RE.sub("[REDACTED_SECRET]", text)
    text = PHONE_RE.sub("[REDACTED_PHONE]", text)
    if strip_comments:
        text = COMMENT_RE.sub("# [REDACTED_COMMENT]", text)
    return text
