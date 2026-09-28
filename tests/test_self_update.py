import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import ducky.self_update as self_update
from ducky.self_update import (
    _version_tuple,
    auto_update_background,
    check_for_update,
    download_and_install,
    force_check_update,
    queue_pending_update,
    rollback_update,
)


class SelfUpdateTests(unittest.TestCase):
    def test_queue_pending_update_writes_a_replaceable_marker(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            self._assert_pending_update_marker(Path(temp_dir))

    @staticmethod
    def _assert_pending_update_marker(tmp_path):
        marker = tmp_path / ".ducky-update-pending.json"
        result = {
            "update_available": True,
            "download_url": "https://pub.example/releases/v1/build/app.zip",
            "sha256": "a" * 64,
            "latest_version": "1.0.0",
        }
        with patch("ducky.self_update._get_marker_path", return_value=marker):
            assert queue_pending_update(result)
        saved = json.loads(marker.read_text(encoding="utf-8"))
        assert saved["download_url"] == result["download_url"]
        assert saved["sha256"] == result["sha256"]

    def test_auto_update_background_reports_live_result(self):
        seen = []
        with (
            patch("ducky.config.load_config", return_value={"auto_update_app": True}),
            patch("ducky.self_update.check_for_update", return_value={
                "update_available": False,
                "current_version": "0.5.1",
                "latest_version": "0.5.1",
                "download_url": "",
                "error": None,
            }),
            patch("ducky.self_update._get_lock_path") as lock_path,
        ):
            lock_path.return_value.exists.return_value = False
            auto_update_background(on_result=seen.append)
        self.assertEqual(seen[0]["latest_version"], "0.5.1")
    def test_check_for_update_returns_expected_keys(self):
        result = check_for_update()
        self.assertIn("update_available", result)
        self.assertIn("current_version", result)
        self.assertIn("latest_version", result)
        self.assertIn("download_url", result)
        self.assertIn("release_notes", result)
        self.assertIn("sha256", result)
        self.assertIn("error", result)

    def test_check_for_update_handles_network_error(self):
        with patch("ducky.self_update.urllib.request.urlopen", side_effect=Exception("network error")):
            result = check_for_update()
            self.assertIsNotNone(result.get("error"))
            self.assertFalse(result["update_available"])

    def test_check_for_update_handles_timeout(self):
        with patch("ducky.self_update.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = check_for_update()
            self.assertIsNotNone(result.get("error"))

    def test_version_tuple_basic(self):
        self.assertEqual(_version_tuple("0.4.0"), (0, 4, 0))

    def test_version_tuple_with_v_prefix(self):
        self.assertEqual(_version_tuple("v1.2.3"), (1, 2, 3))

    def test_version_tuple_with_partial(self):
        self.assertEqual(_version_tuple("1.2.3"), (1, 2, 3))

    def test_check_for_update_compares_versions(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"version": "99.0.0", "download_url": "http://example.com/update.zip", "release_notes": "test", "sha256": "abc123"}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("ducky.self_update.urllib.request.urlopen", return_value=mock_response):
            result = check_for_update()
            self.assertTrue(result["update_available"])
            self.assertEqual(result["latest_version"], "99.0.0")

    def test_check_for_update_same_version(self):
        from ducky import __version__

        mock_response = MagicMock()
        mock_response.read.return_value = ('{"version": "' + __version__ + '"}').encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("ducky.self_update.urllib.request.urlopen", return_value=mock_response):
            result = check_for_update()
            self.assertFalse(result["update_available"])

    def test_download_and_install_network_error(self):
        with patch("ducky.self_update.urllib.request.urlopen", side_effect=Exception("network error")):
            result = download_and_install("http://example.com/update.zip")
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result.get("error"))

    def test_download_and_install_sha256_mismatch(self):

        fake_zip_data = b"fake zip content"
        with patch("ducky.self_update.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.side_effect = [fake_zip_data, b""]
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response
            result = download_and_install("http://example.com/update.zip", expected_sha256="wrong_hash")
            self.assertFalse(result["ok"])
            self.assertIn("SHA256 mismatch", result["error"])

    def test_rollback_update_no_backup(self):
        with patch("ducky.self_update.Path") as MockPath:
            mock_backup = MagicMock()
            mock_backup.exists.return_value = False
            MockPath.return_value.parent.parent.__truediv__ = MagicMock(return_value=mock_backup)
            result = rollback_update()
            self.assertFalse(result["ok"])

    def test_module_has_expected_functions(self):
        import ducky.self_update as m

        self.assertTrue(callable(m.check_for_update))
        self.assertTrue(callable(m.download_and_install))
        self.assertTrue(callable(m.rollback_update))
        self.assertTrue(callable(m._version_tuple))


class StudentReleaseChannelTests(unittest.TestCase):
    def test_startup_student_update_applies_cloudflare_release_before_ui(self):
        with TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / ".ducky-update.lock"
            checked = {
                "update_available": True,
                "latest_version": "0.5.2",
                "download_url": "https://pub.example/releases/student.zip",
                "sha256": "a" * 64,
                "error": None,
            }
            installed = {"ok": True, "version": "0.5.2s", "message": "updated"}
            with (
                patch("ducky.config.load_config", return_value={"auto_update_app": True}),
                patch.object(self_update, "apply_pending_update", return_value=None),
                patch.object(self_update, "check_for_update", return_value=checked) as check,
                patch.object(self_update, "download_and_install", return_value=installed) as install,
                patch.object(self_update, "_get_lock_path", return_value=lock_path),
                patch.object(self_update, "queue_pending_update") as queue,
            ):
                result = self_update.startup_student_update()

            check.assert_called_once_with()
            install.assert_called_once_with(checked["download_url"], checked["sha256"])
            queue.assert_not_called()
            self.assertTrue(result["updated"])
            self.assertTrue(result["restart_required"])

    def test_startup_student_update_is_offline_safe(self):
        with patch("ducky.config.load_config", return_value={"auto_update_app": True}), patch.object(
            self_update, "apply_pending_update", return_value=None
        ), patch.object(
            self_update, "check_for_update", return_value={"error": "offline", "update_available": False}
        ):
            result = self_update.startup_student_update()

        self.assertFalse(result["updated"])
        self.assertEqual(result["check"]["error"], "offline")

    def test_student_build_uses_r2_manifest_not_github(self):
        r2 = {
            "version": "99.0.0",
            "download_url": "https://pub.example/student-latest.zip",
            "release_notes": "student release",
            "sha256": "abc123",
        }
        with (
            patch.object(self_update, "__build_type__", "student"),
            patch.object(self_update, "_check_r2_latest", return_value=r2),
            patch.object(self_update, "_check_github_release") as github_check,
        ):
            result = self_update.check_for_update()

        github_check.assert_not_called()
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "99.0.0")
        self.assertEqual(result["download_url"], r2["download_url"])

    def test_student_build_detects_same_version_cloudflare_rebuild(self):
        """A changed R2 artifact must not be hidden by an unchanged version."""
        r2 = {
            "version": "0.5.1",
            "build_id": "new-student-build",
            "download_url": "https://pub.example/student-latest.zip",
            "release_notes": "student rebuild",
            "sha256": "abc123",
        }
        with (
            patch.object(self_update, "__build_type__", "student"),
            patch.object(self_update, "__version__", "0.5.1"),
            patch.object(self_update, "__build_id__", "old-student-build"),
            patch.object(self_update, "_check_r2_latest", return_value=r2),
        ):
            result = self_update.check_for_update()

        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "0.5.1")
        self.assertEqual(result["latest_build_id"], "new-student-build")

    def test_legacy_student_updater_uses_compatible_build_id(self):
        """Missing __build_id__ must not prevent an old install upgrading."""
        r2 = {
            "version": "0.5.2",
            "build_id": "new-student-build",
            "download_url": "https://pub.example/student-latest.zip",
            "release_notes": "student update",
            "sha256": "abc123",
        }
        with (
            patch.object(self_update, "__build_type__", "student"),
            patch.object(self_update, "__version__", "0.5.1s"),
            patch.object(self_update, "__build_id__", "legacy"),
            patch.object(self_update, "_check_r2_latest", return_value=r2),
        ):
            result = self_update.check_for_update()

        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "0.5.2")

    def test_student_build_never_offers_downgrade_on_older_manifest(self):
        """Regression: a stale/rolled-back latest.json with an OLDER version
        but a different build fingerprint must NOT be offered as an update.

        The old logic let ``build_newer`` (different build_id) alone trigger an
        update regardless of version, so students could be downgraded from, say,
        0.5.2 to a re-served 0.5.1 build. ``build_newer`` must only apply when
        the version is the same or newer than the installed one."""
        r2 = {
            "version": "0.5.1",
            "build_id": "stale-older-build",
            "download_url": "https://pub.example/student-latest.zip",
            "release_notes": "rolled back release",
            "sha256": "abc123",
        }
        with (
            patch.object(self_update, "__build_type__", "student"),
            patch.object(self_update, "__version__", "0.5.2"),
            patch.object(self_update, "__build_id__", "current-build-0.5.2"),
            patch.object(self_update, "_check_r2_latest", return_value=r2),
        ):
            result = self_update.check_for_update()

        self.assertFalse(
            result["update_available"],
            "older version with a different build id must not be offered",
        )
        self.assertEqual(result["latest_version"], "0.5.1")


