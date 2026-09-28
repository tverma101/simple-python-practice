"""The local web app: a LAN-reachable practice server on the standard library.

Serves the practice UI, grades submissions through :mod:`ducky.runner`, and
exposes the content store. No web framework, no build step, no remote assets --
the project holds runtime dependencies at zero and this keeps that true.

Why the security shape is what it is
------------------------------------

This server binds ``0.0.0.0`` so anyone on the local network can open it, and it
runs student code on request. Those two facts together are the whole threat
model, and they pull against each other:

* **Browsing is open.** A student should be able to look at a problem without
  proving who they are. Read-only routes carry no credential.
* **Running code needs a per-student access code.** Grading is the expensive and
  abusable operation. Requiring a code means a classmate cannot anonymously
  saturate the machine's CPU, and it means a run is attributable to a person.
  See :meth:`ducky.classroom.ClassroomDB.create_student`.
* **Grading is rate limited per IP**, because an access code stops anonymous
  abuse but not one student with a valid code submitting in a loop.
* **Host and Origin are checked**, because a server on ``0.0.0.0`` answers to any
  hostname, which is exactly the shape a DNS-rebinding attack needs. Without this
  a page on the internet could drive the grader through a student's browser.

What this is not: a multi-tenant judge. See ``docs/SECURITY_MODEL.md`` for what
the sandbox does and does not stop, and read that before exposing this beyond a
trusted LAN.
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .sandbox import DEFAULT_POLICY, SandboxPolicy, describe_policy

__all__ = [
    "WebApp",
    "Route",
    "Router",
    "Request",
    "Response",
    "RateLimiter",
    "HostGuard",
    "create_app",
]

STATIC_ROOT = Path(__file__).resolve().parent.parent.parent / "static"

#: Grading is the only expensive operation, so it is the only rate limited one.
#: Generous enough that a student can iterate, tight enough that a loop cannot
#: hold the machine: 20 runs a minute is far more than anyone legitimately needs
#: and far less than a runaway.
DEFAULT_GRADE_LIMIT = (20, 60)

#: Refuse bodies larger than this outright. A code submission is a few KB; a
#: lesson description with test cases is tens of KB. 2 MB is generous and stops a
#: trivial memory-exhaustion vector on a machine that is also serving a LAN.
MAX_BODY_BYTES = 2 * 1024 * 1024

#: How long a grading request may take before the client is expected to have
#: given up. The sandbox's own wallclock is shorter; this is the outer bound that
#: stops a hung request occupying a worker thread indefinitely.
GRADE_REQUEST_TIMEOUT = 90.0


# ── Request / response ───────────────────────────────────────────────


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    client_ip: str
    params: dict[str, str] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest(f"body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    def form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self.body.decode("utf-8", "replace"))
        return {k: v[0] for k, v in parsed.items() if v}

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def wants_json(self) -> bool:
        accept = self.header("accept")
        return "application/json" in accept or self.path.startswith("/api/")


class Response:
    __slots__ = ("status", "body", "headers", "content_type")

    def __init__(
        self,
        body: bytes | str = b"",
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.content_type = content_type
        self.headers = headers or {}

    @classmethod
    def json(
        cls,
        payload: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return cls(
            json.dumps(payload, default=str),
            status=status,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    @classmethod
    def text(cls, message: str, *, status: int = 200) -> Response:
        return cls(message, status=status, content_type="text/plain; charset=utf-8")

    @classmethod
    def redirect(cls, location: str, *, status: int = 303) -> Response:
        # 303 so a POST redirects as GET; a 302 would re-submit on some clients.
        return cls(b"", status=status, headers={"Location": location})

    @classmethod
    def problem(cls, status: int, message: str, *, detail: str = "") -> Response:
        return cls.json({"error": message, "detail": detail}, status=status)


class BadRequest(Exception):
    """A client error that should become a 400, not a 500."""


class Unauthorized(Exception):
    """Missing or unknown access code on a mutating route."""


class NotFound(Exception):
    pass


# ── Router ──────────────────────────────────────────────────────────

_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class Route:
    method: str
    pattern: str
    handler: Callable[[Request], Response]
    regex: re.Pattern[str]
    #: Requires a valid per-student access code. Read-only browsing is open.
    requires_code: bool = False

    def match(self, path: str) -> dict[str, str] | None:
        found = self.regex.match(path)
        return found.groupdict() if found else None


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(
        self,
        method: str,
        pattern: str,
        handler: Callable[[Request], Response],
        *,
        requires_code: bool = False,
    ) -> None:
        regex = re.compile(
            "^" + _PARAM.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", pattern) + "$"
        )
        self.routes.append(
            Route(
                method=method.upper(),
                pattern=pattern,
                handler=handler,
                regex=regex,
                requires_code=requires_code,
            )
        )

    def route(self, method: str, path: str) -> tuple[Route | None, dict[str, str], bool]:
        """Find a route. Returns ``(route, params, path_known)``.

        ``path_known`` distinguishes "no such path" from "wrong method", which
        matters for returning 405 instead of a misleading 404.
        """
        path_known = False
        for route in self.routes:
            params = route.match(path)
            if params is None:
                continue
            path_known = True
            if route.method == method.upper():
                return route, params, True
        return None, {}, path_known


# ── Guards ──────────────────────────────────────────────────────────


class RateLimiter:
    """Fixed-window per-key limiter.

    Fixed rather than sliding because it needs no per-key state beyond a counter
    and a reset time, and a practice server's traffic is bursty in a way that
    does not justify a ring buffer per IP.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, seconds_until_reset)``."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False, max(1, int(self.window - (now - min(hits))))
            hits.append(now)
            self._hits[key] = hits
            # Opportunistic cleanup so a long-lived server does not accumulate
            # an entry per IP forever.
            if len(self._hits) > 2048:
                for stale in [k for k, v in self._hits.items() if not v or max(v) < cutoff]:
                    self._hits.pop(stale, None)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class HostGuard:
    """Reject requests whose Host or Origin is not ours.

    A server bound to ``0.0.0.0`` answers to whatever name it is addressed by.
    That is what makes DNS rebinding possible: an attacker's page resolves a
    hostname to the victim's LAN IP, and the browser then treats the response as
    same-origin with the attacker's site. Requiring a Host we recognise, and
    rejecting a cross-site Origin on mutating requests, closes that.

    The host list is seeded with the machine's own addresses, so this works on a
    fresh checkout with no configuration.
    """

    def __init__(self, allowed: set[str] | None = None) -> None:
        self.allowed: set[str] = {h.lower() for h in (allowed or set())}
        self.allowed.update({"localhost", "127.0.0.1", "::1", "[::1]"})
        self.allowed.update(h.lower() for h in _local_addresses())

    def check_host(self, host_header: str) -> bool:
        if not host_header:
            return False
        # Strip the port. IPv6 literals arrive as [::1]:8080.
        name = host_header.strip().lower()
        if name.startswith("["):
            name = name[1:].split("]", 1)[0]
        elif ":" in name:
            name = name.rsplit(":", 1)[0]
        if name in self.allowed:
            return True
        # A LAN address we did not enumerate at startup, e.g. the machine got a
        # new DHCP lease after boot. Accept private ranges rather than force an
        # operator to restart the server.
        return _is_private_host(name)


