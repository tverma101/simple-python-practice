from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from .config import load_config, save_config
from .outbox import DurableOutbox
from .pack import read_manifest
from .paths import app_dir

logger = logging.getLogger(__name__)


class StudentClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    """Stable shape for a progress sync while preserving the legacy dict API."""

    sent: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()
    synced_ids: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "errors": list(self.errors),
            "synced_ids": list(self.synced_ids),
        }


@dataclass(frozen=True)
class SubmissionStatus:
    submission_id: str
    status: str
    queued: bool = False
    queue_id: str = ""


@dataclass(frozen=True)
class PackTransferResult:
    pack_id: str
    path: Path
    sha256: str
    replaced_atomically: bool = True


def _retry(func, *args, retries=2, delay=1.0, **kwargs):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except StudentClientError as exc:
            # Retry StudentClientError too — it wraps connection/timeout errors which
            # ARE transient. HTTP/application errors are already a definitive
            # response and should return immediately instead of making a student
            # wait through three identical retries.
            last_exc = exc
            if str(exc).startswith("Server error:") or str(exc).startswith("Teacher server returned"):
                break
            if attempt < retries:
                time.sleep(delay * (attempt + 1))
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(delay * (attempt + 1))
    raise last_exc


@dataclass
class StudentClient:
    teacher_url: str = ""
    token: str = ""
    student_name: str = ""
    timeout_seconds: int = 10
    # Config-backed student sessions validate downloaded archives at the
    # distribution boundary. Direct construction stays compatible with older
    # local fixtures that use opaque bytes.
    strict_pack_validation: bool = False

    def __post_init__(self) -> None:
        if self.teacher_url:
            self.teacher_url = self.teacher_url.strip()
            if self.teacher_url and not self.teacher_url.startswith(("http://", "https://")):
                self.teacher_url = f"http://{self.teacher_url}"

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "ducky-student/0.2"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _open(self, request: urllib.request.Request, *, timeout: int | None = None):
        effective_timeout = timeout if timeout is not None else self.timeout_seconds

        def _do_open():
            try:
                return urllib.request.urlopen(request, timeout=effective_timeout)
            except HTTPError as exc:
                if exc.code == 401 and not getattr(self, "_reconnect_in_progress", False):
                    # Reconnect at most once for this request. Clear the guard
                    # afterwards so a later request can recover from an expired
                    # token again, while nested /api/connect cannot recurse.
                    self._reconnect_in_progress = True
                    try:
                        if self._maybe_reconnect_on_stale_token(request):
                            # The handler above mutates this client in place (new
                            # token) and re-issues the original request once.
                            return self._reissue(request, timeout=effective_timeout)
                    finally:
                        self._reconnect_in_progress = False
                body = exc.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(body)
                    msg = data.get("error", body)
                except Exception:
                    msg = body or f"HTTP {exc.code}"
                raise StudentClientError(f"Server error: {msg}") from exc
            except URLError as exc:
                raise StudentClientError(f"Could not reach teacher server: {exc.reason}") from exc
            except Exception as exc:
                raise StudentClientError(f"Connection failed: {exc}") from exc

        return _retry(_do_open)

    def _maybe_reconnect_on_stale_token(self, request: urllib.request.Request) -> bool:
        """Attempt a one-time transparent reconnect when the server rejects our token.

        Returns True if a reconnect was attempted and the caller should retry the
        original request (via :meth:`_reissue`).

        The caller sets a guard before calling :meth:`connect` so that the reconnect
        attempt itself cannot re-enter this method (a permanent 401 during connect
        would otherwise recurse forever). The caller clears that guard afterwards,
        allowing future requests to recover again.
        """
        config = load_config()
        access_code = config.get("teacher_access_code", "")
        name = self.student_name or config.get("student_name", "")
        if not access_code or not name or not self.teacher_url:
            return False
        try:
            reconnect_result = self.connect(access_code, name)
        except StudentClientError:
            # Code no longer valid — leave the original 401 to surface.
            return False
        if not reconnect_result.get("token"):
            return False
        # Refresh the Authorization header on the original request so _reissue works.
        request.add_header("Authorization", f"Bearer {self.token}")
        return True

    def _reissue(self, request: urllib.request.Request, *, timeout: int | None = None):
        effective_timeout = timeout if timeout is not None else self.timeout_seconds
        try:
            return urllib.request.urlopen(request, timeout=effective_timeout)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
                msg = data.get("error", body)
            except Exception:
                msg = body or f"HTTP {exc.code}"
            raise StudentClientError(f"Server error: {msg}") from exc
        except URLError as exc:
            raise StudentClientError(f"Could not reach teacher server: {exc.reason}") from exc
        except Exception as exc:
            raise StudentClientError(f"Connection failed: {exc}") from exc

    def _post_json(self, path: str, data: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        url = f"{self.teacher_url.rstrip('/')}{path}"
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        with self._open(request, timeout=timeout) as response:
            resp_body = response.read()
            if not resp_body:
                return {"ok": True}
            try:
                payload = json.loads(resp_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise StudentClientError("Teacher server returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise StudentClientError("Teacher server returned a non-object response")
            return payload

    def _get_json(self, path: str, *, timeout: int | None = None) -> dict[str, Any]:
        url = f"{self.teacher_url.rstrip('/')}{path}"
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        with self._open(request, timeout=timeout) as response:
            resp_body = response.read()
            if not resp_body:
                return {}
            try:
                payload = json.loads(resp_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise StudentClientError("Teacher server returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise StudentClientError("Teacher server returned a non-object response")
            return payload

    def _get_bytes(self, path: str) -> tuple[bytes, dict[str, str]]:
        url = f"{self.teacher_url.rstrip('/')}{path}"
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        with self._open(request) as response:
            return response.read(), {key.lower(): value for key, value in response.headers.items()}

    def connect(self, access_code: str, student_name: str | None = None) -> dict[str, Any]:
        name = student_name or self.student_name
        if not name:
            raise StudentClientError("Student name is required")
        if not access_code:
            raise StudentClientError("Access code is required")

        if not self.teacher_url:
            from .network import discover_teacher_servers

            servers = discover_teacher_servers()
            if len(servers) == 1:
                self.teacher_url = str(servers[0]["url"])
            elif len(servers) > 1:
                names = ", ".join(str(s.get("class_name", s.get("url", "teacher"))) for s in servers)
                raise StudentClientError(
                    f"Found multiple teacher servers ({names}). Enter the teacher URL and try again."
                )
            else:
                raise StudentClientError(
                    "No teacher server found on this network. Make sure Teacher Server is running, "
                    "or enter its LAN URL (for example http://192.168.1.5:8420)."
                )

        # Quick health check first (short timeout) so the user gets a fast,
        # clear error instead of waiting 10+ seconds for a TCP timeout.
        try:
            self._get_json("/api/health", timeout=3)
        except StudentClientError as exc:
            raise StudentClientError(
                f"Could not reach teacher server at {self.teacher_url}. "
                "Is the server running? Start it from Teacher → Server Control → Start Server."
            ) from exc

        result = self._post_json("/api/connect", {"name": name, "code": access_code})
        self.token = result.get("token", "")
        self.student_name = name

        config_updates = {
            "teacher_url": self.teacher_url,
            "teacher_token": self.token,
            "student_name": name,
            "teacher_access_code": access_code,
        }
        save_config(config_updates)
        return result

    def list_packs(self) -> list[dict[str, Any]]:
        result = self._get_json("/api/packs")
        packs = result.get("packs", [])
        if not isinstance(packs, list):
            raise StudentClientError("Teacher server returned an invalid pack catalog")
        return [item for item in packs if isinstance(item, dict)]

    def download_pack(self, pack_id: str, dest: Path | str | None = None) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pack_id):
            raise StudentClientError("Invalid pack ID")
        data, headers = self._get_bytes(f"/api/packs/{pack_id}/download")
        if not data:
            raise StudentClientError(f"Empty response downloading pack: {pack_id}")

        expected_hash = headers.get("x-ducky-sha256", "")
        actual_hash = hashlib.sha256(data).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise StudentClientError("Pack checksum does not match the teacher's copy")

        if dest is None:
            dest = app_dir() / "packs" / f"{pack_id}.duckypack"
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Validate the archive before replacing the current lesson pack. The
        # temporary file is in the same directory so os.replace is atomic.
        fd, tmp_name = tempfile.mkstemp(prefix=f".{pack_id}.", suffix=".duckypack.tmp", dir=dest.parent)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(data)
            # Older classroom fixtures can contain opaque pack bytes. Real
            # duckypacks are always validated; keeping the opaque path preserves
            # compatibility with those local databases while never replacing an
            # existing archive with a malformed ZIP.
            if zipfile.is_zipfile(tmp_path):
                # The teacher may use a stable pack ID that differs from the
                # manifest lesson ID. The manifest itself is still parsed and
                # validated, but pack_id is not incorrectly treated as a lesson
                # ID here.
                read_manifest(tmp_path)
            elif dest.exists():
                raise StudentClientError("Downloaded pack is not a valid duckypack archive")
            os.replace(tmp_path, dest)
        except StudentClientError:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise StudentClientError(f"Downloaded pack is invalid: {exc}") from exc
        return dest

    def queue_submission(
        self, pack_id: str, report: dict[str, Any], student_code: str = "", submission_id: str | None = None
    ) -> dict[str, Any]:
        from uuid import uuid4

        payload = {
            "pack_id": pack_id,
            "report_json": report,
            "student_code": student_code,
            "submission_id": submission_id or f"student-{uuid4().hex}",
        }
        try:
            item = DurableOutbox().add("submission", payload)
        except RuntimeError as exc:
            raise StudentClientError(str(exc)) from exc
        return {"ok": False, "queued": True, "queue_id": item["queue_id"], "submission_id": payload["submission_id"]}

    def submit(
        self,
        pack_id: str,
        report: dict[str, Any],
        student_code: str = "",
        *,
        submission_id: str | None = None,
        queue_on_failure: bool = False,
    ) -> dict[str, Any]:
        from uuid import uuid4

        payload = {
            "pack_id": pack_id,
            "report_json": report,
            "student_code": student_code,
            "submission_id": submission_id or f"student-{uuid4().hex}",
        }
        try:
            return self._post_json("/api/submit", payload)
        except StudentClientError:
            if queue_on_failure:
                try:
                    item = DurableOutbox().add("submission", payload)
                except RuntimeError as queue_exc:
                    raise StudentClientError(str(queue_exc)) from queue_exc
                return {
                    "ok": False,
                    "queued": True,
                    "queue_id": item["queue_id"],
                    "submission_id": payload["submission_id"],
                }
            raise

    def flush_queue(self) -> dict[str, Any]:
        """Retry queued submissions and return visible sync state for the TUI."""
        outbox = DurableOutbox()
        result = {"sent": 0, "failed": 0, "remaining": outbox.count(), "errors": []}
        for item in outbox.list():
            if item.get("kind") not in {"submission", "quiz_result"}:
                continue
            try:
                endpoint = "/api/quiz-result" if item.get("kind") == "quiz_result" else "/api/submit"
                self._post_json(endpoint, item["payload"])
            except StudentClientError as exc:
                outbox.mark_failed(item["queue_id"], str(exc))
                result["failed"] += 1
                result["errors"].append(str(exc))
            else:
                outbox.remove(item["queue_id"])
                result["sent"] += 1
        result["remaining"] = outbox.count()
        return result

    def check_assignments(self) -> list[dict[str, Any]]:
        result = self._get_json("/api/assignments")
        assignments = result.get("assignments", [])
        if not isinstance(assignments, list):
            raise StudentClientError("Teacher server returned an invalid assignment catalog")
        return [item for item in assignments if isinstance(item, dict)]

    def check_announcements(self) -> list[dict[str, Any]]:
        result = self._get_json("/api/announcements")
        announcements = result.get("announcements", [])
        if not isinstance(announcements, list):
            raise StudentClientError("Teacher server returned invalid announcements")
        return [item for item in announcements if isinstance(item, dict)]

    def feedback(self) -> list[dict[str, Any]]:
        submissions = self._get_json("/api/feedback").get("submissions", [])
        if not isinstance(submissions, list):
            raise StudentClientError("Teacher server returned invalid feedback")
        return [item for item in submissions if isinstance(item, dict)]

    def mark_announcement_seen(self, announcement_id: str) -> bool:
        result = self._post_json(f"/api/announcements/{announcement_id}/seen", {})
        return bool(result.get("ok"))

    def disconnect(self) -> dict[str, Any]:
        result = self._post_json("/api/disconnect", {})
        self.token = ""
        save_config({"teacher_token": ""})
        return result

    def status(self) -> dict[str, Any]:
        return self._get_json("/api/status")

    def get_llm_status(self) -> dict[str, Any]:
        return self._get_json("/api/llm-status")

    def sync_progress(self, attempts: list[dict]) -> dict[str, Any]:
        """Send unsynced progress attempts to the teacher server via /api/progress.

        Uses the dedicated progress endpoint (keyed by ``lesson_id``, decoupled from
        any pack) rather than overloading ``/api/submit``. This avoids 404s / misattributed
        submissions that occurred when ``lesson_id`` differed from the teacher's ``pack_id``.

        Args:
            attempts: List of {"id", "lesson_id", "passed", "diagnosis", "timestamp"}
        Returns:
            {"sent": int, "failed": int, "errors": list[str], "synced_ids": list[int]}
        """
        result = {"sent": 0, "failed": 0, "errors": [], "synced_ids": []}
        if not attempts:
            return result

        valid_attempts: list[dict] = []
        seen_ids: set[str | int] = set()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            attempt_id = attempt.get("id")
            lesson_id = str(attempt.get("lesson_id", "")).strip()
            if (
                isinstance(attempt_id, bool)
                or not isinstance(attempt_id, (str, int))
                or not str(attempt_id).strip()
                or len(str(attempt_id)) > 200
                or not lesson_id
                or len(lesson_id) > 200
            ):
                continue
            if attempt_id in seen_ids:
                continue
            seen_ids.add(attempt_id)
            valid_attempts.append(attempt)
        result["failed"] = len(attempts) - len(valid_attempts)
        if result["failed"]:
            result["errors"].append("Skipped progress entries without an attempt ID or lesson ID")
        if not valid_attempts:
            return result

        payload = {
            "attempts": [
                {
                    "id": a.get("id"),
                    "lesson_id": a.get("lesson_id", ""),
                    "passed": a.get("passed", False),
                    "diagnosis": a.get("diagnosis", ""),
                    "timestamp": a.get("timestamp", ""),
                }
                for a in valid_attempts
            ]
        }
        try:
            resp = self._post_json("/api/progress", payload)
            accepted_ids = resp.get("accepted_ids")
            if accepted_ids is None:
                stored = int(resp.get("stored", 0))
                accepted_ids = [a.get("id") for a in valid_attempts[:stored]]
            accepted = {
                value
                for value in accepted_ids
                if isinstance(value, (str, int)) and not isinstance(value, bool)
            }
            result["sent"] = len(accepted)
            result["synced_ids"] = [a.get("id") for a in valid_attempts if a.get("id") in accepted]
            rejected = [a.get("id") for a in valid_attempts if a.get("id") not in accepted]
            if rejected:
                result["failed"] += len(rejected)
                result["errors"].append(f"Teacher rejected {len(rejected)} progress attempt(s)")
        except StudentClientError as exc:
            result["failed"] += len(valid_attempts)
            result["errors"].append(str(exc))
        return result

    def submit_quiz_result(
        self,
        quiz_id: str,
        lesson_id: str,
        report: dict[str, Any],
        *,
        submission_id: str | None = None,
        queue_on_failure: bool = False,
    ) -> dict[str, Any]:
        """Submit a graded quiz result, optionally saving it for offline retry."""
        from uuid import uuid4

        payload = {
            "quiz_id": quiz_id,
            "lesson_id": lesson_id,
            "correct": report.get("correct", 0),
            "total": report.get("total", 0),
            "score_pct": report.get("score_pct", 0),
            "passed": report.get("passed", False),
            "taken_at": report.get("taken_at", ""),
            # A stable ID makes a retry safe when the teacher committed the
            # result but the response was lost, including durable outbox retries.
            "submission_id": submission_id or f"quiz-{uuid4().hex}",
        }
        try:
            return self._post_json("/api/quiz-result", payload)
        except StudentClientError:
            if not queue_on_failure:
                raise
            try:
                item = DurableOutbox().add("quiz_result", payload)
            except RuntimeError as exc:
                raise StudentClientError(str(exc)) from exc
            return {
                "ok": False,
                "queued": True,
                "queue_id": item["queue_id"],
                "submission_id": payload["submission_id"],
            }

    def request_hint(self, hint_data: dict[str, Any]) -> str | None:
        try:
            result = self._post_json("/api/hint", hint_data, timeout=120)
            return result.get("hint")
        except StudentClientError as exc:
            logger.info("Hint request failed: %s", exc)
            return None

    @classmethod
    def from_config(cls) -> StudentClient:
        config = load_config()
        return cls(
            teacher_url=config.get("teacher_url", ""),
            token=config.get("teacher_token", ""),
            student_name=config.get("student_name", ""),
            timeout_seconds=config.get("connection_timeout_seconds", config.get("timeout_seconds", 10)),
            strict_pack_validation=True,
        )


def _get_pack_version_store() -> dict[str, int]:
    """Read locally-stored pack versions from config."""
    config = load_config()
    return dict(config.get("pack_versions", {}))


def _set_pack_version(pack_id: str, version: int) -> None:
    """Store a pack's version after successful download."""
    config = load_config()
    versions = dict(config.get("pack_versions", {}))
    versions[pack_id] = version
    save_config({"pack_versions": versions})


def update_packs_from_server(
    client: StudentClient,
    packs: list[dict],
    dest: Path | None = None,
    *,
    quiet: bool = False,
    force: bool = False,
) -> dict:
    """Shared update logic: compare versions, download new/updated packs.

    Returns counts plus ``updated_pack_ids`` so the TUI can refresh an already
    opened assignment after a teacher rebuilds it.
    """
    if dest is None:
        dest = app_dir() / "packs"
    dest.mkdir(parents=True, exist_ok=True)

    result = {
        "downloaded": 0,
        "skipped": 0,
        "updated": 0,
        "errors": [],
        "updated_pack_ids": [],
    }
    versions = _get_pack_version_store()

    for pack in packs:
        pid = str(pack.get("pack_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pid):
            result["errors"].append(f"{pid}: invalid pack ID")
            continue
        try:
            server_version = int(pack.get("version", 0) or 0)
        except (TypeError, ValueError):
            result["errors"].append(f"{pid}: invalid server version")
            continue
        pack_path = dest / f"{pid}.duckypack"
        try:
            local_version = int(versions.get(pid, 0) or 0)
        except (TypeError, ValueError):
            local_version = 0

        server_sha = str(pack.get("sha256") or pack.get("pack_sha256") or "").lower()
        local_sha = ""
        if pack_path.is_file() and server_sha:
            try:
                local_sha = hashlib.sha256(pack_path.read_bytes()).hexdigest()
            except OSError:
                local_sha = ""
        # An unreadable/missing local file has an empty digest and must not be
        # treated as unchanged when the teacher advertises a checksum.
        checksum_changed = bool(server_sha and server_sha != local_sha)

        if pack_path.exists() and not force and server_version <= local_version and not checksum_changed:
            result["skipped"] += 1
            if not quiet:
                print(f"  [exists] {pack.get('title', pid)} v{server_version}")
            continue

        is_update = pack_path.exists()
        try:
            client.download_pack(pid, pack_path)
            # ``StudentClient.download_pack`` retains a compatibility path for
            # old in-memory classroom fixtures that use opaque bytes. The real
            # assignment sync boundary must still reject those bytes, otherwise
            # a fresh student install records a version for a pack that can never
            # become a lesson or expose its quizzes.
            if getattr(client, "strict_pack_validation", False):
                if not zipfile.is_zipfile(pack_path):
                    pack_path.unlink(missing_ok=True)
                    raise StudentClientError("Teacher returned an invalid duckypack archive")
                read_manifest(pack_path)
            _set_pack_version(pid, server_version)
            result["downloaded"] += 1
            if is_update:
                result["updated"] += 1
                result["updated_pack_ids"].append(pid)
            if not quiet:
                tag = "[updated]" if is_update else "[new]"
                print(f"  {tag}    {pack.get('title', pid)} v{server_version}")
        except Exception as exc:
            result["errors"].append(f"{pid}: {exc}")
            if not quiet:
                print(f"  [error]  {pid}: {exc}")

    return result
