from __future__ import annotations

import base64
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse

from .runner import TestRunReport


class TeacherBackendError(RuntimeError):
    pass


def _response_text(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def _response_preview(payload: bytes, limit: int = 200) -> str:
    text = _response_text(payload).strip().replace("\r", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _default_output_name(source_url: str, fallback_suffix: str = ".bin") -> str:
    parsed = urlparse(source_url)
    name = Path(parsed.path).name
    if name:
        return name
    return f"downloaded{fallback_suffix}"


def _require_http_url(value: str, *, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TeacherBackendError(f"{label} must be an http or https URL: {value!r}")
    return value


def _decode_content_value(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except Exception:
            return value.encode("utf-8")
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False).encode("utf-8")
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False).encode("utf-8")
    return str(value).encode("utf-8")


@dataclass
class TeacherBackendClient:
    submit_url: str = ""
    fetch_url: str = ""
    token: str = ""
    timeout_seconds: int = 8

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "ducky/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _open(self, request: urllib.request.Request):
        try:
            return urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            raise TeacherBackendError(f"Teacher backend returned HTTP {exc.code} for {request.full_url}") from exc
        except URLError as exc:
            raise TeacherBackendError(f"Could not reach teacher backend at {request.full_url}: {exc.reason}") from exc
        except Exception as exc:
            raise TeacherBackendError(f"Could not reach teacher backend at {request.full_url}: {exc}") from exc

    def submit_report(
        self, report: TestRunReport, *, student_name: str = "", include_raw: bool = False
    ) -> dict[str, Any]:
        submit_url = _require_http_url(self.submit_url, label="Teacher submit URL")

        payload: dict[str, Any] = {
            "student_name": student_name,
            "report": report.to_dict(include_raw=include_raw),
            "metadata": {
                "lesson_id": report.lesson_id,
                "title": report.title,
                "passed": report.passed,
                "returncode": report.returncode,
                "duration_seconds": report.duration_seconds,
                "include_raw": include_raw,
            },
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            submit_url,
            data=data,
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        with self._open(request) as response:
            body = response.read()
            if not body:
                return {"ok": True}
            try:
                decoded = json.loads(_response_text(body))
            except json.JSONDecodeError:
                return {"ok": True, "message": _response_preview(body)}
            if isinstance(decoded, dict):
                return decoded
            return {"ok": True, "response": decoded}

    def fetch_file(self, *, output_path: str | Path | None = None) -> Path:
        fetch_url = _require_http_url(self.fetch_url, label="Teacher fetch URL")

        request = urllib.request.Request(
            fetch_url,
            headers=self._headers(accept="application/octet-stream, text/plain, application/json"),
            method="GET",
        )
        with self._open(request) as response:
            body = response.read()
            if not body:
                raise TeacherBackendError("Teacher backend returned an empty response while fetching code.")
            response_url = fetch_url
            if hasattr(response, "geturl"):
                try:
                    response_url = str(response.geturl()) or fetch_url
                except Exception:
                    response_url = fetch_url
            content_type = response.headers.get_content_type()
            if content_type == "application/json":
                try:
                    decoded = json.loads(_response_text(body))
                except json.JSONDecodeError as exc:
                    raise TeacherBackendError(
                        f"Teacher backend returned invalid JSON while fetching code: {_response_preview(body)}"
                    ) from exc
                if isinstance(decoded, dict):
                    nested_url = decoded.get("download_url") or decoded.get("fetch_url") or decoded.get("code_url")
                    if nested_url:
                        nested_target = urljoin(response_url, str(nested_url))
                        nested = TeacherBackendClient(
                            submit_url=self.submit_url,
                            fetch_url=_require_http_url(nested_target, label="Nested download URL"),
                            token=self.token,
                            timeout_seconds=self.timeout_seconds,
                        )
                        return nested.fetch_file(output_path=output_path)
                    if "content" in decoded:
                        body = _decode_content_value(decoded["content"])
                    elif "code" in decoded:
                        body = _decode_content_value(decoded["code"])
                    elif "source" in decoded:
                        body = _decode_content_value(decoded["source"])
                    elif "error" in decoded:
                        raise TeacherBackendError(f"Teacher backend fetch failed: {decoded['error']}")
                    else:
                        raise TeacherBackendError("Teacher backend JSON response did not include code content.")
                else:
                    raise TeacherBackendError("Teacher backend JSON response was not an object.")

            if output_path is None:
                output_path = _default_output_name(fetch_url)
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(body)
            return output