class ForceCheckUpdateTests(unittest.TestCase):
    """Tests for :func:`ducky.self_update.force_check_update`."""

    def test_force_check_returns_proof_fields(self):
        """force_check_update should include proof fields on success."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"tag_name": "v99.99.99", "assets": [], "body": "test"}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("ducky.self_update.urllib.request.urlopen", return_value=mock_response):
            result = force_check_update(github_owner="test", github_repo="test")

        self.assertEqual(result.get("source"), "force-live-github")
        self.assertFalse(result.get("used_cache", True))
        self.assertEqual(result.get("version_source"), "live-checked")
        self.assertIn("checked_at", result)
        self.assertEqual(result.get("latest_version"), "99.99.99")

    def test_force_check_error_returns_proof_fields(self):
        """force_check_update should include proof fields even on error."""
        with patch("ducky.self_update.urllib.request.urlopen", side_effect=Exception("network error")):
            result = force_check_update(github_owner="test", github_repo="test")

        self.assertEqual(result.get("source"), "error")
        self.assertFalse(result.get("used_cache", True))
        self.assertEqual(result.get("version_source"), "error")
        self.assertIsNotNone(result.get("error"))
        self.assertIn("checked_at", result)

    def test_force_check_never_uses_cache(self):
        """used_cache should always be False, never fallback."""
        with patch("ducky.self_update.urllib.request.urlopen", side_effect=Exception("fail")):
            result = force_check_update(github_owner="x", github_repo="y")

        self.assertFalse(result["used_cache"])
        self.assertEqual(result["source"], "error")

    def test_force_check_no_repo_returns_error(self):
        """Without owner/repo (and no configured default), force_check_update
        should fail gracefully rather than hitting the live GitHub API."""
        with patch("ducky.self_update._get_github_repo", return_value=("", "")):
            result = force_check_update(github_owner="", github_repo="")
        self.assertEqual(result["source"], "error")
        self.assertIsNotNone(result["error"])

    def test_force_check_sentinel_version(self):
        """force_check_update should return the exact sentinel version from GitHub."""
        sentinel = "v99.99.99-test-live"
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"tag_name": "' + sentinel.encode() + b'", "assets": [], "body": "Integration test"}'
        )
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("ducky.self_update.urllib.request.urlopen", return_value=mock_response):
            result = force_check_update(github_owner="test", github_repo="test")

        self.assertEqual(result["latest_version"], "99.99.99-test-live")
        self.assertEqual(result["source"], "force-live-github")
        self.assertFalse(result["used_cache"])
        self.assertEqual(result["version_source"], "live-checked")

    def test_check_for_update_force_true_dispatches_to_force_check(self):
        """check_for_update(force=True) should route to force_check_update."""
        with patch("ducky.self_update.force_check_update") as mock_force:
            mock_force.return_value = {"source": "force-live-github", "used_cache": False, "latest_version": "99.99.99"}
            from ducky.self_update import check_for_update
            result = check_for_update(force=True)
            mock_force.assert_called_once()
            # The mock return value should be returned (or merged into default)
            if "source" in result:
                self.assertEqual(result["source"], "force-live-github")

    def test_github_api_override_config(self):
        """force_check_update should use github_api_override_base when configured."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"tag_name": "v99.0.0", "assets": [], "body": ""}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("ducky.self_update.urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
            patch("ducky.config.load_config", return_value={
                "github_api_override_base": "http://127.0.0.1:9999",
            }),
        ):
            result = force_check_update(github_owner="test", github_repo="test")

            # Verify it used the override base URL
            call_url = mock_urlopen.call_args[0][0].full_url
            self.assertIn("127.0.0.1:9999", call_url)
            self.assertNotIn("api.github.com", call_url)

            self.assertTrue(result["latest_version"], "99.0.0")


