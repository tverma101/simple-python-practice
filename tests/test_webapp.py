"""Tests for the practice server.

Weighted towards the security properties rather than the happy path, because the
happy path is a template render and the security properties are the reason this
server is allowed to exist on a network at all.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ducky.classroom import ClassroomDB
from ducky.content import ContentStatus, ProblemSpec, TestCase, parse_test_case
from ducky.contentstore import Verdict, build_store
from ducky.sandbox import SandboxPolicy
from ducky.webapp import WebApp, _parse_cookies, serve

# ── fixtures ────────────────────────────────────────────────────────


def _spec(slug: str = "hello") -> ProblemSpec:
    cases = [
        parse_test_case(0, '{"args": {"name": "Ada"}}', '"Hello, Ada!"'),
        parse_test_case(1, '{"args": {"name": "Bob"}}', '"Hello, Bob!"', is_hidden=True),
    ]
    return ProblemSpec(
        slug=slug,
        title="Say hello",
        description="Return a greeting.",
        difficulty="Beginner",
        function_name="greet",
        parameters=["name"],
        function_signature="def greet(name):",
        starter_code="def greet(name):\n    # YOUR CODE HERE\n    pass\n",
        solution_code="def greet(name):\n    return f'Hello, {name}!'\n",
        test_cases=cases,
    )


@pytest.fixture
def store(tmp_path):
    return build_store([_spec()], tmp_path / "content.db")


@pytest.fixture
def classroom(tmp_path):
    db = ClassroomDB(db_path=tmp_path / "classroom.db")
    db.create_class("c1", "Cohort", access_code="111111111")
    db.create_student("c1", "Ada", access_code="222222222")
    return db


@pytest.fixture
def app(store, classroom, tmp_path):
    from ducky.routes import build_app

    return build_app(
        classroom=classroom,
        content=store,
        static_root=tmp_path / "static",
        # Short: these tests spawn real subprocesses and should not be slow.
        policy=SandboxPolicy(wall_seconds=8, cpu_seconds=5),
    )


def _request(app: WebApp, method: str, path: str, **kwargs):
    from ducky.webapp import Request

    body = kwargs.pop("body", b"")
    headers = {k.lower(): v for k, v in kwargs.pop("headers", {}).items()}
    headers.setdefault("host", "127.0.0.1:8765")
    request = Request(
        method=method,
        path=path,
        query=kwargs.pop("query", {}),
        headers=headers,
        body=body,
        client_ip=kwargs.pop("ip", "10.0.0.5"),
    )
    return app.handle(request)


# ── host / origin: DNS rebinding ────────────────────────────────────


class TestHostGuard:
    def test_known_host_allowed(self, app):
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            response = _request(app, "GET", "/", headers={"host": host})
            assert response.status == 200, host

    def test_unknown_host_refused(self, app):
        """The rebinding case: a public hostname pointed at the LAN address."""
        response = _request(app, "GET", "/", headers={"host": "attacker.example.com"})
        assert response.status == 403
        assert b"Host" in response.body or b"host" in response.body

    def test_private_lan_addresses_allowed(self, app):
        for host in ("192.168.1.50:8765", "10.0.0.9:8765"):
            assert _request(app, "GET", "/", headers={"host": host}).status == 200, host

    def test_missing_host_refused(self, app):
        assert _request(app, "GET", "/", headers={"host": ""}).status == 403

    def test_host_check_runs_before_routing(self, app):
        """Even a known path must not be served to an unrecognised Host."""
        response = _request(app, "GET", "/api/health", headers={"host": "evil.test"})
        assert response.status == 403


# ── access codes ────────────────────────────────────────────────────


class TestAccessCodes:
    def test_browsing_is_open(self, app):
        assert _request(app, "GET", "/").status == 200
        assert _request(app, "GET", "/practice").status == 200
        assert _request(app, "GET", "/p/hello").status == 200
        assert _request(app, "GET", "/api/problems/hello").status == 200

    def test_grading_requires_a_code(self, app):
        body = json.dumps({"slug": "hello", "code": "x"}).encode()
        response = _request(app, "POST", "/api/grade", body=body)
        assert response.status == 401
        assert json.loads(response.body)["detail"] == "access_code_required"

    def test_wrong_code_refused(self, app):
        body = json.dumps({"slug": "hello", "code": "x",
                           "access_code": "999999999"}).encode()
        assert _request(app, "POST", "/api/grade", body=body).status == 401

    def test_valid_code_accepted_via_header(self, app):
        body = json.dumps({"slug": "hello",
                           "code": "def greet(name):\n    return f'Hello, {name}!'\n"}).encode()
        response = _request(app, "POST", "/api/grade", body=body,
                            headers={"x-ducky-access-code": "222222222"})
        assert response.status == 200
        assert json.loads(response.body)["passed"] is True

    def test_session_cookie_replaces_repeated_codes(self, app):
        sign_in = json.dumps({"access_code": "222222222"}).encode()
        first = _request(app, "POST", "/api/sign-in", body=sign_in)
        assert first.status == 200
        cookie = first.headers["Set-Cookie"].split(";")[0]
        body = json.dumps({"slug": "hello", "code": "x"}).encode()
        response = _request(app, "POST", "/api/grade", body=body,
                            headers={"cookie": cookie})
        assert response.status == 200

    def test_revoked_code_stops_working(self, app):
        app.classroom.revoke_student(
            app.classroom.get_student_by_code("222222222")["token"], "c1"
        )
        body = json.dumps({"slug": "hello", "code": "x",
                           "access_code": "222222222"}).encode()
        assert _request(app, "POST", "/api/grade", body=body).status == 401

    @pytest.mark.parametrize("route", ["/api/run", "/api/grade", "/api/draft"])
    def test_every_mutating_route_is_guarded(self, app, route):
        assert _request(app, "POST", route, body=b"{}").status == 401


# ── hidden test secrecy ─────────────────────────────────────────────


class TestHiddenSecrecy:
    def _grade(self, app, code: str, *, expect_pass: bool):
        body = json.dumps({"slug": "hello", "code": code,
                           "access_code": "222222222"}).encode()
        response = _request(app, "POST", "/api/grade", body=body)
        assert response.status == 200
        return json.loads(response.body)

    def test_passing_submission_reveals_no_hidden_answer(self, app):
        payload = self._grade(
            app, "def greet(name):\n    return f'Hello, {name}!'\n", expect_pass=True
        )
        assert payload["passed"] is True
        assert payload["cases_hidden"] == 1
        # A passing run reports no failures at all, so there is nothing to leak.
        assert payload["failures"] == []

    def test_failing_hidden_test_does_not_leak_its_expected_value(self, app):
        """The regression that motivated the data-layer redaction.

        The previous codebase set `expected` on every grader result including
        hidden ones and the UI printed it unconditionally, so failing one of
        23,349 hidden tests printed the answer.
        """
        payload = self._grade(app, "def greet(name):\n    return 'nope'\n",
                              expect_pass=False)
        assert payload["passed"] is False
        hidden = [f for f in payload["failures"] if f["is_hidden"]]
        assert hidden, "the hidden case should have been reported as failing"
        for failure in hidden:
            assert failure["expected_repr"] == "<hidden>"
            assert "Hello, Bob!" not in json.dumps(payload)

    def test_visible_failure_still_shows_expected(self, app):
        """Redaction must not remove feedback the student needs."""
        payload = self._grade(app, "def greet(name):\n    return 'nope'\n",
                              expect_pass=False)
        visible = [f for f in payload["failures"] if not f["is_hidden"]]
        assert visible
        assert visible[0]["expected_repr"] == repr("Hello, Ada!")

    def test_problem_api_never_returns_any_expected_value(self, app):
        payload = json.loads(_request(app, "GET", "/api/problems/hello").body)
        blob = json.dumps(payload)
        assert "Hello, Ada!" not in blob
        assert "Hello, Bob!" not in blob
        assert "solution" not in payload["problem"]


# ── routing ─────────────────────────────────────────────────────────


class TestRouting:
    def test_unknown_path_is_404(self, app):
        assert _request(app, "GET", "/nope").status == 404

    def test_wrong_method_is_405(self, app):
        assert _request(app, "GET", "/api/grade").status == 405

    def test_malformed_json_is_400_not_500(self, app):
        response = _request(app, "POST", "/api/grade", body=b"{not json",
                            headers={"x-ducky-access-code": "222222222"})
        assert response.status == 400

    def test_missing_fields_are_400(self, app):
        response = _request(app, "POST", "/api/grade", body=b"{}",
                            headers={"x-ducky-access-code": "222222222"})
        assert response.status == 400


# ── rate limiting ───────────────────────────────────────────────────


class TestRateLimit:
    def test_grading_is_rate_limited_per_ip(self, classroom, store, tmp_path):
        from ducky.routes import build_app

        instance = build_app(
            classroom=classroom, content=store,
            static_root=tmp_path / "static", grade_limit=(3, 60),
        )

        codes = [
            b'{"slug":"hello","code":"def greet(name):\\n    return 1\\n"}',
            b'{"slug":"hello","code":"def greet(name):\\n    return 1\\n"}',
            b'{"slug":"hello","code":"def greet(name):\\n    return 1\\n"}',
            b'{"slug":"hello","code":"def greet(name):\\n    return 1\\n"}',
        ]
        statuses = [
            _request(instance, "POST", "/api/grade", body=c, ip="10.0.0.9",
                     headers={"x-ducky-access-code": "222222222"}).status
            for c in codes
        ]
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429

    def test_limit_is_per_student_not_per_address(self, classroom, store, tmp_path):
        """Two students behind one NAT address get independent budgets.

        A per-IP limit would make a shared home or classroom connection a
        classroom-wide budget, so one person's iteration locked out everyone
        else. Per-student access codes exist to make this attributable, so the
        limiter keys on the resolved student.
        """
        from ducky.routes import build_app

        classroom.create_student("c1", "Bob", access_code="333333333")
        instance = build_app(
            classroom=classroom, content=store,
            static_root=tmp_path / "static", grade_limit=(1, 60),
        )
        instance.content = store
        instance.classroom = classroom

        payload = b'{"slug":"hello","code":"x"}'
        # Same address, different students: each gets their own first run.
        first = _request(instance, "POST", "/api/grade", body=payload, ip="10.0.0.1",
                         headers={"x-ducky-access-code": "222222222"}).status
        second = _request(instance, "POST", "/api/grade", body=payload, ip="10.0.0.1",
                          headers={"x-ducky-access-code": "333333333"}).status
        assert first == 200 and second == 200

    def test_one_student_exhausts_only_their_own_budget(self, classroom, store, tmp_path):
        from ducky.routes import build_app

        classroom.create_student("c1", "Bob", access_code="333333333")
        instance = build_app(
            classroom=classroom, content=store,
            static_root=tmp_path / "static", grade_limit=(1, 60),
        )
        instance.content = store
        instance.classroom = classroom

        payload = b'{"slug":"hello","code":"x"}'
        headers = {"x-ducky-access-code": "222222222"}
        assert _request(instance, "POST", "/api/grade", body=payload,
                        ip="10.0.0.1", headers=headers).status == 200
        # Same student, different address: still the same budget.
        assert _request(instance, "POST", "/api/grade", body=payload,
                        ip="10.0.0.9", headers=headers).status == 429
        # A classmate is unaffected.
        assert _request(instance, "POST", "/api/grade", body=payload, ip="10.0.0.9",
                        headers={"x-ducky-access-code": "333333333"}).status == 200

    def test_browsing_is_never_rate_limited(self, app):
        for _ in range(50):
            assert _request(app, "GET", "/practice").status == 200


# ── sandbox wiring ──────────────────────────────────────────────────


class TestGradingGoesThroughTheSandbox:
    def test_infinite_loop_is_bounded(self, app):
        body = json.dumps({
            "slug": "hello",
            "code": "def greet(name):\n    while True:\n        pass\n",
            "access_code": "222222222",
        }).encode()
        response = _request(app, "POST", "/api/grade", body=body)
        assert response.status == 200
        assert json.loads(response.body)["passed"] is False

    def test_blocked_import_is_rejected_not_executed(self, app):
        body = json.dumps({
            "slug": "hello",
            "code": "import os\ndef greet(name):\n    return os.getcwd()\n",
            "access_code": "222222222",
        }).encode()
        payload = json.loads(_request(app, "POST", "/api/grade", body=body).body)
        assert payload["passed"] is False
        assert payload["status"] == "sandbox_violation"

    def test_grading_does_not_leak_the_reference_solution(self, app):
        body = json.dumps({"slug": "hello", "code": "x",
                           "access_code": "222222222"}).encode()
        blob = _request(app, "POST", "/api/grade", body=body).body.decode()
        assert "f'Hello, {name}!'" not in blob


# ── helpers ─────────────────────────────────────────────────────────


class TestCookieParsing:
    def test_parses_pairs(self):
        assert _parse_cookies("a=1; b=2") == {"a": "1", "b": "2"}

    def test_tolerates_junk(self):
        assert _parse_cookies("") == {}
        assert _parse_cookies("novalue") == {}


class TestEndToEndOverHTTP:
    """Drive a real socket, so the http.server adapter is covered too."""

    @pytest.fixture
    def live(self, app):
        server = serve(app, host="127.0.0.1", port=0)
        port = server.server_address[1]
        yield port
        server.shutdown()
        server.server_close()

    def _get(self, port, path, host=None):
        url = f"http://127.0.0.1:{port}{path}"
        request = urllib.request.Request(url, headers={"Host": host or f"127.0.0.1:{port}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()

    def _post(self, port, path, payload, *, headers=None):
        url = f"http://127.0.0.1:{port}{path}"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_home_page_renders(self, live):
        status, body = self._get(live, "/")
        assert status == 200
        assert b"Simple Python Practice" in body

    def test_problem_page_carries_the_import_map(self, live):
        status, body = self._get(live, "/p/hello")
        assert status == 200
        text = body.decode()
        assert 'type="importmap"' in text
        # Absolute paths, because the document lives at /p/hello and a relative
        # specifier would resolve to /p/vendor/... and fail silently.
        assert "/static/vendor/cm/view.js" in text
        assert '"./vendor' not in text

    def test_unknown_host_refused_over_http(self, live):
        try:
            self._get(live, "/", host="attacker.example.com")
            raised = False
        except urllib.error.HTTPError as exc:
            raised = exc.code == 403
        assert raised

    def test_grade_over_http(self, live):
        status, payload = self._post(
            live, "/api/grade",
            {"slug": "hello", "code": "def greet(name):\n    return f'Hello, {name}!'\n",
             "access_code": "222222222"},
        )
        assert status == 200
        assert payload["passed"] is True

    def test_grade_without_code_over_http(self, live):
        status, payload = self._post(live, "/api/grade",
                                     {"slug": "hello", "code": "x"})
        assert status == 401
        assert payload["detail"] == "access_code_required"


class TestConcurrencyBound:
    """The limiter bounds arrivals per minute; a semaphore bounds parallelism.

    A rate limit cannot protect a CPU when 20 requests arriving in the same
    second all pass it and all run. Measured: one student with one valid code
    could hold every core continuously. The semaphore is the control that
    matches the resource.
    """

    def test_slots_are_bounded(self, app):
        assert app.grade_slots is not None
        # Every slot must be acquirable and then all held.
        held = [app.grade_slots.acquire(blocking=False) for _ in range(64)]
        acquired = sum(1 for ok in held if ok)
        try:
            assert 1 <= acquired <= 4, acquired
        finally:
            for ok in held:
                if ok:
                    app.grade_slots.release()

    def test_a_run_that_times_out_still_releases_its_slot(self, app):
        """A leaked slot would eventually wedge the grader for everyone."""
        before = _free_slots(app)
        body = json.dumps({
            "slug": "hello",
            "code": "def greet(name):\n    while True:\n        pass\n",
            "access_code": "222222222",
        }).encode()
        assert _request(app, "POST", "/api/grade", body=body).status == 200
        assert _free_slots(app) == before

    def test_a_rejected_submission_still_releases_its_slot(self, app):
        before = _free_slots(app)
        body = json.dumps({
            "slug": "hello", "code": "import os\nx=1\n", "access_code": "222222222",
        }).encode()
        assert _request(app, "POST", "/api/grade", body=body).status == 200
        assert _free_slots(app) == before


def _free_slots(app):
    """Count currently-available slots.

    Drain the semaphore without releasing as it goes. Acquiring *and* releasing
    in the same loop never exhausts it, so the loop never terminates.
    """
    taken = []
    try:
        while app.grade_slots.acquire(blocking=False):
            taken.append(True)
        return len(taken)
    finally:
        for _ in taken:
            app.grade_slots.release()


class TestNegativeContentLength:
    """`Content-Length: -1` used to reach rfile.read(-1), which reads to EOF.

    One header, unauthenticated, and the server buffers whatever the client
    sends -- on a machine that is also serving a LAN. Asserted over a real socket
    because the guard lives in the http.server adapter, not in dispatch.
    """

    def test_rejected_rather_than_buffered(self, app):
        import socket as _socket

        server = serve(app, host="127.0.0.1", port=0)
        port = server.server_address[1]
        try:
            sock = _socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.sendall(
                b"POST /api/grade HTTP/1.1\r\n"
                b"Host: 127.0.0.1:" + str(port).encode() + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: -1\r\n\r\n"
            )
            sock.settimeout(10)
            try:
                reply = sock.recv(4096)
            except TimeoutError:
                sock.close()
                pytest.fail("server blocked on a negative Content-Length")
            sock.close()
            # Either a rejection, or a reset -- what matters is that it did not
            # sit there waiting for EOF.
            assert reply, "no response at all"
            assert reply.split()[1] in (b"400", b"403", b"413", b"404"), reply[:80]
        finally:
            server.shutdown()
            server.server_close()

    def test_oversize_body_is_rejected(self, app):
        import socket as _socket

        server = serve(app, host="127.0.0.1", port=0)
        port = server.server_address[1]
        try:
            sock = _socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.sendall(
                b"POST /api/grade HTTP/1.1\r\n"
                b"Host: 127.0.0.1:" + str(port).encode() + b"\r\n"
                b"Content-Length: 5000000\r\n\r\n"
            )
            sock.settimeout(10)
            reply = sock.recv(4096)
            sock.close()
            assert b"413" in reply.split()[1:2] or reply.split()[1] == b"413", reply[:80]
        finally:
            server.shutdown()
            server.server_close()


class TestRunLog:
    def test_runs_are_attributed(self, app, tmp_path):
        from ducky.store import RunLog

        log = RunLog(tmp_path / "runs.db")
        app.run_log = log
        body = json.dumps({
            "slug": "hello",
            "code": "def greet(name):\n    return f'Hello, {name}!'\n",
            "access_code": "222222222",
        }).encode()
        assert _request(app, "POST", "/api/grade", body=body).status == 200

        token = app.classroom.get_student_by_code("222222222")["token"]
        history = log.for_student(token)
        assert len(history) == 1
        assert history[0]["slug"] == "hello"
        assert history[0]["passed"] == 1
        assert log.attempts_on("hello") == 1
        log.close()

    def test_drafts_round_trip(self, app, tmp_path):
        from ducky.store import DraftStore

        store = DraftStore(tmp_path / "drafts.db")
        app.drafts = store
        token = app.classroom.get_student_by_code("222222222")["token"]

        store.save(token, "hello", "def greet(name):\n    return 1\n")
        assert store.load(token, "hello") == "def greet(name):\n    return 1\n"
        # Per-student: a classmate cannot read someone else's work.
        assert store.load("someone-else", "hello") == ""
        store.clear(token, "hello")
        assert store.load(token, "hello") == ""
        store.close()

    def test_draft_route_says_saved_when_it_saved(self, app, tmp_path):
        from ducky.store import DraftStore

        app.drafts = DraftStore(tmp_path / "d2.db")
        token = app.classroom.get_student_by_code("222222222")["token"]
        body = json.dumps({"slug": "hello", "code": "x = 1"}).encode()
        response = _request(app, "POST", "/api/draft", body=body,
                            headers={"x-ducky-access-code": "222222222"})
        payload = json.loads(response.body)
        assert payload["saved"] is True
        assert app.drafts.load(token, "hello") == "x = 1"

    def test_draft_route_is_honest_when_no_store_is_configured(self, app):
        """200 with saved:false, so the client can tell -- not a bare 200."""
        app.drafts = None
        body = json.dumps({"slug": "hello", "code": "x = 1"}).encode()
        response = _request(app, "POST", "/api/draft", body=body,
                            headers={"x-ducky-access-code": "222222222"})
        assert response.status == 200
        assert json.loads(response.body)["saved"] is False


class TestStaticAssets:
    """Every asset the templates reference must actually be served.

    Regression: the served root is the `static/` directory *and* every template
    URL begins `/static/`, so the prefix was being prepended twice and all 30-odd
    assets 404'd. The page still rendered, so it looked like a styling problem --
    but the editor is only unhidden by its own script, so the student got no
    editor and no working Run/Submit. Every other test in this file passed
    because none of them fetched a static URL.
    """

    ASSETS = [
        "/static/css/tokens.css",
        "/static/css/workspace.css",
        "/static/js/app.js",
        "/static/js/problem.js",
        "/static/js/editor.js",
        "/static/js/theme.js",
        "/static/vendor/cm/view.js",
        "/static/vendor/cm/state.js",
        "/static/vendor/cm/lang-python.js",
    ]

    def test_every_referenced_asset_is_served(self, app, tmp_path):
        # A real static tree, since the app is served from one.
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "static"
        assert root.is_dir(), "the shipped static tree is missing"
        app.static.root = root.resolve()
        for asset in self.ASSETS:
            assert app.static.resolve(asset) is not None, f"{asset} does not resolve"
            served = app.static.serve(asset)
            assert served is not None, f"{asset} is not served"
            assert served.status == 200
            assert len(served.body) > 0

    def test_assets_are_served_over_http(self, app):
        from pathlib import Path

        from ducky.webapp import serve

        app.static.root = (Path(__file__).resolve().parent.parent / "static").resolve()
        server = serve(app, host="127.0.0.1", port=0)
        port = server.server_address[1]
        try:
            import urllib.request

            for asset in self.ASSETS:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{asset}",
                    headers={"Host": f"127.0.0.1:{port}"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = response.read()
                assert response.status == 200, asset
                assert body, asset
        finally:
            server.shutdown()
            server.server_close()

    def test_javascript_is_served_with_a_script_content_type(self, app):
        """A wrong MIME type makes the browser refuse to execute the module."""
        from pathlib import Path

        app.static.root = (Path(__file__).resolve().parent.parent / "static").resolve()
        served = app.static.serve("/static/js/problem.js")
        assert "javascript" in served.content_type

    def test_traversal_is_still_refused_after_prefix_stripping(self, app):
        from pathlib import Path

        app.static.root = (Path(__file__).resolve().parent.parent / "static").resolve()
        for attempt in (
            "/static/../../etc/passwd",
            "/static/../pyproject.toml",
            "/static/%2e%2e/pyproject.toml",
        ):
            assert app.static.resolve(attempt) is None, attempt


# ── Run and Submit must not be the same button ──────────────────────
#
# Both handlers used to call _grade with identical arguments, so "Run" ran the
# hidden cases and wrote the run log exactly as Submit did. A student could
# complete a problem by pressing Run, and the two buttons were indistinguishable.
class TestRunIsNotSubmit:
    AUTH = {"x-ducky-access-code": "222222222"}

    def _run(self, app, mode):
        body = json.dumps({
            "slug": "hello",
            "code": "def greet(name):\n    return f'Hello, {name}!'\n",
        }).encode()
        response = _request(app, "POST", f"/api/{mode}", body=body,
                            headers=self.AUTH)
        assert response.status == 200
        return json.loads(response.body)

    def test_run_checks_only_the_visible_samples(self, app):
        payload = self._run(app, "run")
        # The fixture has one visible and one hidden case. Run must not count the
        # hidden one, or "2 of 2 passed" implies a hidden test was graded.
        assert payload["cases_total"] == 1
        assert payload["cases_hidden"] == 0

    def test_submit_checks_everything(self, app):
        payload = self._run(app, "grade")
        assert payload["cases_total"] == 2
        assert payload["cases_hidden"] == 1

    def test_run_does_not_record_progress(self, app, tmp_path):
        # Run is practice. If it logged the run, a student could complete a
        # problem by never submitting.
        app.attach_stores(tmp_path / "state")
        assert app.run_log is not None
        token = app.classroom.get_student_by_code("222222222")["token"]

        self._run(app, "run")
        assert app.run_log.for_student(token) == []

        self._run(app, "grade")
        assert len(app.run_log.for_student(token)) == 1

    def test_run_does_not_mutate_the_stored_spec(self, app, store):
        # Stripping the hidden cases has to happen on a copy. Mutating the spec
        # the store returned would destroy the hidden cases for every later
        # request the moment anything caches them.
        self._run(app, "run")
        reloaded = store.load_problem("hello")
        assert len([tc for tc in reloaded.test_cases if tc.is_hidden]) == 1


# ── drafts must come back ───────────────────────────────────────────
#
# The save authenticated and keyed the draft on the student token; the load used
# the session cookie. Two different identifiers, so a saved draft could never be
# read back and a returning student silently got the starter code.
class TestDraftRoundTrip:
    AUTH = {"x-ducky-access-code": "222222222"}

    def test_saved_draft_is_returned_by_the_same_session(self, app, tmp_path):
        app.attach_stores(tmp_path / "state")

        saved = _request(app, "POST", "/api/draft", headers=self.AUTH,
                         body=json.dumps({"slug": "hello",
                                          "code": "# my saved work"}).encode())
        assert json.loads(saved.body)["saved"] is True

        # Same session, as a student returning to the page would have.
        sign_in = _request(app, "POST", "/api/sign-in",
                           body=json.dumps({"access_code": "222222222"}).encode())
        cookie = sign_in.headers["Set-Cookie"].split(";")[0]
        loaded = _request(app, "GET", "/api/draft/hello",
                          headers={"cookie": cookie})
        assert json.loads(loaded.body)["code"] == "# my saved work"

    def test_load_requires_a_session(self, app, tmp_path):
        app.attach_stores(tmp_path / "state")
        _request(app, "POST", "/api/draft", headers=self.AUTH,
                 body=json.dumps({"slug": "hello", "code": "work"}).encode())
        anonymous = _request(app, "GET", "/api/draft/hello")
        assert anonymous.status == 401


class TestProblemListPaging:
    """The list used to hard-cap at 200 and tell the reader to narrow the search.

    2,400 of 2,603 problems were reachable only by guessing a filter, and the
    JSON list returned 200 items with no total, so a client could not even tell
    that more existed.
    """

    def _seed(self, tmp_path, count):
        # The browsable list only contains VERIFIED problems, and build_store
        # recomputes status via classify_static rather than trusting the spec, so
        # passing a verdict is the only way to get a non-empty list. Verdicts are
        # keyed by source_id, so each spec needs a distinct one.
        specs = []
        for i in range(count):
            spec = _spec(f"p{i:03d}")
            spec.source_id = i + 1
            specs.append(spec)
        verdicts = {
            spec.source_id: Verdict(ContentStatus.VERIFIED, "2/2 cases pass", "none")
            for spec in specs
        }
        return build_store(specs, tmp_path / "content.db", verdicts=verdicts)

    def _app(self, store, tmp_path):
        from ducky.routes import build_app

        return build_app(
            classroom=ClassroomDB(db_path=tmp_path / "classroom.db"),
            content=store,
            static_root=tmp_path / "static",
            policy=SandboxPolicy(wall_seconds=8, cpu_seconds=5),
        )

    def test_every_problem_is_reachable(self, tmp_path):
        store = self._seed(tmp_path, 260)
        app = self._app(store, tmp_path)
        seen = set()
        for page in range(1, 7):
            # The test helper takes the query separately; the real HTTP handler
            # parses it off the request line (webapp.py).
            response = _request(app, "GET", "/practice", query={"page": [str(page)]})
            assert response.status == 200
            seen.update(re.findall(r'href="/p/(p\d+)"', response.body.decode()))
        assert len(seen) == 260, sorted(seen)[:5]

    def test_json_reports_the_total_and_paging(self, tmp_path):
        store = self._seed(tmp_path, 260)
        app = self._app(store, tmp_path)
        payload = json.loads(_request(app, "GET", "/api/problems").body)
        assert payload["total"] == 260
        assert payload["has_more"] is True
        assert payload["returned"] == len(payload["problems"])

        last = json.loads(
            _request(app, "GET", "/api/problems", query={"offset": ["250"]}).body
        )
        assert last["returned"] == 10
        assert last["has_more"] is False

    def test_filters_narrow_the_total(self, tmp_path):
        store = self._seed(tmp_path, 120)
        app = self._app(store, tmp_path)
        payload = json.loads(
            _request(app, "GET", "/api/problems", query={"limit": ["500"]}).body
        )
        assert payload["total"] == 120
        # The limit is clamped, so asking for 500 does not return 500 rows.
        assert payload["limit"] <= 200
