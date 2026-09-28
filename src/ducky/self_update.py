"""Student-safe self-update using the public R2 student release manifest."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import GITHUB_OWNER, GITHUB_REPO, __build_type__, __version__

# Keep the updater usable for installations from before release fingerprints
# were introduced. Without this fallback, an older installed GUI would fail
# during import and never reach the Cloudflare check that upgrades it.
try:
    from . import __build_id__
except ImportError:
    __build_id__ = "legacy"

logger = logging.getLogger(__name__)

# ── Update sources (checked in order) ────────────────────────────────

# Primary: GitHub releases API
GITHUB_API_RELEASES = "https://api.github.com/repos/{owner}/{repo}/releases/latest"

# Fallback: R2 with latest.json
R2_BASE_URL = "https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev"
R2_LATEST_JSON = f"{R2_BASE_URL}/latest.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_UPDATE_BYTES = 100 * 1024 * 1024

# Default repo — set in __init__.py by the distributor
DEFAULT_GITHUB_OWNER = GITHUB_OWNER
DEFAULT_GITHUB_REPO = GITHUB_REPO


def _version_tuple(version_str: str) -> tuple[int, ...]:
    """Convert '0.4.0' to (0, 4, 0) for comparison.

    Handles suffixed versions like '0.5.1s' and '99.99.99-test-live'
    by stripping non-numeric trailing characters from each part.
    """
    import re
    parts = version_str.strip().lstrip("v").split(".")
    numeric_parts = []
    for p in parts[:3]:
        # Strip trailing non-numeric characters, e.g. "1s" -> "1", "99-test-live" -> "99"
        m = re.match(r"(\d+)", p)
        if m:
            numeric_parts.append(int(m.group(1)))
        else:
            numeric_parts.append(0)
    return tuple(numeric_parts)


def _get_github_repo() -> tuple[str, str]:
    """Get (owner, repo) from config or defaults."""
    from .config import load_config

    config = load_config()
    owner = config.get("github_owner", "") or DEFAULT_GITHUB_OWNER
    repo = config.get("github_repo", "") or DEFAULT_GITHUB_REPO
    return owner, repo


def _check_github_release(owner: str, repo: str) -> dict[str, Any] | None:
    """Query GitHub releases API for the latest version.

    Returns None on failure, or a dict with version, download_url, release_notes.
    """
    url = GITHUB_API_RELEASES.format(owner=owner, repo=repo)
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ducky-selfupdate"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    tag = data.get("tag_name", "")
    version = tag.lstrip("v")

    if not version:
        return None

    # Find the .whl or source archive
    download_url = data.get("zipball_url", "")
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".whl") or name.endswith(".tar.gz"):
            download_url = asset.get("browser_download_url", download_url)
            break

    return {
        "version": version,
        "download_url": download_url,
        "release_notes": data.get("body", "")[:500] if data.get("body") else "",
        "sha256": "",
    }


def _check_r2_latest(*, strict: bool = False) -> dict[str, Any] | None:
    """Fallback: check R2 for latest.json.

    Student builds use strict validation because their public channel must
    contain a student-only, immutable, checksum-verifiable archive. The full
    build keeps the older permissive fallback contract for compatibility with
    existing development mirrors.
    """
    try:
        req = urllib.request.Request(
            R2_LATEST_JSON,
            headers={"Accept": "application/json", "User-Agent": "ducky-selfupdate"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            content_length = ""
            headers = getattr(response, "headers", None)
            if headers is not None and hasattr(headers, "get"):
                content_length = headers.get("Content-Length", "") or ""
            if content_length and int(content_length) > MAX_MANIFEST_BYTES:
                return None
            raw = response.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                return None
            data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None

    download_url = data.get("download_url", "") or data.get("zip_url", "")
    if strict and (
        data.get("build_type") != "student"
        or not isinstance(download_url, str)
        or not download_url.startswith(R2_BASE_URL.rstrip("/") + "/")
        or "/releases/" not in download_url
        or not download_url.endswith(".zip")
        or download_url.endswith("/student-latest.zip")
        or not re.fullmatch(r"[0-9a-fA-F]{64}", str(data.get("sha256", "")))
    ):
        return None

    return {
        "version": data.get("version", __version__),
        "build_id": data.get("build_id", ""),
        "download_url": download_url,
        "release_notes": data.get("release_notes", ""),
        "sha256": data.get("sha256", ""),
    }


def check_for_update(*, force: bool = False) -> dict[str, Any]:
    """Check GitHub (primary) then R2 (fallback) for updates.

    When ``force=True``, delegates to :func:`force_check_update` which
    always queries GitHub live and never falls back to R2.

    Returns:
        {
            "update_available": bool,
            "current_version": str,
            "latest_version": str,
            "download_url": str,
            "release_notes": str,
            "sha256": str,
            "error": str | None,
        }
    """
    if force:
        return force_check_update()

    result = {
        "update_available": False,
        "current_version": __version__,
        "latest_version": __version__,
        "current_build_id": __build_id__,
        "latest_build_id": "",
        "download_url": "",
        "release_notes": "",
        "sha256": "",
        "error": None,
    }

    # Student artifacts have their own release channel.  Do not let a student
    # install a teacher/source build simply because GitHub has a newer tag.
    if __build_type__ == "student":
        info = _check_r2_latest(strict=True)
        if info is None:
            result["error"] = "Could not reach the student release server"
            return result
        result["latest_version"] = info.get("version", __version__)
        result["latest_build_id"] = info.get("build_id", "")
        result["download_url"] = info.get("download_url", "")
        result["release_notes"] = info.get("release_notes", "")
        result["sha256"] = info.get("sha256", "")
        try:
            local = _version_tuple(__version__)
            remote = _version_tuple(result["latest_version"])
            version_newer = remote > local
            # A rebuilt artifact with the SAME version can carry fixes; surface
            # it as an update only when the version is not older than installed.
            # Without the equality guard, a stale/rolled-back latest.json with a
            # different build fingerprint would offer students a DOWNgrade.
            same_or_newer = version_newer or remote == local
            build_newer = bool(
                same_or_newer
                and result["latest_build_id"]
                and result["latest_build_id"] != __build_id__
            )
            result["update_available"] = bool(
                result["download_url"] and (version_newer or build_newer)
            )
        except (ValueError, TypeError):
            result["update_available"] = False
        return result

    owner, repo = _get_github_repo()

    # Version authority: GitHub releases (primary) → R2 latest.json (fallback).
    info = None
    if owner and repo:
        info = _check_github_release(owner, repo)
    if info is None:
        info = _check_r2_latest()
    if info is None:
        result["error"] = "Could not reach any update server"
        return result

    # Download URL must come from R2 (the canonical distribution), NEVER the
    # GitHub asset URL. A GitHub release's .whl/zipball points at a layout
    # download_and_install() cannot consume (no src/ducky / .tar.gz), which made
    # every auto-update silently fail. latest.json carries the installable
    # ducky_student-v<v>.zip URL (see CLAUDE.md distribution contract).
    r2 = _check_r2_latest()
    download_url = (r2 or {}).get("download_url", "") if r2 else ""
    sha256 = (r2 or {}).get("sha256", "") if r2 else ""

    result["latest_version"] = info["version"]
    result["download_url"] = download_url
    result["release_notes"] = info.get("release_notes", "") or (r2 or {}).get("release_notes", "")
    result["sha256"] = sha256

    try:
        local = _version_tuple(__version__)
        remote = _version_tuple(result["latest_version"])
        version_newer = remote > local
    except (ValueError, TypeError):
        version_newer = False

    # Only surface an update when there is an installable download. The download
    # URL comes exclusively from R2; if it's missing (R2 unreachable) we cannot
    # actually perform the update, so we must not claim one is available.
    result["update_available"] = bool(version_newer and result["download_url"])

    return result


def force_check_update(
    timeout: int = 10,
    *,
    github_owner: str = "",
    github_repo: str = "",
) -> dict[str, Any]:
    """Force a live GitHub releases API check with explicit proof fields.

    Always hits the live GitHub API — no caching, no lock files, no R2
    fallback.  Use this when you need to *prove* the packaged application
    reaches the live version service.

    Proof fields returned in every response:
    - ``source``       — ``"force-live-github"`` on success / ``"error"``
    - ``used_cache``   — ``False`` (the invariant this function guarantees)
    - ``version_source`` — ``"live-checked"`` on success / ``"error"``
    - ``checked_at``   — ISO-8601 UTC timestamp of the live HTTP call

    Args:
        timeout: HTTP timeout in seconds.
        github_owner: Override owner (else loaded from config / defaults).
        github_repo: Override repo (else loaded from config / defaults).

    Returns dict with keys listed above plus the standard
    ``update_available``, ``current_version``, ``latest_version``,
    ``download_url``, ``release_notes``, ``error``.
    """
    from datetime import datetime, timezone

    result: dict[str, Any] = {
        "source": "force-live-github",
        "used_cache": False,
        "version_source": "live-checked",
        "current_version": __version__,
        "latest_version": "",
        "update_available": False,
        "download_url": "",
        "release_notes": "",
        "sha256": "",
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    owner = github_owner or ""
    repo = github_repo or ""
    if not owner or not repo:
        owner, repo = _get_github_repo()

    if not owner or not repo:
        result["source"] = "error"
        result["version_source"] = "error"
        result["error"] = "No GitHub repo configured"
        return result

    # Allow config-level override of the API base URL (used by integration
    # tests to point at a fake/local server).
    from .config import load_config

    config = load_config()
    api_base = (config.get("github_api_override_base", "") or "").rstrip("/")

    if api_base:
        url = f"{api_base}/repos/{owner}/{repo}/releases/latest"
    else:
        url = GITHUB_API_RELEASES.format(owner=owner, repo=repo)

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ducky-force-check"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        result["source"] = "error"
        result["version_source"] = "error"
        result["error"] = str(exc)
        return result

    tag = data.get("tag_name", "")
    version = tag.lstrip("v")
    if not version:
        result["source"] = "error"
        result["version_source"] = "error"
        result["error"] = "No version found in GitHub release data"
        return result

    download_url = data.get("zipball_url", "")
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".whl") or name.endswith(".tar.gz"):
            download_url = asset.get("browser_download_url", download_url)
            break

    result["latest_version"] = version
    result["download_url"] = download_url
    result["release_notes"] = (data.get("body", "") or "")[:500]

    try:
        local = _version_tuple(__version__)
        remote = _version_tuple(version)
        result["update_available"] = remote > local
    except (ValueError, TypeError):
        result["update_available"] = False

    return result


def download_and_install(download_url: str, expected_sha256: str = "") -> dict[str, Any]:
    """Download ZIP from R2 to a temp directory, verify, and install.

    Returns:
        {"ok": bool, "message": str, "version": str, "error": str | None}
    """
    result = {"ok": False, "message": "", "version": "", "error": None}

    try:
        with tempfile.TemporaryDirectory(prefix="ducky-update-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_path = tmp_path / "update.zip"

            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": "ducky-selfupdate/0.1"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                content_length = ""
                headers = getattr(response, "headers", None)
                if headers is not None and hasattr(headers, "get"):
                    content_length = headers.get("Content-Length", "") or ""
                try:
                    if content_length and int(content_length) > MAX_UPDATE_BYTES:
                        result["error"] = "Student update is larger than the safe download limit"
                        return result
                except (TypeError, ValueError):
                    pass
                total = 0
                with zip_path.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UPDATE_BYTES:
                            result["error"] = "Student update is larger than the safe download limit"
                            return result
                        output.write(chunk)

            if expected_sha256:
                actual_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
                if actual_sha != expected_sha256:
                    result["error"] = f"SHA256 mismatch: expected {expected_sha256[:16]}..., got {actual_sha[:16]}..."
                    return result

            with zipfile.ZipFile(zip_path, "r") as zf:
                extract_dir = tmp_path / "extracted"
                for member in zf.infolist():
                    name = member.filename.replace("\\", "/")
                    target = (extract_dir / name).resolve()
                    mode = (member.external_attr >> 16) & 0o170000
                    if name.startswith("/") or ".." in Path(name).parts:
                        result["error"] = f"Unsafe path in student update: {member.filename}"
                        return result
                    if mode == 0o120000:
                        result["error"] = f"Symlinks are not allowed in student update: {member.filename}"
                        return result
                    try:
                        target.relative_to(extract_dir.resolve())
                    except ValueError:
                        result["error"] = f"Unsafe path in student update: {member.filename}"
                        return result
                zf.extractall(extract_dir)

            ducky_pkg = None
            for candidate in [
                extract_dir / "src" / "ducky",
                *extract_dir.glob("*/src/ducky"),
                *extract_dir.glob("*/ducky"),
            ]:
                if candidate.is_dir() and (candidate / "__init__.py").exists():
                    ducky_pkg = candidate
                    break

            if ducky_pkg is None:
                result["error"] = "Could not find ducky package in downloaded ZIP"
                return result

            init_text = (ducky_pkg / "__init__.py").read_text(encoding="utf-8")
            for line in init_text.splitlines():
                if line.startswith("__version__"):
                    result["version"] = line.split("=")[1].strip().strip('"').strip("'")
                    break

            current_pkg = Path(__file__).parent  # src/ducky/
            backup_path = current_pkg.parent / "ducky.bak"

            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(current_pkg, backup_path)

            shutil.rmtree(current_pkg)
            try:
                shutil.copytree(ducky_pkg, current_pkg)
            except Exception:
                # Auto-rollback: restore from backup
                if backup_path.exists():
                    shutil.copytree(backup_path, current_pkg)
                    shutil.rmtree(backup_path)
                raise

            result["ok"] = True
            result["message"] = f"Updated to v{result['version']}. Restart ducky to use the new version."

    except Exception as exc:
        result["error"] = f"Update failed: {exc}"
        return result

    return result


def rollback_update() -> dict[str, Any]:
    """Restore from backup if update went wrong."""
    backup_path = Path(__file__).parent.parent / "ducky.bak"
    current_pkg = Path(__file__).parent

    if not backup_path.exists():
        return {"ok": False, "error": "No backup found"}

    try:
        if current_pkg.exists():
            shutil.rmtree(current_pkg)
        shutil.copytree(backup_path, current_pkg)
        shutil.rmtree(backup_path)
        return {"ok": True, "message": "Rolled back to previous version"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _get_marker_path() -> Path:
    """Get the path for the pending update marker file."""
    return Path(__file__).parent.parent.parent / ".ducky-update-pending.json"


def _get_lock_path() -> Path:
    """Get the path for the update lock file."""
    return Path(__file__).parent.parent.parent / ".ducky-update.lock"


def queue_pending_update(result: dict[str, Any]) -> bool:
    """Persist a verified update offer for the next application launch.

    Student startup checks use this synchronous helper so the Cloudflare
    manifest is fetched before the TUI opens. The running process is not
    replaced in place; ``apply_pending_update`` handles that safely on the
    next launch.
    """
    if result.get("error") or not result.get("update_available") or not result.get("download_url"):
        return False
    marker_path = _get_marker_path()
    marker_data = {
        "download_url": result["download_url"],
        "sha256": result.get("sha256", ""),
        "version": result.get("latest_version", ""),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    temp_path = marker_path.with_name(f".{marker_path.name}.tmp")
    try:
        temp_path.write_text(json.dumps(marker_data), encoding="utf-8")
        os.replace(temp_path, marker_path)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def auto_update_background(*, force: bool = False, on_result=None) -> None:
    """Check for updates in background and download if available.

    Does NOT block the caller. The update takes effect on next launch.
    Only downloads if auto_update_app config is True.
    Uses a lock file to prevent concurrent downloads.

    Args:
        force: If True, uses :func:`force_check_update` (live GitHub).
    """
    from .config import load_config

    config = load_config()
    if not config.get("auto_update_app", False):
        if on_result:
            on_result({"disabled": True, "current_version": __version__})
        return

    lock_path = _get_lock_path()

    # Prevent concurrent update checks
    if lock_path.exists():
        try:
            import time

            lock_age = time.time() - lock_path.stat().st_mtime
            if lock_age < 300:  # 5 minutes
                if on_result:
                    on_result({"busy": True, "current_version": __version__})
                return
        except OSError:
            pass

    try:
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        if on_result:
            on_result({"error": "Could not create update lock", "current_version": __version__})
        return

    try:
        result = check_for_update(force=force) if force else check_for_update()
        if on_result:
            on_result(dict(result))
        if result.get("error") or not result.get("update_available"):
            return

        # Write marker file so main() can apply it on next launch.
        queue_pending_update(result)
    except Exception as exc:
        logger.warning("Background auto-update check failed: %s", exc)
        if on_result:
            on_result({"error": str(exc), "current_version": __version__})
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Failed to remove update lock file: %s", exc)


def apply_pending_update() -> dict[str, Any] | None:
    """Apply any pending update that was downloaded in background.

    Called at CLI startup. Returns the result dict if an update was applied, None otherwise.
    """
    from .config import load_config

    config = load_config()
    if not config.get("auto_update_app", False):
        return None

    marker_path = _get_marker_path()
    if not marker_path.is_file():
        return None

    try:
        marker_data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        try:
            marker_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    # Don't apply if older than 24 hours
    try:
        checked = datetime.fromisoformat(str(marker_data["checked_at"]))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - checked
        if age.total_seconds() > 86400 or age.total_seconds() < -300:
            marker_path.unlink(missing_ok=True)
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        # A hand-edited or partially written marker is not trustworthy. Remove
        # it instead of applying an update with an unknown age.
        marker_path.unlink(missing_ok=True)
        return None

    # Apply the update
    download_url = marker_data.get("download_url", "")
    sha256_val = marker_data.get("sha256", "")

    if not download_url:
        try:
            marker_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    result = download_and_install(download_url, sha256_val)
    try:
        marker_path.unlink(missing_ok=True)
    except OSError:
        pass
    return result


def startup_student_update() -> dict[str, Any]:
    """Synchronously verify and apply the student release before opening UI.

    The old student launch path only queued a marker after checking R2. That
    meant the student saw the old application during the current launch, and
    the marker could be lost when the app was installed in a read-only or
    short-lived environment. A student desktop launch now uses this bounded
    path: apply an existing verified marker, fetch Cloudflare ``latest.json``,
    download the immutable checksum-verified archive immediately, and report
    that the process must restart before showing the window.

    A failed network check never blocks local learning. Failed downloads are
    retained as a pending marker so the next launch can retry.
    """
    from .config import load_config

    result: dict[str, Any] = {
        "checked": False,
        "updated": False,
        "restart_required": False,
        "version": "",
        "check": {},
        "error": None,
    }
    config = load_config()
    if not config.get("auto_update_app", True):
        result["disabled"] = True
        result["check"] = {"disabled": True, "current_version": __version__}
        return result

    # A prior background check may already have downloaded a verified update.
    # Apply it first and restart; do not immediately download the same build a
    # second time using the still-imported old module metadata.
    try:
        applied = apply_pending_update()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not apply pending student update: %s", exc)
        applied = {"ok": False, "error": str(exc)}
    if applied and applied.get("ok"):
        result.update(
            {
                "updated": True,
                "restart_required": True,
                "version": applied.get("version", ""),
                "message": applied.get("message", "Student update applied"),
            }
        )
        return result

    try:
        check = check_for_update()
    except Exception as exc:  # noqa: BLE001
        check = {
            "update_available": False,
            "current_version": __version__,
            "latest_version": __version__,
            "error": str(exc),
        }
    result["checked"] = True
    result["check"] = check
    if check.get("error") or not check.get("update_available"):
        return result

    lock_path = _get_lock_path()
    try:
        if lock_path.exists():
            result["error"] = "Another Ducky update is already running"
            return result
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        result["error"] = f"Could not start student update: {exc}"
        return result

    try:
        installed = download_and_install(check.get("download_url", ""), check.get("sha256", ""))
        if installed.get("ok"):
            result.update(
                {
                    "updated": True,
                    "restart_required": True,
                    "version": installed.get("version") or check.get("latest_version", ""),
                    "message": installed.get("message", "Student update applied"),
                }
            )
        else:
            result["error"] = installed.get("error", "Student update could not be installed")
            queue_pending_update(check)
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        queue_pending_update(check)
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    return result
