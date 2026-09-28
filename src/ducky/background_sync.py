"""Background sync daemon for periodically checking for pack updates."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .config import load_config

logger = logging.getLogger(__name__)


class BackgroundSync:
    """Periodically polls teacher server for pack updates in a daemon thread.

    Usage:
        sync = BackgroundSync(on_update=my_callback)
        sync.start()
        # ... later
        sync.stop()
    """

    def __init__(
        self,
        on_update: Callable[[dict], None] | None = None,
        on_announcement: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._on_update = on_update
        self._on_announcement = on_announcement
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_result: dict | None = None
        self._last_error: dict[str, Any] | None = None
        self._last_check_time: datetime | None = None
        self._last_success_time: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background sync daemon thread."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="ducky-background-sync",
        )
        self._thread.start()

    @staticmethod
    def _interval(value: Any, default: int, *, minimum: int = 5, maximum: int = 86400) -> int:
        """Clamp persisted intervals so bad config cannot create a hot loop."""
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = default
        return max(minimum, min(seconds, maximum))

    def stop(self) -> None:
        """Signal the daemon thread to stop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def _run_loop(self) -> None:
        """Main loop: sleep N seconds, check for updates, repeat."""
        while not self._stop_event.is_set():
            config = load_config()

            if not config.get("auto_update", False):
                try:
                    self._sync_progress()
                    self._flush_submission_queue()
                except Exception as exc:
                    logger.warning("Background offline retry failed: %s", exc)
                self._stop_event.wait(
                    timeout=self._interval(config.get("sync_retry_interval_seconds", 60), 60)
                )
                continue

            interval = self._interval(config.get("update_interval_seconds", 300), 300)

            try:
                self._check_and_update()
            except Exception as exc:
                logger.exception("Background check/update failed")
                with self._lock:
                    self._last_error = {
                        "last_error": f"Check/update failed: {exc}",
                        "timestamp": str(datetime.now()),
                    }

            try:
                self._sync_progress()
            except Exception as exc:
                logger.exception("Background progress sync failed")
                with self._lock:
                    self._last_error = {
                        "last_error": f"Progress sync failed: {exc}",
                        "timestamp": str(datetime.now()),
                    }

            try:
                self._flush_submission_queue()
            except Exception as exc:
                logger.exception("Background submission retry failed")
                with self._lock:
                    self._last_error = {
                        "last_error": f"Submission retry failed: {exc}",
                        "timestamp": str(datetime.now()),
                    }

            try:
                self._check_announcements()
            except Exception as exc:
                logger.exception("Background announcement check failed")
                with self._lock:
                    self._last_error = {
                        "last_error": f"Announcement check failed: {exc}",
                        "timestamp": str(datetime.now()),
                    }

            with self._lock:
                self._last_check_time = datetime.now()

            self._stop_event.wait(timeout=interval)

    def _check_and_update(self) -> dict:
        """Single check: list packs, compare versions, download updates."""
        from .student_client import StudentClient, StudentClientError, update_packs_from_server

        client = StudentClient.from_config()
        if not client.teacher_url:
            return {"skipped": True, "reason": "not connected"}

        try:
            packs = client.list_packs()
        except StudentClientError:
            return {"skipped": True, "reason": "server unreachable"}

        if not packs:
            return {"skipped": True, "reason": "no packs"}

        result = update_packs_from_server(client, packs, quiet=True)

        with self._lock:
            self._last_result = result
            if not result.get("errors") and result.get("reason") not in {"server unreachable"}:
                self._last_error = None

        if result.get("downloaded", 0) > 0 and self._on_update:
            try:
                self._on_update(result)
            except Exception:
                logger.exception("Background sync on_update callback failed")

        with self._lock:
            if result.get("downloaded", 0) > 0:
                self._last_success_time = datetime.now()

        return result

    def _sync_progress(self) -> None:
        """Send unsynced progress to teacher server."""
        config = load_config()
        if not config.get("auto_sync_progress", False):
            return

        from .progress import ProgressTracker
        from .student_client import StudentClient

        client = StudentClient.from_config()
        if not client.teacher_url:
            return

        tracker = ProgressTracker()
        try:
            unsynced = tracker.get_unsynced_attempts()
            if not unsynced:
                return
            result = client.sync_progress(unsynced)
            if result.get("synced_ids"):
                tracker.mark_attempt_synced(result["synced_ids"])
        except Exception as exc:
            logger.warning("Progress sync failed for batch: %s", exc)
        finally:
            tracker.close()

    def _flush_submission_queue(self) -> dict[str, Any]:
        """Retry submissions that were queued while the teacher was offline."""
        from .student_client import StudentClient

        client = StudentClient.from_config()
        if not client.teacher_url or not client.token:
            return {"sent": 0, "failed": 0, "remaining": 0, "errors": []}
        result = client.flush_queue()
        with self._lock:
            if result.get("sent"):
                self._last_success_time = datetime.now()
            self._last_result = {**(self._last_result or {}), "submission_queue": result}
        return result

    def _check_announcements(self) -> None:
        """Fetch announcements and surface any not seen before via the callback.

        Deduplicates by announcement id persisted in config (``seen_announcement_ids``)
        so the same announcement is only surfaced once, even across restarts.
        """
        from .config import load_config, save_config
        from .student_client import StudentClient, StudentClientError

        client = StudentClient.from_config()
        if not client.teacher_url:
            return

        try:
            announcements = client.check_announcements()
        except StudentClientError:
            return
        if not announcements:
            return

        config = load_config()
        seen = set(config.get("seen_announcement_ids", []))
        fresh = [a for a in announcements if a.get("announcement_id") not in seen]
        if not fresh:
            return

        # Persist the full current id set (bounded to the server's list) to dedupe.
        new_seen = sorted({a.get("announcement_id") for a in announcements if a.get("announcement_id")})
        save_config({"seen_announcement_ids": new_seen})

        if self._on_announcement:
            try:
                self._on_announcement(fresh)
            except Exception:
                logger.exception("Background sync on_announcement callback failed")

    def get_last_result(self) -> dict | None:
        with self._lock:
            return self._last_result

    def get_last_error(self) -> dict[str, Any] | None:
        """Return the last error info (``{"last_error": str, "timestamp": str}``) or ``None``."""
        with self._lock:
            return self._last_error

    def get_status(self) -> dict[str, Any]:
        """Return a summary of sync state for display in UI."""
        with self._lock:
            return {
                "running": self.is_running,
                "last_check": self._last_check_time.isoformat() if self._last_check_time else None,
                "last_success": self._last_success_time.isoformat() if self._last_success_time else None,
                "last_error": self._last_error["last_error"] if self._last_error else None,
            }
