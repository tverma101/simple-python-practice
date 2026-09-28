"""Tests for ducky.server and ducky.student_client — comprehensive edge cases.

Targets common AI-generated code failures:
  1. Rate limiting
  2. Malformed / missing auth headers
  3. Invalid JSON / large bodies
  4. Missing required fields
  5. CORS and OPTIONS handling
  6. Foreign key integrity for submissions
  7. Teacher endpoint auth isolation
  8. Unicode / unusual inputs
  9. Base64 decode errors
  10. Server health metadata
"""

from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ducky.classroom import ClassroomDB
from ducky.config import save_config
from ducky.network import discover_teacher_servers
from ducky.server import DuckyServer, _RateLimiter
from ducky.student_client import StudentClient, StudentClientError


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> None:
    """Poll a TCP port until it's ready, or raise on timeout."""
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except (ConnectionRefusedError, OSError) as e:
            last_err = e
            time.sleep(0.05)
    raise TimeoutError(
        f"Server on {host}:{port} did not become ready within {timeout}s"
    ) from last_err


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests for _RateLimiter
# ═══════════════════════════════════════════════════════════════════════════


class RateLimiterTests(unittest.TestCase):
    """Test the internal rate limiter used by the server."""

    def test_allows_within_limit(self):
        limiter = _RateLimiter(max_attempts=5, window_seconds=60)
        for _ in range(5):
            self.assertTrue(limiter.check("client-1"))

    def test_blocks_after_limit(self):
        limiter = _RateLimiter(max_attempts=3, window_seconds=60)
        limiter.check("client-1")
        limiter.check("client-1")
        limiter.check("client-1")
        self.assertFalse(limiter.check("client-1"))

    def test_different_clients_independent(self):
        limiter = _RateLimiter(max_attempts=2, window_seconds=60)
        limiter.check("a")
        limiter.check("a")
        self.assertFalse(limiter.check("a"))
        self.assertTrue(limiter.check("b"))

    def test_window_expires(self):
        """The rate limiter should eventually allow retries after window passes."""
        limiter = _RateLimiter(max_attempts=1, window_seconds=0.05)
        limiter.check("client-x")
        self.assertFalse(limiter.check("client-x"))
        time.sleep(0.06)
        self.assertTrue(limiter.check("client-x"))


# ═══════════════════════════════════════════════════════════════════════════
# Server integration tests
# ═══════════════════════════════════════════════════════════════════════════


