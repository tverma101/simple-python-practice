"""Routes for the practice server.

Split from :mod:`ducky.webapp` so the transport and the policy stay separable:
``webapp`` knows how to receive bytes and enforce guards, this module knows what
a student can do.

Two rules are enforced here and are worth stating plainly, because both were
violations in the codebase this replaces:

* **Quarantined problems are not routable.** A student must never land on a
  problem whose tests are broken, because the outcome is being marked wrong for
  being right. Those appear in a teacher-facing queue instead.
* **A hidden test's expected value never leaves the server.** Redaction happens
  in :meth:`ducky.verify.CaseResult.student_view`; this module only ever calls the
  student view.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from .content import ContentStatus
from .verify import verify_problem
from .webapp import BadRequest, NotFound, Request, Response, Router, WebApp

__all__ = ["build_router", "register_routes"]


def register_routes(router: Router, app: WebApp) -> Router:
    """Attach every route. Grouped by what the student is trying to do."""

    # ── Content: browsing is open ─────────────────────────────────
    # No credential. Looking at a problem should not require proving who you are.

    def index(request: Request) -> Response:
        return _render(request, app, "index.html", {"page": "home"})

    def problem_list(request: Request) -> Response:
        topic = _query(request, "topic")
        difficulty = _query(request, "difficulty")
        search = _query(request, "q")
        per_page = 50
        try:
            page_no = max(1, int(_query(request, "page") or 1))
        except ValueError:
            page_no = 1
        total = _verified_total(app, topic=topic, difficulty=difficulty, search=search)
        page_count = max(1, (total + per_page - 1) // per_page)
        # Clamp rather than 404: a stale bookmark should land on the last page.
        page_no = min(page_no, page_count)
        start = (page_no - 1) * per_page
        problems = _verified_page(
            app, topic=topic, difficulty=difficulty, search=search,
            limit=per_page, offset=start,
        )
        # Pagination. The list hard-capped at 200 and told the reader to narrow
        # the search, so 2,400 of 2,603 problems were reachable only by guessing
        # a filter. A student browsing in order simply hit a wall.
        return _render(
            request,
            app,
            "problems.html",
            {
                "page": "practice",
                # Already paged by the store. Slicing again here re-sliced the
                # page, so every page after the first came back empty.
                "problems": problems,
                "total": total,
                "page_no": page_no,
                "page_count": page_count,
                "per_page": per_page,
                "first_shown": start + 1 if total else 0,
                "last_shown": min(start + per_page, total),
                "truncated": total > per_page,
                "filters": {"topic": topic, "difficulty": difficulty, "q": search},
                "topics": _topics(app),
                "difficulties": ["Beginner", "Intermediate", "Advanced"],
            },
        )

    def problem_detail(request: Request) -> Response:
        spec = _load_problem(app, request.params["slug"])
        if spec is None:
            raise NotFound(f"No problem called {request.params['slug']!r}.")
        if not _is_shippable(spec):
            # Deliberately a 404, not a 403. A student should not be able to
            # enumerate which problems exist but are broken.
            raise NotFound(f"No problem called {request.params['slug']!r}.")
        return _render(
            request,
            app,
            "problem.html",
            {
                # The problem surface is a full-viewport two-pane layout with its
                # own chrome. Without this the renderer wrapped it in base.html a
                # second time: two doctypes, two <body>, four <head>, and
                # duplicate ids for #main and #theme-toggle, so the page rendered
                # two top bars and theme.js bound to the outer, dead one.
                "layout": None,
                "page": "problem",
                "problem": _public_problem(spec),
                "visible_cases": [
                    _public_case(tc) for tc in spec.test_cases if not tc.is_hidden
                ],
                "hidden_count": sum(1 for tc in spec.test_cases if tc.is_hidden),
            },
        )

    def api_problem(request: Request) -> Response:
        spec = _load_problem(app, request.params["slug"])
        if spec is None or not _is_shippable(spec):
            raise NotFound("No such problem.")
        return Response.json({"problem": _public_problem(spec)})

    def api_health(request: Request) -> Response:
        return Response.json(
            {
                "ok": True,
                "sandbox": app.security_summary(),
                "policy": app.policy.to_dict(),
            }
        )

    router.add("GET", "/", index)
    router.add("GET", "/practice", problem_list)
    router.add("GET", "/p/{slug}", problem_detail)
    def api_problem_list(request: Request) -> Response:
        # Returned 200 problems with no total and no way to ask for more, so a
        # client had no way to know 2,400 more existed. Report the count and
        # support explicit paging.
        topic = _query(request, "topic")
        difficulty = _query(request, "difficulty")
        search = _query(request, "q")
        try:
            offset = max(0, int(_query(request, "offset") or 0))
        except ValueError:
            offset = 0
        try:
            limit = min(200, max(1, int(_query(request, "limit") or 50)))
        except ValueError:
            limit = 50
        total = _verified_total(app, topic=topic, difficulty=difficulty, search=search)
        window = _verified_page(
            app, topic=topic, difficulty=difficulty, search=search,
            limit=limit, offset=offset,
        )
        return Response.json({
            "problems": window,
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(window),
            "has_more": offset + len(window) < total,
        })

    router.add("GET", "/api/problems", api_problem_list)
    router.add("GET", "/api/problems/{slug}", api_problem)
    router.add("GET", "/api/health", api_health)

    # ── Mutating: an access code is required ─────────────────────

    def api_run(request: Request) -> Response:
        """Run code against the visible sample tests only.

        Deliberately narrower than ``api_grade``: no hidden cases, nothing
        recorded. This used to call ``_grade`` with the same arguments as Submit,
        so "Run samples" ran everything and logged the result -- the two buttons
        were indistinguishable, and a student could complete a problem by
        pressing Run.

        Rate limited per client IP. An access code stops anonymous abuse; this
        stops one authenticated student from submitting in a loop and taking the
        machine with them.
        """
        throttled = _throttle(app, request)
        if throttled is not None:
            return throttled

        payload = request.json()
        slug = str(payload.get("slug") or "").strip()
        code = str(payload.get("code") or "")
        if not slug:
            raise BadRequest("slug is required")
        if not code.strip():
            raise BadRequest("code is required")

        spec = _load_problem(app, slug)
        if spec is None or not _is_shippable(spec):
            raise NotFound("No such problem.")

        return _grade(
            app, spec, code,
            student_token=request.params.get("_student_token", ""),
            ip=request.client_ip,
            samples_only=True,
        )

    def api_grade(request: Request) -> Response:
        """Grade against every test, including hidden ones."""
        throttled = _throttle(app, request)
        if throttled is not None:
            return throttled
        payload = request.json()
        slug = str(payload.get("slug") or "").strip()
        code = str(payload.get("code") or "")
        if not slug:
            raise BadRequest("slug is required")
        if not code.strip():
            raise BadRequest("code is required")
        spec = _load_problem(app, slug)
        if spec is None or not _is_shippable(spec):
            raise NotFound("No such problem.")
        return _grade(
            app, spec, code,
            student_token=request.params.get("_student_token", ""),
            ip=request.client_ip,
        )

    def api_save_draft(request: Request) -> Response:
        payload = request.json()
        slug = str(payload.get("slug") or "").strip()
        code = payload.get("code")
        if not slug:
            raise BadRequest("slug is required")
        store = _draft_store(app)
        token = request.params.get("_student_token", "")
        if store is None or not token:
            # 200 with saved:false, and the client branches on the field rather
            # than on the status. Returning an error here would show a student a
            # failure for something they cannot fix, but returning a bare 200
            # would let the client claim success for a save that never happened.
            return Response.json(
                {"saved": False, "reason": "no draft store configured"}
            )
        store.save(token, slug, str(code or ""))
        return Response.json({"saved": True, "saved_at": time.time()})

    def api_load_draft(request: Request) -> Response:
        # Keyed on the same student token the save uses. The save authenticated
        # and got a student token while the load used the session cookie, so a
        # saved draft could never be read back and a returning student got the
        # starter code every time, silently.
        store = _draft_store(app)
        token = request.params.get("_student_token", "")
        if store is None or not token:
            return Response.json({"code": ""})
        return Response.json({"code": store.load(token, request.params["slug"])})

    def api_sign_in(request: Request) -> Response:
        """Exchange an access code for a session, so the code is typed once."""
        code = str(request.json().get("access_code") or "").strip()
        if not code:
            raise BadRequest("access_code is required")
        student = app.classroom.get_student_by_code(code) if app.classroom else None
        if student is None:
            raise BadRequest("That access code is not recognised.")
        token = app.start_session(dict(student))
        return Response.json(
            {"name": student["name"], "class_id": student["class_id"]},
            headers={
                "Set-Cookie": (
                    f"ducky_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"
                )
            },
        )

    router.add("POST", "/api/run", api_run, requires_code=True)
    router.add("POST", "/api/grade", api_grade, requires_code=True)
    router.add("POST", "/api/draft", api_save_draft, requires_code=True)
    router.add("POST", "/api/sign-in", api_sign_in, requires_code=True)
    router.add("GET", "/api/draft/{slug}", api_load_draft, requires_code=True)

    return router


def build_router(app: WebApp) -> Router:
    """Register the routes against ``app``.

    The handlers close over the app instance, so this must be called with the
    *same* instance that will serve the requests. Wiring it the other way round
    -- ``build_router(WebApp())`` and then copying the router onto a configured
    app -- produces a server whose handlers read ``content=None`` and 404 on
    every problem, which is exactly the bug this note exists to prevent. Use
    :func:`build_app`, which cannot be wired wrongly.
    """
    return register_routes(Router(), app)


def build_app(**kwargs: Any) -> WebApp:
    """Construct a fully wired app: router and app reference each other correctly."""
    from .webapp import WebApp as _WebApp

    instance = _WebApp(**kwargs)
    instance.router = build_router(instance)
    return instance


# ── Grading ─────────────────────────────────────────────────────────


def _throttle(app: WebApp, request: Request) -> Response | None:
    """Rate limit on the resolved student, falling back to the client IP.

    Keying on the student rather than the address matters on exactly the
    deployment this server is for: a home or classroom LAN where every student
    shares one NAT address, where a per-IP budget means one person's iteration
    locks out everyone else. ``authenticate`` runs before the handler, so the
    token is already in ``request.params``.
    """
    key = request.params.get("_student_token") or f"ip:{request.client_ip}"
    allowed, retry_after = app.grade_limiter.check(key)
    if allowed:
        return None
    return Response.json(
        {"error": "Too many runs. Give it a moment.",
         "retry_after_seconds": retry_after},
        status=429,
    )


_STUDENT_STATUS = {
    "solution_disagrees": "Some of your answers do not match yet.",
    "sandbox_violation": "Your code reached for something this exercise does not allow.",
    "solution_errors": "Your code did not finish, so nothing could be checked.",
    "no_callable": "The function the tests call is not defined in your code.",
    "no_usable_tests": "This problem has no working tests yet, so it cannot be graded.",
    "expected_not_recorded": "This problem's tests are incomplete, so it cannot be graded.",
    "verified": "",
}

_FAILURE_REASON = {
    "value_mismatch": "your answer was the wrong value",
    "type_mismatch": "your answer was the right value but the wrong type",
    "stdout_mismatch": "your printed output did not match",
    "raised": "your code raised an error",
}


def _student_detail(status: str, detail: str) -> str:
    """Turn an internal diagnosis into a sentence about the student's code.

    The diagnosis is kept in the payload for tooling, but the student-facing text
    must not name the reference solution -- that tells them an answer key exists
    and is what they are measured against -- must not use a 0-based case index,
    and must not leak an untranslated enum value.
    """
    lead = _STUDENT_STATUS.get(status, "Something went wrong while grading.")
    if status == "sandbox_violation":
        # The one diagnosis that is already written for a student: actionable,
        # and correctly not blaming their code.
        return detail or lead
    if status == "no_callable" and detail:
        # "signature declares 'add_two' but the solution defines 'f'" -> name the
        # function the tests actually call, which is the part a student can act
        # on. Parse it rather than taking the first quoted fragment, which is the
        # word "declares" and produced "a function called `signature declares `".
        marker = "declares "
        if marker in detail:
            tail = detail.split(marker, 1)[1]
            wanted = tail.split("'", 2)[1] if tail.count("'") >= 2 else ""
            if wanted.strip():
                return (
                    f"The tests call a function called `{wanted.strip()}`. "
                    f"Make sure your code defines it with that name."
                )
    if not lead:
        return ""
    reasons = [
        _FAILURE_REASON[r] for r in _FAILURE_REASON
        if r in detail
    ]
    if reasons:
        lead += " " + reasons[0].capitalize() + "."
    return lead


def _grade(
    app: WebApp,
    spec: Any,
    code: str,
    *,
    student_token: str = "",
    ip: str = "",
    samples_only: bool = False,
) -> Response:
    """Grade a submission and return only what a student may see.

    The reference solution is swapped in for grading and then swapped back out in
    the payload. That is the crux: a student must never receive the answer, and
    the most reliable way to guarantee that is for the answer to never leave the
    function that computed it.
    """
    submitted = ProblemSpecProxy(spec, code)

    # Run means "check my work against the samples", so the hidden cases are
    # stripped before grading rather than merely hidden in the response. Running
    # them and withholding the answer still burns the grader's time budget on
    # every keystroke-triggered Run, and the count would leak how many hidden
    # cases a problem has.
    if samples_only:
        # A copy, not an in-place edit. `load_problem` builds a fresh spec per
        # call today so this is currently harmless, but mutating a store-returned
        # object would silently destroy the hidden cases for every later request
        # the moment anyone adds a cache.
        spec = replace(
            spec,
            test_cases=[tc for tc in spec.test_cases if not tc.is_hidden],
        )

    # Concurrency bound. The rate limiter above bounds arrivals per minute, but
    # each run holds a CPU for up to wall_seconds, so simultaneous requests all
    # pass it. This is the control that actually protects the machine.
    if not app.grade_slots.acquire(timeout=app.policy.wall_seconds + 5):
        return Response.json(
            {"error": "The grader is busy. Try again in a moment."}, status=503
        )
    try:
        result = verify_problem(submitted, policy=app.policy)
    finally:
        app.grade_slots.release()

    if app.run_log is not None and not samples_only:
        app.run_log.record(
            student_token=student_token, slug=spec.slug, ip=ip,
            passed=result.shippable, cases=result.cases_passed,
            total=result.cases_usable, elapsed=None,
        )

    visible = [tc for tc in spec.test_cases if not tc.is_hidden]
    hidden = [tc for tc in spec.test_cases if tc.is_hidden]

    # Surface the diagnosis the student needs, not the internal status. A
    # rejection by the static allowlist is something they can act on; telling
    # them "solution_errors" would suggest their code is broken.
    status = (
        "sandbox_violation"
        if result.status is ContentStatus.SANDBOX_REJECTED
        else result.status.value
    )

    return Response.json(
        {
            "slug": spec.slug,
            "passed": result.shippable,
            "status": status,
            "cases_total": len(visible) + len(hidden),
            "cases_visible": len(visible),
            "cases_hidden": len(hidden),
            "cases_passed": result.cases_passed,
            # student_view redacts a hidden case's expected value. This is the
            # only shape a student-facing route is allowed to serialise.
            "failures": [f.student_view() for f in result.failures],
            # Keyed on the *student-facing* status, not the internal enum:
            # SANDBOX_REJECTED is reported to the student as sandbox_violation,
            # and looking it up under its own name missed the map and fell
            # through to the generic "something went wrong".
            "detail": _student_detail(status, result.detail)
            if not result.shippable else "",
            # Kept alongside, for tooling and for a teacher reading the report.
            "diagnosis": result.status.value,
        }
    )


class ProblemSpecProxy:
    """A problem spec with the student's code in place of the reference.

    Exists so :func:`verify_problem` grades the submission through exactly the
    same path it used to verify the reference -- one grader, so the two can never
    disagree about what counts as correct.
    """

    def __init__(self, spec: Any, code: str) -> None:
        # Order matters. Replacing __dict__ with a copy of the spec's deletes
        # anything set before it -- including _spec -- after which __getattr__
        # recurses forever on the first attribute that is not a plain field. It
        # worked only because every attribute currently touched happens to be one.
        object.__setattr__(self, "_spec", spec)
        self.__dict__ = {**spec.__dict__, "solution_code": code}

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not in __dict__, so _spec is always present here.
        return getattr(object.__getattribute__(self, "_spec"), name)


# ── Content helpers ─────────────────────────────────────────────────


def _load_problem(app: WebApp, slug: str) -> Any | None:
    store = getattr(app, "content", None)
    if store is None:
        return None
    return store.load_problem(slug)


def _is_shippable(spec: Any) -> bool:
    status = spec.status
    return status in (ContentStatus.VERIFIED, ContentStatus.UNVERIFIED) and bool(
        spec.test_cases
    )


def _query(request: Request, name: str) -> str:
    return ((request.query.get(name) or [""])[0] or "").strip()


def _verified_page(
    app: WebApp,
    *,
    topic: str = "",
    difficulty: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """One page of browsable problems, filtered in SQL."""
    store = getattr(app, "content", None)
    if store is None:
        return []
    return store.list_problems(
        limit=limit, offset=offset, topic=topic,
        difficulty=difficulty, search=search,
    )


def _verified_total(
    app: WebApp,
    *,
    topic: str = "",
    difficulty: str = "",
    search: str = "",
) -> int:
    store = getattr(app, "content", None)
    if store is None:
        return 0
    return store.count_problems(
        topic=topic, difficulty=difficulty, search=search,
    )


def _topics(app: WebApp) -> list[str]:
    store = getattr(app, "content", None)
    if store is None:
        return []
    return store.topics()


def _draft_store(app: WebApp) -> Any:
    return getattr(app, "drafts", None)


def _session_token(request: Request) -> str:
    from .webapp import _parse_cookies

    return _parse_cookies(request.header("cookie")).get("ducky_session", "")


def _public_problem(spec: Any) -> dict[str, Any]:
    """The problem as a student may see it.

    No ``solution_code``, no test-case expectations, no reference to the upstream
    database id. The slug is the public identity because it is the only field
    that is unique and stable across every row in the source.
    """
    return {
        "slug": spec.slug,
        "title": spec.title,
        "description": spec.description,
        "difficulty": spec.difficulty,
        "difficulty_percent": spec.difficulty_percent,
        "topic_tier": spec.topic_tier,
        "tags": spec.tags,
        "learning_outcomes": spec.learning_outcomes,
        "time_complexity": spec.time_complexity,
        "space_complexity": spec.space_complexity,
        "optimal_approach": spec.optimal_approach,
        "function_name": spec.function_name,
        "function_signature": spec.function_signature,
        "parameters": spec.parameters,
        "starter_code": spec.starter_code,
    }


def _public_case(case: Any) -> dict[str, Any]:
    """A *visible* test case.

    Refuses a hidden one outright. The invariant "never called for a hidden case"
    is exactly the kind that survives until someone adds a route, and this is the
    one function standing between a hidden expected value and the response.
    """
    if getattr(case, "is_hidden", False):
        raise ValueError(
            "refusing to serialise a hidden test case: its expected value is "
            "the answer key"
        )
    return {
        "index": case.index,
        "args": case.args,
        "kwargs": case.kwargs,
        "expected": case.expected,
        "description": case.description,
    }


# ── Templates ───────────────────────────────────────────────────────

_TEMPLATE_CACHE: dict[str, str] = {}


def _render(request: Request, app: WebApp, template: str, context: dict[str, Any]) -> Response:
    """Minimal template rendering.

    Deliberately not a template engine. The pages need substitution and one
    loop each; pulling in Jinja would add a runtime dependency to a project whose
    zero-dependency property is load-bearing for the R2 student build.
    """
    from .webtemplate import render

    try:
        return Response(render(template, context), content_type="text/html; charset=utf-8")
    except FileNotFoundError as exc:
        raise NotFound(f"No template {template!r}.") from exc