class DownloadSourceContractTests(unittest.TestCase):
    """Regression guards for the R2-vs-GitHub update-source contract.

    GitHub is the *version authority*; R2's latest.json is the *download source*.
    A GitHub release's .whl/zipball asset is NOT installable by download_and_install
    (no src/ducky layout / .tar.gz), so it must never be used as the download_url,
    and an update must never be reported without an installable R2 URL.
    """

    def _github_only_response(self, version, asset_name="ducky-1.2.3.whl"):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"tag_name": "v' + version.encode() + b'", "assets": '
            b'[{"name": "' + asset_name.encode() + b'",'
            b' "browser_download_url": "https://github.com/x/y/releases/download/v'
            + version.encode() + b'/'
            + asset_name.encode() + b'"}], "body": "notes"}'
        )
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response

    def _make_r2_latest(self, version, url):
        return {"version": version, "download_url": url, "release_notes": "r2 notes", "sha256": "abc"}

    def test_download_url_comes_from_r2_not_github_whl(self):
        """Even when GitHub is newer and offers a .whl, download_url must be the R2 zip."""
        r2 = self._make_r2_latest("0.5.1", "https://r2/ducky_student-v0.5.1.zip")
        with patch("ducky.self_update.urllib.request.urlopen", return_value=self._github_only_response("99.0.0")):
            with patch("ducky.self_update._check_r2_latest", return_value=r2):
                result = check_for_update()
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "99.0.0")  # version authority = GitHub
        self.assertEqual(result["download_url"], "https://r2/ducky_student-v0.5.1.zip")  # source = R2
        self.assertNotIn(".whl", result["download_url"])
        self.assertNotIn("github.com", result["download_url"])

    def test_no_installable_url_means_no_update(self):
        """If R2 provides no download_url, an update must never be reported."""
        with patch("ducky.self_update.urllib.request.urlopen", return_value=self._github_only_response("99.0.0")):
            with patch("ducky.self_update._check_r2_latest", return_value=None):
                result = check_for_update()
        self.assertFalse(result["update_available"])
        self.assertEqual(result["download_url"], "")

    def test_github_unreachable_falls_back_to_r2_source_and_url(self):
        """When GitHub is down, R2 supplies both the version and the installable URL."""
        r2 = self._make_r2_latest("99.0.0", "https://r2/ducky_student-v99.0.0.zip")
        with patch("ducky.self_update._check_github_release", return_value=None):
            with patch("ducky.self_update._check_r2_latest", return_value=r2):
                result = check_for_update()
        self.assertTrue(result["update_available"])
        self.assertEqual(result["download_url"], "https://r2/ducky_student-v99.0.0.zip")

    def test_download_and_install_rejects_whl_layout(self):
        """download_and_install must refuse a GitHub .whl (no src/ducky) rather than
        install the wrong package layout. This is the contract the source fix protects."""
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ducky/__init__.py", b"x")  # .whl layout: no src/
        with patch("ducky.self_update.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.side_effect = [buf.getvalue(), b""]
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response
            result = download_and_install("http://example.com/ducky-1.2.3.whl")
        self.assertFalse(result["ok"])
        self.assertIn("Could not find ducky package", result["error"])


if __name__ == "__main__":
    unittest.main()