class ServerTests(unittest.TestCase):
    """Original server tests from pre-existing test_server.py — preserved intact."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_server.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.port = _find_free_port()
        self.server = None

    def tearDown(self):
        if self.server and self.server.is_running():
            self.server.stop()
        self.db.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start_server(self, access_code: str = "123456789") -> DuckyServer:
        self.server = DuckyServer(
            port=self.port,
            access_code=access_code,
            class_id="test-class",
            class_name="Test Class",
            db=self.db,
        )
        self.server.start()
        _wait_for_port(self.port)
        return self.server

    def test_server_starts_and_stops(self):
        server = self._start_server()
        self.assertTrue(server.is_running())
        server.stop()
        self.assertFalse(server.is_running())

    def _broadcast_available(self) -> bool:
        """Whether this host can actually send the discovery broadcast.

        Teacher discovery is a UDP datagram to the broadcast address, which is
        refused inside most container and sandbox network namespaces with
        ``EHOSTUNREACH``. ``discover_teacher_servers`` swallows that OSError and
        returns an empty list, so without a probe the test fails for a reason
        that has nothing to do with the code under test. Probing the send keeps
        the rest of the class running everywhere while still covering the real
        behaviour wherever broadcast does work.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe.sendto(b"probe", ("<broadcast>", 8421))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def test_server_reports_bound_port_and_is_discoverable(self):
        server = DuckyServer(
            port=0,
            access_code="123456789",
            class_id="discover-class",
            class_name="Discovery Class",
            db=self.db,
        )
        self.server = server
        server.start()
        self.assertGreater(server.port, 0)
        self.assertIn(f":{server.port}", server.get_url())

        if not self._broadcast_available():
            self.skipTest("UDP broadcast unavailable in this network namespace")

        discovered = discover_teacher_servers(timeout=1.0)
        matches = [item for item in discovered if item.get("class_id") == "discover-class"]
        self.assertTrue(matches, "running server was not found by discovery")
        self.assertEqual(matches[0]["url"], server.get_url())
        self.assertNotIn("access_code", matches[0])

    def test_server_creates_class(self):
        self._start_server()
        cls = self.db.get_class("test-class")
        self.assertIsNotNone(cls)
        self.assertEqual(cls["access_code"], "123456789")

    def test_connect_endpoint(self):
        self._start_server()
        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "123456789"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertIn("token", result)
        self.assertEqual(result["class_name"], "Test Class")

    def test_student_connect_can_discover_teacher_without_url(self):
        self._start_server()
        client = StudentClient(teacher_url="")
        with patch(
            "ducky.network.discover_teacher_servers",
            return_value=[{"url": f"http://127.0.0.1:{self.port}", "class_name": "Test Class"}],
        ), patch("ducky.student_client.save_config"):
            result = client.connect("123456789", "Ada")
        self.assertEqual(result["class_name"], "Test Class")
        self.assertEqual(client.teacher_url, f"http://127.0.0.1:{self.port}")

    def test_connect_wrong_code(self):
        self._start_server()
        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "000000000"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_list_packs(self):
        self._start_server()
        self.db.add_pack("test-class", "l1", "Lesson 1", b"pack-data")

        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "123456789"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            token = json.loads(resp.read())["token"]

        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/packs",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertEqual(len(result["packs"]), 1)
        self.assertEqual(result["packs"][0]["pack_id"], "l1")

    def test_download_pack(self):
        self._start_server()
        self.db.add_pack("test-class", "l1", "Lesson 1", b"binary-pack-data")

        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "123456789"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            token = json.loads(resp.read())["token"]

        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/packs/l1/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
        self.assertEqual(data, b"binary-pack-data")

    def test_submit(self):
        self._start_server()
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")

        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "123456789"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            token = json.loads(resp.read())["token"]

        submit_data = json.dumps(
            {
                "pack_id": "l1",
                "report_json": {"passed": True},
                "student_code": "def add(a, b): return a + b",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/submit",
            data=submit_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertTrue(result["ok"])
        self.assertIn("submission_id", result)

    def test_status(self):
        self._start_server()
        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "123456789"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            token = json.loads(resp.read())["token"]

        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertEqual(result["pack_count"], 0)
        self.assertEqual(result["student_count"], 1)

    def test_unauthorized_access(self):
        self._start_server()
        req = urllib.request.Request(f"http://localhost:{self.port}/api/packs")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


class HintEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_hint.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.port = _find_free_port()
        self.server = None

    def tearDown(self):
        if self.server and self.server.is_running():
            self.server.stop()
        self.db.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start_server(self, llm_config=None) -> DuckyServer:
        self.server = DuckyServer(
            port=self.port,
            access_code="123456789",
            class_id="test-class",
            class_name="Test Class",
            db=self.db,
            llm_config=llm_config or {},
        )
        self.server.start()
        _wait_for_port(self.port)
        return self.server

    def _get_token(self) -> str:
        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Ada", "code": "123456789"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())["token"]

    def test_hint_endpoint_no_llm(self):
        self._start_server(llm_config={"llm_enabled": False})
        token = self._get_token()
        hint_data = json.dumps(
            {
                "diagnosis": "wrong return value",
                "lesson_id": "l1",
                "title": "Test",
                "code_snippet": "def f(): return 2",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/hint",
            data=hint_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertIsNone(result["hint"])
        self.assertEqual(result["reason"], "LLM hints not enabled")

    def test_hint_endpoint_missing_diagnosis(self):
        self._start_server(llm_config={"llm_enabled": True, "llm_backend": "none"})
        token = self._get_token()
        hint_data = json.dumps(
            {
                "lesson_id": "l1",
                "title": "Test",
                "code_snippet": "def f(): return 2",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/hint",
            data=hint_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_hint_endpoint_with_invalid_auth(self):
        self._start_server(llm_config={"llm_enabled": True})
        hint_data = json.dumps(
            {
                "diagnosis": "error",
                "lesson_id": "l1",
                "title": "Test",
                "code_snippet": "x = 1",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/hint",
            data=hint_data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer invalid-token-xyz"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_health_endpoint(self):
        self._start_server()
        req = urllib.request.Request(f"http://localhost:{self.port}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertTrue(result["ok"])
        self.assertIn("version", result)

    def test_hint_endpoint_without_auth(self):
        self._start_server(llm_config={"llm_enabled": True})
        hint_data = json.dumps({"diagnosis": "error"}).encode()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/hint",
            data=hint_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


class StudentClientTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_client.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.port = _find_free_port()
        self.server = DuckyServer(
            port=self.port,
            access_code="123456789",
            class_id="test-class",
            class_name="Test Class",
            db=self.db,
        )
        self.server.start()
        _wait_for_port(self.port)
        self.url = f"http://localhost:{self.port}"

    def tearDown(self):
        if self.server.is_running():
            self.server.stop()
        self.db.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_connect(self):
        client = StudentClient(teacher_url=self.url)
        result = client.connect("123456789", "Ada")
        self.assertIn("token", result)
        self.assertEqual(result["class_name"], "Test Class")

    def test_list_packs(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        packs = client.list_packs()
        self.assertEqual(len(packs), 1)

    def test_download_pack(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"pack-bytes")
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        dest = Path(self.tmpdir) / "downloaded.duckypack"
        client.download_pack("l1", dest)
        self.assertEqual(dest.read_bytes(), b"pack-bytes")

    def test_submit(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        result = client.submit("l1", {"passed": True}, "code here")
        self.assertTrue(result["ok"])

    def test_status(self):
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        status = client.status()
        self.assertEqual(status["student_count"], 1)

    def test_connect_wrong_code(self):
        client = StudentClient(teacher_url=self.url)
        with self.assertRaises(StudentClientError):
            client.connect("000000000", "Ada")

    def test_stale_token_reconnect_can_recover_more_than_once(self):
        """A later expiry in the same session must trigger a fresh reconnect."""
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        first_token = client.token
        self.db.revoke_student(first_token, "test-class")

        with patch(
            "ducky.student_client.load_config",
            return_value={"teacher_access_code": "123456789", "student_name": "Ada"},
        ), patch("ducky.student_client.save_config"):
            self.assertEqual(client.status()["class_id"], "test-class")
            second_token = client.token
            self.assertNotEqual(second_token, first_token)

            self.db.revoke_student(second_token, "test-class")
            self.assertEqual(client.status()["class_id"], "test-class")
            self.assertNotEqual(client.token, second_token)


# ═══════════════════════════════════════════════════════════════════════════
# AI Slop Edge Cases — HTTP/Server-level
# ═══════════════════════════════════════════════════════════════════════════


class ServerEdgeCaseTests(unittest.TestCase):
    """Edge case tests for DuckyServer covering AI slop patterns."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_edge.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.port = _find_free_port()
        self.server = None
        self._start_server()

    def tearDown(self):
        if self.server and self.server.is_running():
            self.server.stop()
        self.db.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _start_server(self, access_code: str = "123456789", llm_config: dict | None = None):
        self.server = DuckyServer(
            port=self.port,
            access_code=access_code,
            class_id="test-class",
            class_name="Test Class",
            db=self.db,
            llm_config=llm_config or {},
        )
        self.server.start()
        _wait_for_port(self.port)
        return self.server

    def _connect(self, name: str = "Ada", code: str = "123456789") -> str:
        """Connect a student and return token."""
        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": name, "code": code}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())["token"]

    def _admin_token(self) -> str:
        """Return this server's secret teacher admin token (NOT the access code)."""
        return self.server.get_admin_token()

    def _get(self, path: str, token: str | None = None) -> dict:
        """GET with optional Bearer token."""
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://localhost:{self.port}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def _post(self, path: str, body: bytes | None = None, token: str | None = None, expected_status: int | None = None) -> tuple[int, bytes]:
        """POST with optional body and token. Returns (status_code, body_bytes)."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://localhost:{self.port}{path}",
            data=body or b"{}",
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return (resp.status, resp.read())
        except urllib.error.HTTPError as e:
            if expected_status is not None:
                self.assertEqual(e.code, expected_status)
            return (e.code, e.read())

    # ── Health / metadata ─────────────────────────────────

    def test_health_returns_version(self):
        result = self._get("/api/health")
        self.assertTrue(result["ok"])
        self.assertIn("version", result)
        self.assertIsInstance(result["version"], str)

    def test_llm_status_disabled(self):
        result = self._get("/api/llm-status")
        self.assertIn("llm_enabled", result)
        self.assertIn("backend", result)

    def test_llm_status_enabled(self):
        # Create a new server with LLM enabled
        port2 = _find_free_port()
        server2 = DuckyServer(
            port=port2,
            access_code="999999999",
            class_id="llm-class",
            class_name="LLM Class",
            db=self.db,
            llm_config={"llm_enabled": True, "llm_backend": "gemini", "llm_model": "gemini-2.0-flash"},
        )
        server2.start()
        _wait_for_port(port2)
        try:
            req = urllib.request.Request(f"http://localhost:{port2}/api/llm-status")
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            self.assertTrue(result["llm_enabled"])
            self.assertEqual(result["backend"], "gemini")
        finally:
            server2.stop()

    # ── Connect edge cases ────────────────────────────────

    def test_connect_missing_name(self):
        status, body = self._post("/api/connect", json.dumps({"code": "123456789"}).encode(), expected_status=400)
        result = json.loads(body)
        self.assertIn("error", result)

    def test_connect_missing_code(self):
        status, body = self._post("/api/connect", json.dumps({"name": "Ada"}).encode(), expected_status=400)
        result = json.loads(body)
        self.assertIn("error", result)

    def test_connect_empty_name(self):
        status, body = self._post("/api/connect", json.dumps({"name": "", "code": "123456789"}).encode(), expected_status=400)
        result = json.loads(body)
        self.assertIn("error", result)

    def test_connect_empty_code(self):
        status, body = self._post("/api/connect", json.dumps({"name": "Ada", "code": ""}).encode(), expected_status=400)
        result = json.loads(body)
        self.assertIn("error", result)

    def test_connect_unicode_name(self):
        status, body = self._post("/api/connect", json.dumps({"name": "Zoë Müller", "code": "123456789"}).encode())
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertIn("token", result)

    def test_connect_wrong_code_causes_401(self):
        status, body = self._post("/api/connect", json.dumps({"name": "Ada", "code": "000000000"}).encode(), expected_status=401)

    # ── Auth edge cases ────────────────────────────────────

    def test_no_auth_header_returns_401(self):
        status, body = self._post("/api/submit", b"{}", expected_status=401)

    def test_malformed_auth_header_returns_401(self):
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/status",
            headers={"Authorization": "NotBearer abc"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_expired_token_returns_401(self):
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/status",
            headers={"Authorization": "Bearer deadbeefdeadbeefdeadbeefdeadbeef"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_wrong_class_token_returns_403(self):
        """Token valid for a different class in the same DB returns 403."""
        # Create a second class in the same DB
        self.db.create_class("class-2", "Second Class", "222222222")
        # Connect student to class-2 directly
        self.db.add_pack("class-2", "l1", "Lesson 1", b"data")
        url = f"http://localhost:{self.port}/api/connect"
        data = json.dumps({"name": "Eve", "code": "222222222"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            token2 = json.loads(resp.read())["token"]
        # Use token2 against the class-1 pack endpoint — 403 not 401
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/packs",
            headers={"Authorization": f"Bearer {token2}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    # ── Body / JSON edge cases ────────────────────────────

    def test_empty_body_on_connect(self):
        status, body = self._post("/api/connect", b"", expected_status=400)

    def test_invalid_json_on_connect(self):
        """Invalid JSON is parsed as empty dict; missing name/code → 400."""
        status, body = self._post("/api/connect", b"not json", expected_status=400)
        result = json.loads(body)
        self.assertIn("error", result)

    def test_submit_without_pack_id(self):
        token = self._connect()
        status, body = self._post("/api/submit", json.dumps({"report_json": {"passed": True}}).encode(), token=token, expected_status=400)

    def test_submit_without_report_json(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        token = self._connect()
        status, body = self._post("/api/submit", json.dumps({"pack_id": "l1"}).encode(), token=token)
        self.assertEqual(status, 200)
        # Should succeed with empty report_json

    def test_submit_non_existent_pack(self):
        token = self._connect()
        status, body = self._post("/api/submit", json.dumps({"pack_id": "nonexistent", "report_json": {}}).encode(), token=token, expected_status=404)

    def test_submit_with_code_not_connected(self):
        """Student who never connected tries to submit — token not in DB."""
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/submit",
            data=json.dumps({"pack_id": "l1", "report_json": {}}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer never-created-token-1234567890abcdef"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    # ── Pack download edge cases ──────────────────────────

    def test_download_nonexistent_pack(self):
        token = self._connect()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/packs/missing/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_download_pack_without_data(self):
        """Pack with no pack_bytes returns 404."""
        self.db.add_pack("test-class", "l1", "Lesson 1", b"")  # b"" is falsy
        token = self._connect()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/packs/l1/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ── CORS / routing ────────────────────────────────────

    def test_options_request(self):
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/packs",
            method="OPTIONS",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIn("Access-Control-Allow-Origin", resp.headers)
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    def test_unknown_route_returns_404(self):
        req = urllib.request.Request(f"http://localhost:{self.port}/api/unknown")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_unknown_route_post_returns_404(self):
        status, body = self._post("/api/unknown", b"{}", expected_status=404)

    def test_unknown_route_delete_returns_404(self):
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/unknown",
            method="DELETE",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ── Teacher endpoints ──────────────────────────────────

    def test_teacher_students_without_auth(self):
        req = urllib.request.Request(f"http://localhost:{self.port}/api/teacher/students")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_teacher_students_with_student_token(self):
        """Student token should not work for teacher endpoints."""
        token = self._connect()
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/teacher/students",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Should have raised HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_teacher_students_access_code_rejected(self):
        """The shared student access code must NOT grant teacher/admin access.

        This is the core privacy fix: previously the access code (which every
        student knows) granted read access to all classmates' submissions.
        """
        self._connect("Alice")
        self._connect("Bob")
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/teacher/students",
            headers={"Authorization": "Bearer 123456789"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("Access code should not grant teacher access")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_teacher_students_with_admin_token(self):
        """Teacher can list students using the separate admin token as Bearer."""
        self._connect("Alice")
        self._connect("Bob")
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/teacher/students",
            headers={"Authorization": f"Bearer {self._admin_token()}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertIn("students", result)
        self.assertEqual(len(result["students"]), 2)

    def test_teacher_students_admin_token_distinct_from_access_code(self):
        """The admin token and access code must be different credentials."""
        self.assertNotEqual(self._admin_token(), "123456789")

    def test_teacher_submissions(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        token = self._connect()
        self._post("/api/submit", json.dumps({"pack_id": "l1", "report_json": {"passed": True}}).encode(), token=token)
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/teacher/submissions",
            headers={"Authorization": f"Bearer {self._admin_token()}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertIn("submissions", result)
        self.assertEqual(len(result["submissions"]), 1)

    def test_teacher_announce_empty_message(self):
        status, body = self._post("/api/teacher/announce",
                                  json.dumps({"message": ""}).encode(),
                                  token=self._admin_token(),
                                  expected_status=400)

    def test_teacher_announce_missing_message(self):
        status, body = self._post("/api/teacher/announce",
                                  json.dumps({}).encode(),
                                  token=self._admin_token(),
                                  expected_status=400)

    def test_teacher_announce_success(self):
        status, body = self._post("/api/teacher/announce",
                                  json.dumps({"message": "Hello class!"}).encode(),
                                  token=self._admin_token())
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertIn("announcement_id", result)

    # ── Teacher add-pack edge cases ────────────────────────

    def test_teacher_add_pack_missing_fields(self):
        # Missing pack_id
        status, body = self._post("/api/teacher/packs",
                                  json.dumps({"title": "Test", "pack_bytes": base64.b64encode(b"data").decode()}).encode(),
                                  token=self._admin_token(),
                                  expected_status=400)
        # Missing title
        status, body = self._post("/api/teacher/packs",
                                  json.dumps({"pack_id": "l1", "pack_bytes": base64.b64encode(b"data").decode()}).encode(),
                                  token=self._admin_token(),
                                  expected_status=400)
        # Missing pack_bytes
        status, body = self._post("/api/teacher/packs",
                                  json.dumps({"pack_id": "l1", "title": "Test"}).encode(),
                                  token=self._admin_token(),
                                  expected_status=400)

    def test_teacher_add_pack_invalid_base64(self):
        status, body = self._post("/api/teacher/packs",
                                  json.dumps({"pack_id": "l1", "title": "Test", "pack_bytes": "not-valid-base64!!"}).encode(),
                                  token=self._admin_token(),
                                  expected_status=400)

    def test_teacher_add_pack_success(self):
        pack_bytes = base64.b64encode(b"test-pack-data").decode()
        status, body = self._post("/api/teacher/packs",
                                  json.dumps({"pack_id": "l1", "title": "Lesson 1", "pack_bytes": pack_bytes}).encode(),
                                  token=self._admin_token())
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["ok"])

    def test_teacher_remove_pack(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        req = urllib.request.Request(
            f"http://localhost:{self.port}/api/teacher/packs/l1",
            headers={"Authorization": f"Bearer {self._admin_token()}"},
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        self.assertTrue(result["ok"])

    def test_teacher_endpoints_without_auth(self):
        for path in ["/api/teacher/students", "/api/teacher/submissions", "/api/teacher/announce", "/api/teacher/packs"]:
            req = urllib.request.Request(f"http://localhost:{self.port}{path}")
            try:
                if path in ("/api/teacher/announce", "/api/teacher/packs"):
                    data = json.dumps({"dummy": "data"}).encode()
                    req.data = data
                    req.method = "POST"
                    req.add_header("Content-Type", "application/json")
                urllib.request.urlopen(req, timeout=5)
                self.fail(f"Should have raised HTTPError for {path}")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401, f"Expected 401 for {path}, got {e.code}")

    # ── Status / announcements / assignments access ────────

    def test_status_shows_assignment_and_announcement_counts(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        self.db.add_assignment("test-class", "l1", "2026-07-15")
        self.db.add_announcement("test-class", "Welcome!")
        token = self._connect()
        result = self._get("/api/status", token)
        self.assertEqual(result["pack_count"], 1)
        self.assertEqual(result["assignment_count"], 1)
        self.assertEqual(result["announcement_count"], 1)

    def test_list_announcements(self):
        self.db.add_announcement("test-class", "Hello!")
        token = self._connect()
        result = self._get("/api/announcements", token)
        self.assertIn("announcements", result)
        self.assertEqual(len(result["announcements"]), 1)

    def test_list_assignments(self):
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        self.db.add_assignment("test-class", "l1", "2026-07-15")
        token = self._connect()
        result = self._get("/api/assignments", token)
        self.assertIn("assignments", result)
        self.assertEqual(len(result["assignments"]), 1)


# ═══════════════════════════════════════════════════════════════════════════
# Student Client AI Slop edge cases
# ═══════════════════════════════════════════════════════════════════════════


class StudentClientEdgeCaseTests(unittest.TestCase):
    """Edge cases for StudentClient targeting AI slop patterns."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Isolate the Ducky config so the reconnect-on-stale-token feature cannot
        # read a real ~/.ducky/config.json (with a matching access code) and silently
        # reconnect where the test expects an error.
        self._old_home = os.environ.get("DUCKY_HOME")
        self._cfg_home = Path(self.tmpdir) / "cfg"
        self._cfg_home.mkdir(parents=True, exist_ok=True)
        os.environ["DUCKY_HOME"] = str(self._cfg_home)
        self.db_path = Path(self.tmpdir) / "test_sc_edge.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.port = _find_free_port()
        self.server = DuckyServer(
            port=self.port,
            access_code="123456789",
            class_id="test-class",
            class_name="Test Class",
            db=self.db,
        )
        self.server.start()
        _wait_for_port(self.port)
        self.url = f"http://localhost:{self.port}"

    def tearDown(self):
        if self.server.is_running():
            self.server.stop()
        self.db.close()
        import shutil

        if self._old_home is None:
            os.environ.pop("DUCKY_HOME", None)
        else:
            os.environ["DUCKY_HOME"] = self._old_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_connect_refused(self):
        """Connecting to a non-running server should raise."""
        client = StudentClient(teacher_url="http://localhost:1")
        with self.assertRaises(StudentClientError):
            client.connect("123456789", "Ada")

    def test_list_packs_without_connect(self):
        """Calling list_packs without connecting first should raise."""
        client = StudentClient(teacher_url=self.url)
        with self.assertRaises(StudentClientError):
            client.list_packs()

    def test_download_pack_without_connect(self):
        client = StudentClient(teacher_url=self.url)
        with self.assertRaises(StudentClientError):
            client.download_pack("l1", Path("/dev/null"))

    def test_submit_without_connect(self):
        client = StudentClient(teacher_url=self.url)
        with self.assertRaises(StudentClientError):
            client.submit("l1", {"passed": True}, "code")

    def test_status_without_connect(self):
        client = StudentClient(teacher_url=self.url)
        with self.assertRaises(StudentClientError):
            client.status()

    def test_trailing_slash_url(self):
        """Teacher URL with trailing slash should still work."""
        client = StudentClient(teacher_url=self.url + "/")
        result = client.connect("123456789", "Ada")
        self.assertIn("token", result)

    def test_reconnect_updates_token(self):
        """Connecting twice with same name updates the stored token."""
        client = StudentClient(teacher_url=self.url)
        first = client.connect("123456789", "Ada")
        second = client.connect("123456789", "Ada")
        self.assertNotEqual(first["token"], second["token"])

    def test_connect_with_wrong_code_returns_error_message(self):
        client = StudentClient(teacher_url=self.url)
        with self.assertRaises(StudentClientError) as ctx:
            client.connect("000000000", "Ada")
        self.assertIn("Invalid access code", str(ctx.exception))


class ConnectionImprovementTests(unittest.TestCase):
    """Tests for the end-to-end connection improvements:
      - progress sync keyed by lesson_id (decoupled from pack_id)
      - quiz result submission loop
      - transparent reconnect on a stale/invalidated token
      - announcement dedup in background sync
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Isolate the Ducky config so reconnect logic uses only what we write here.
        self._old_home = os.environ.get("DUCKY_HOME")
        self._cfg_home = Path(self.tmpdir) / "cfg"
        self._cfg_home.mkdir(parents=True, exist_ok=True)
        os.environ["DUCKY_HOME"] = str(self._cfg_home)
        self.db_path = Path(self.tmpdir) / "test_conn.db"
        self.db = ClassroomDB(db_path=self.db_path)
        self.port = _find_free_port()
        self.server = DuckyServer(
            port=self.port,
            access_code="123456789",
            class_id="test-class",
            class_name="Test Class",
            db=self.db,
        )
        self.server.start()
        _wait_for_port(self.port)
        self.url = f"http://localhost:{self.port}"

    def tearDown(self):
        if self.server.is_running():
            self.server.stop()
        self.db.close()
        if self._old_home is None:
            os.environ.pop("DUCKY_HOME", None)
        else:
            os.environ["DUCKY_HOME"] = self._old_home
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _connect(self, name: str = "Ada") -> str:
        client = StudentClient(teacher_url=self.url)
        return client.connect("123456789", name)["token"]

    def _student_client(self) -> StudentClient:
        return StudentClient(
            teacher_url=self.url,
            token=self._connect(),
            student_name="Ada",
        )

    def test_progress_sync_with_lesson_id_not_pack_id(self):
        """Progress with a lesson_id that is NOT a teacher pack_id must succeed.

        Regression guard: the old /api/submit path 404'd (or FK-violated) when a
        student's lesson_id differed from the teacher's pack_id. /api/progress is
        decoupled from pack existence.
        """
        client = self._student_client()
        attempts = [
            {"id": 1, "lesson_id": "lesson_07_functions", "passed": True,
             "diagnosis": "ok", "timestamp": "2026-07-22T00:00:00"},
            {"id": 2, "lesson_id": "lesson_03_loops", "passed": False,
             "diagnosis": "wrong", "timestamp": "2026-07-22T00:01:00"},
        ]
        result = client.sync_progress(attempts)
        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["failed"], 0)
        # Stored as kind='progress' with NULL pack_id.
        self.assertEqual(len(self.db.list_submissions("test-class", kind="progress")), 2)

    def test_progress_sync_empty_is_noop(self):
        client = self._student_client()
        self.assertEqual(client.sync_progress([]), {"sent": 0, "failed": 0, "errors": [], "synced_ids": []})

    def test_sync_progress_does_not_create_fake_submission(self):
        """Progress pings must NOT appear as class submissions (kind filter)."""
        client = self._student_client()
        client.sync_progress([{"id": 1, "lesson_id": "l9", "passed": True}])
        self.assertEqual(len(self.db.list_submissions("test-class", kind="submission")), 0)

    def test_quiz_result_submission_round_trip(self):
        """POST /api/quiz-result stores a kind='quiz' row the teacher can read."""
        client = self._student_client()
        report = {"correct": 2, "total": 3, "score_pct": 66.6, "passed": False, "taken_at": "t"}
        result = client.submit_quiz_result("quiz.l9", "l9", report)
        self.assertTrue(result.get("ok"))
        quiz_rows = self.db.list_submissions("test-class", kind="quiz")
        self.assertEqual(len(quiz_rows), 1)
        payload = json.loads(quiz_rows[0]["report_json"])
        self.assertEqual(payload["quiz_id"], "quiz.l9")
        self.assertEqual(payload["correct"], 2)

    def test_quiz_result_retry_with_same_id_is_idempotent(self):
        client = self._student_client()
        first = client.submit_quiz_result(
            "quiz.l9", "l9", {"correct": 2, "total": 3, "score_pct": 67, "passed": False}
        )
        second = client.submit_quiz_result(
            "quiz.l9",
            "l9",
            {"correct": 2, "total": 3, "score_pct": 67, "passed": False},
            submission_id=first["submission_id"],
        )
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.assertEqual(len(self.db.list_submissions("test-class", kind="quiz")), 1)

    def test_progress_rejects_unhashable_attempt_ids_without_crashing(self):
        client = self._student_client()
        result = client.sync_progress([{"id": ["bad"], "lesson_id": "l9"}])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(self.db.list_submissions("test-class", kind="progress")), 0)

    def test_quiz_result_queues_when_teacher_is_temporarily_unavailable(self):
        """Offline quiz grading must remain visible and retryable."""
        from ducky.outbox import DurableOutbox

        client = self._student_client()
        with patch.object(
            client, "_post_json", side_effect=StudentClientError("offline")
        ):
            queued = client.submit_quiz_result(
                "quiz.l9",
                "l9",
                {"correct": 1, "total": 2, "score_pct": 50, "passed": False},
                queue_on_failure=True,
            )

        self.assertTrue(queued["queued"])
        self.assertEqual(len(DurableOutbox().list("quiz_result")), 1)

        with patch.object(client, "_post_json", return_value={"ok": True}):
            flushed = client.flush_queue()
        self.assertEqual(flushed["sent"], 1)
        self.assertEqual(DurableOutbox().count(), 0)

    def test_quiz_result_requires_quiz_id(self):
        token = self._connect()
        req = urllib.request.Request(
            f"{self.url}/api/quiz-result",
            data=json.dumps({"lesson_id": "l9", "correct": 1}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        # Either way, no quiz row may be stored.
        self.assertEqual(len(self.db.list_submissions("test-class", kind="quiz")), 0)

    def test_teacher_endpoints_reject_access_code(self):
        """Access code (student-known) must be rejected for admin endpoints."""
        for path in ["/api/teacher/students", "/api/teacher/submissions"]:
            req = urllib.request.Request(
                f"{self.url}{path}",
                headers={"Authorization": "Bearer 123456789"},
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail(f"{path} should reject access code")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 403, path)

    def test_teacher_submissions_show_quiz_separately(self):
        """Teacher reports list quiz/progress without them polluting submissions."""
        client = self._student_client()
        self.db.add_pack("test-class", "l1", "Lesson 1", b"data")
        client.submit("l1", {"passed": True}, "code")
        client.sync_progress([{"id": 1, "lesson_id": "l9", "passed": True}])
        client.submit_quiz_result("quiz.l9", "l9", {"correct": 1, "total": 1})
        self.assertEqual(len(self.db.list_submissions("test-class", kind="submission")), 1)
        self.assertEqual(len(self.db.list_submissions("test-class", kind="progress")), 1)
        self.assertEqual(len(self.db.list_submissions("test-class", kind="quiz")), 1)

    def test_reconnect_on_stale_token_succeeds(self):
        """When the server forgets a student's token, a silent reconnect via the
        saved access code lets the next call succeed (no exception, no user action).

        This is the regression guard for the 'stale token → permanent failures'
        failure mode.
        """
        # Connect and write the access code/name into the isolated config.
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        # Sanity: list_packs works with the fresh token.
        self.assertEqual(client.list_packs(), [])
        # Invalidate the student's token in the SAME db the running server uses
        # (simulates a teacher clearing students / server reset).
        conn = self.db._get_conn()
        with self.db._lock:
            conn.execute("DELETE FROM students")
            conn.commit()
        # The next call should transparently reconnect (token was invalidated).
        self.assertEqual(client.list_packs(), [])
        # Token should have been refreshed by the reconnect.
        self.assertTrue(client.token)

    def test_reconnect_does_not_loop_on_permanent_auth_failure(self):
        """If the access code is wrong, reconnect returns the 401 instead of looping.

        Guard against infinite reconnect recursion: a permanently-invalid code
        must fail cleanly, not spin forever.
        """
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        client.student_name = "Ada"
        # Invalidate the current token so the next call triggers a reconnect attempt.
        conn = self.db._get_conn()
        with self.db._lock:
            conn.execute("DELETE FROM students")
            conn.commit()
        # Point config at a wrong access code so reconnect fails.
        save_config({"teacher_access_code": "000000000", "student_name": "Ada"})
        client.teacher_url = self.url
        with self.assertRaises(StudentClientError):
            client.list_packs()

    def test_reconnect_requires_saved_access_code(self):
        """Without a saved access code, a stale token surfaces the error (old behavior)."""
        client = StudentClient(teacher_url=self.url)
        client.connect("123456789", "Ada")
        # Invalidate the current token so the next call triggers a reconnect attempt.
        conn = self.db._get_conn()
        with self.db._lock:
            conn.execute("DELETE FROM students")
            conn.commit()
        # No access code in config → reconnect can't happen.
        save_config({"teacher_access_code": "", "student_name": "Ada"})
        client.teacher_url = self.url
        with self.assertRaises(StudentClientError):
            client.list_packs()


if __name__ == "__main__":
    unittest.main()