def _is_private_host(name: str) -> bool:
    import ipaddress

    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        # Not an IP. Refuse unknown names: a public hostname is exactly the
        # rebinding case.
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _local_addresses() -> set[str]:
    """Best-effort list of this machine's addresses."""
    found: set[str] = set()
    try:
        import socket

        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except OSError:
        pass
    return found


# ── Static files ────────────────────────────────────────────────────


class StaticFiles:
    """Serve files from a root, refusing anything that escapes it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    #: URL prefix the templates use. The served root is the static directory
    #: itself, so the prefix has to come off before joining -- otherwise
    #: /static/js/app.js resolves to static/static/js/app.js and every asset in
    #: the app 404s while the HTML still renders.
    URL_PREFIX = "static/"

    def resolve(self, url_path: str) -> Path | None:
        relative = urllib.parse.unquote(url_path).lstrip("/")
        if relative.startswith(self.URL_PREFIX):
            relative = relative[len(self.URL_PREFIX):]
        if not relative:
            return None
        try:
            target = (self.root / relative).resolve()
            target.relative_to(self.root)
        except (ValueError, OSError):
            return None
        return target if target.is_file() else None

    def serve(self, url_path: str) -> Response | None:
        target = self.resolve(url_path)
        if target is None:
            return None
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        headers = {
            # Content can change between builds; a stale cached editor is worse
            # than a re-fetch on a LAN.
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(
            target.read_bytes(),
            content_type=ctype or "application/octet-stream",
            headers=headers,
        )


# ── Application ─────────────────────────────────────────────────────


class WebApp:
    """Wires the router, guards, and content together.

    Deliberately constructed with its dependencies passed in rather than reached
    for, so a test can supply a temporary content store and a real sandbox
    policy without touching the filesystem or a network.
    """

    def __init__(
        self,
        *,
        router: Router | None = None,
        classroom: Any = None,
        content: Any = None,
        static_root: Path | None = None,
        policy: SandboxPolicy | None = None,
        host_guard: HostGuard | None = None,
        grade_limit: tuple[int, int] = DEFAULT_GRADE_LIMIT,
        grade_concurrency: int | None = None,
    ) -> None:
        if grade_concurrency is None:
            import os as _os

            # Leave a core for the server itself and the student's machine.
            grade_concurrency = max(1, min(4, (_os.cpu_count() or 2) - 1))
        self.router = router or Router()
        self.classroom = classroom
        self.content = content
        # Optional persistence. Declared here rather than reached for with
        # getattr, because a getattr default silently disables the feature it was
        # looking for: the client was told "Saved" while nothing was stored.
        self.run_log: Any = None
        self.drafts: Any = None
        # Per-instance, not a class attribute: a mutable list on the class is
        # shared by every WebApp, so one instance's warnings surface on another.
        self.warnings: list[str] = []
        self.static = StaticFiles(static_root or STATIC_ROOT)
        self.policy = policy or DEFAULT_POLICY
        self.host_guard = host_guard or HostGuard()
        self.grade_limiter = RateLimiter(*grade_limit)
        # Concurrency, not just rate. The limiter above bounds arrivals per
        # minute, but grading consumes a CPU for up to wall_seconds, so N
        # simultaneous requests all pass the limiter and all run. A semaphore
        # sized near the core count is the control that matches the resource.
        # Without it, one valid access code can hold every core indefinitely by
        # keeping 20 runs in flight at all times.
        self.grade_slots = threading.BoundedSemaphore(max(1, grade_concurrency))
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_lock = threading.Lock()

    # -- sessions -----------------------------------------------------

    def start_session(self, student: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions[token] = {
                "student": student,
                "created": time.monotonic(),
            }
        return token

    def session_for(self, token: str) -> dict[str, Any] | None:
        with self._session_lock:
            return self._sessions.get(token)

    def end_session(self, token: str) -> None:
        with self._session_lock:
            self._sessions.pop(token, None)

    def authenticate(self, request: Request) -> dict[str, Any]:
        """Resolve the student behind a mutating request.

        Two accepted credentials, in order: an existing session cookie, then a
        per-student access code. The code path exists because a student should be
        able to open the app, be told their code, and start work without a login
        flow -- the server is on a trusted LAN and the code is the credential.
        """
        cookie = _parse_cookies(request.header("cookie")).get("ducky_session")
        if cookie:
            found = self.session_for(cookie)
            if found:
                return found["student"]
        code = (
            request.header("x-ducky-access-code")
            or request.form().get("access_code")
            or (request.json().get("access_code") if request.body else "")
        )
        code = str(code or "").strip()
        if not code:
            raise Unauthorized("This action needs an access code.")
        student = None
        if self.classroom is not None:
            student = self.classroom.get_student_by_code(code)
        if student is None:
            raise Unauthorized("That access code is not recognised.")
        return dict(student)

    # -- dispatch -----------------------------------------------------

    def handle(self, request: Request) -> Response:
        # Host first: a rebinding attempt must not reach any route, including
        # the static handler.
        if not self.host_guard.check_host(request.header("host")):
            return Response.problem(
                HTTPStatus.FORBIDDEN,
                "Unrecognised host",
                detail=(
                    "This server only answers for its own address. A request "
                    "arriving with an unfamiliar Host header is usually a DNS "
                    "rebinding attempt."
                ),
            )

        route, params, path_known = self.router.route(request.method, request.path)
        if route is None:
            if path_known:
                return Response.problem(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Method not allowed",
                    detail=f"{request.path} accepts other methods.",
                )
            served = self.static.serve(request.path)
            if served is not None:
                return served
            return Response.problem(HTTPStatus.NOT_FOUND, "Not found", detail=request.path)

        request.params = params
        try:
            if route.requires_code:
                request_student = self.authenticate(request)
                request.params = {**params, "_student_token": request_student.get("token", "")}
            return route.handler(request)
        except BadRequest as exc:
            return Response.problem(HTTPStatus.BAD_REQUEST, str(exc))
        except Unauthorized as exc:
            return Response.problem(HTTPStatus.UNAUTHORIZED, str(exc), detail="access_code_required")
        except NotFound as exc:
            return Response.problem(HTTPStatus.NOT_FOUND, str(exc))

    def attach_stores(self, state_dir: Path) -> WebApp:
        """Create the run log and draft store under ``state_dir``.

        Called by the app factory rather than left to a caller, because the
        attribute existing but never being assigned is what made autosave
        silently dead: the route read ``app.drafts``, got ``None``, and answered
        ``{"saved": false}`` while the client displayed "Saved".
        """
        from .store import DraftStore, RunLog

        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        if self.run_log is None:
            self.run_log = RunLog(state_dir / "runs.db")
        if self.drafts is None:
            self.drafts = DraftStore(state_dir / "drafts.db")
        return self

    def log_warning(self, message: str) -> None:
        """Record an operator-facing problem the student cannot act on."""
        self.warnings.append(message)

    def security_summary(self) -> str:
        """What this server is and is not, in one block. Used by /api/security."""
        return (
            f"sandbox: {describe_policy(self.policy)}\n"
            f"binds: 0.0.0.0 (reachable from the local network)\n"
            f"browsing: open, no credential\n"
            f"code execution: requires a per-student access code\n"
            f"grade limit: {self.grade_limiter.limit} per "
            f"{self.grade_limiter.window}s per client\n"
        )


def _parse_cookies(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


# ── http.server adapter ─────────────────────────────────────────────


def make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DuckyPractice"
        sys_version = ""  # do not advertise the Python version
        protocol_version = "HTTP/1.1"
        # Without this, a client that connects and sends nothing pins a worker
        # thread for as long as it likes. ThreadingHTTPServer spawns one thread
        # per connection with no cap, so this is the whole bound on concurrency.
        timeout = 30

        def log_message(self, fmt: str, *args: Any) -> None:
            # Quiet by default; a LAN server that logs every keystroke-bearing
            # request to a terminal is its own problem.
            return

        def handle_one_request(self) -> None:
            # Wrap the base implementation so a client that hangs up mid-request
            # does not print a full traceback to the teacher's terminal on every
            # aborted connection.
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError, TimeoutError):
                self.close_connection = True

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._emit(Response.problem(400, "Bad Content-Length"))
                return None
            # `length > MAX` alone is not enough: a negative Content-Length
            # passes it, and rfile.read(-1) reads to EOF. That is one header
            # away from unbounded buffering in this process, unauthenticated, on
            # a machine that is also serving a LAN.
            if length < 0:
                self._emit(Response.problem(400, "Negative Content-Length"))
                return None
            if length > MAX_BODY_BYTES:
                self._emit(
                    Response.problem(
                        413,
                        "Request body too large",
                        detail=f"limit is {MAX_BODY_BYTES} bytes",
                    )
                )
                return None
            return self.rfile.read(length) if length else b""

        def _dispatch(self, method: str) -> None:
            body = self._read_body()
            if body is None:
                return
            parsed = urllib.parse.urlparse(self.path)
            request = Request(
                method=method,
                path=parsed.path,
                query=urllib.parse.parse_qs(parsed.query),
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
                client_ip=self.client_address[0] if self.client_address else "",
            )
            try:
                response = app.handle(request)
            except Exception as exc:  # noqa: BLE001 - last line of defence
                # Never surface a traceback to a client: it leaks paths and
                # internals, and this is a LAN service.
                response = Response.problem(
                    500,
                    "Internal error",
                    detail=f"{type(exc).__name__}",
                )
            self._emit(response)

        def _emit(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            # A browser on another origin must not be able to read responses,
            # and the page must not be embeddable in someone else's frame.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def serve(
    app: WebApp,
    *,
    host: str = "0.0.0.0",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Bind and start. Returns the server so a caller can read the real port."""
    mimetypes.add_type("text/javascript", ".js")
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def create_app(**kwargs: Any) -> WebApp:
    return WebApp(**kwargs)
