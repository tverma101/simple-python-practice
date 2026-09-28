"""Tests for the content store and the tool that builds it.

Weighted towards the two properties that are expensive to get wrong and cheap to
lose: **the store must not serve the answer**, and **the store must agree with
the report that describes it**. A content store that quietly ships a stale status
or leaks a reference solution is worse than no store, because the practice server
has no way to tell.

The tool itself is exercised end to end here, against a small upstream-shaped
database, including running reference solutions in the sandbox. That is not
decoration: the reason this file exists is that the store was previously only
ever built by hand in a fixture, so nothing checked that the build tool could
build one.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from ducky.content import (
    ContentStatus,
    Defect,
    ProblemSpec,
    TestCase,
    classify_static,
    parse_test_case,
)
from ducky.contentstore import ContentStore, Verdict, build_store

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"

#: A solution body no student should ever be able to read out of the store.
SECRET = "return 1234  # the answer"


def _spec(slug: str = "hello", *, source_id: int = 1, verified: bool = True) -> ProblemSpec:
    return ProblemSpec(
        slug=slug,
        source_id=source_id,
        title="Say hello",
        description="Return a greeting.",
        difficulty="Beginner",
        function_name="greet",
        parameters=["name"],
        function_signature="def greet(name):",
        starter_code="def greet(name):\n    # YOUR CODE HERE\n    pass\n",
        solution_code=f"def greet(name):\n    {SECRET}\n",
        test_cases=[
            parse_test_case(0, '{"args": {"name": "Ada"}}', '"Hello, Ada!"'),
            parse_test_case(1, '{"args": {"name": "Bob"}}', '"Hello, Bob!"', is_hidden=True),
        ],
    )


# ── answer-key isolation ─────────────────────────────────────────────


class TestReferenceSolutionIsolation:
    """The server must not be able to read a reference solution. Prove it."""

    @pytest.fixture
    def store(self, tmp_path):
        build_store([_spec()], tmp_path / "content.db")
        return ContentStore(tmp_path / "content.db", read_only=True)

    def test_solution_is_actually_stored(self, tmp_path):
        """Otherwise the tests below would pass by the file simply lacking it."""
        build_store([_spec()], tmp_path / "content.db")
        raw = sqlite3.connect(f"file:{tmp_path / 'content.db'}?mode=ro", uri=True)
        try:
            row = raw.execute(
                "SELECT solution_code FROM solutions WHERE slug = 'hello'"
            ).fetchone()
        finally:
            raw.close()
        assert row is not None
        assert SECRET in row[0]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT solution_code FROM solutions",
            "SELECT * FROM solutions",
            "SELECT slug FROM solutions",
            "SELECT s.solution_code FROM solutions s JOIN problems p ON p.slug = s.slug",
            "SELECT * FROM problems p JOIN solutions s ON s.slug = p.slug",
            # Even an aggregate touches the table, so a count is a read too.
            "SELECT COUNT(*) FROM solutions",
        ],
    )
    def test_read_only_store_cannot_query_solutions(self, store, sql):
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(sql).fetchall()

    def test_read_only_store_has_no_api_to_fetch_a_solution(self, store):
        with pytest.raises(PermissionError):
            store.reference_solution("hello")

    def test_loaded_problem_carries_no_solution(self, store):
        """`load_problem` is what the routes call, so it must not fill the field."""
        spec = store.load_problem("hello")
        assert spec is not None
        assert spec.solution_code == ""

    def test_no_column_anywhere_on_the_student_facing_path_holds_it(self, store):
        rows = store._conn.execute(
            "SELECT * FROM problems WHERE slug = 'hello'"
        ).fetchall()
        for row in rows:
            assert SECRET not in " ".join(str(v) for v in tuple(row))
        cases = store._conn.execute(
            "SELECT * FROM test_cases WHERE slug = 'hello'"
        ).fetchall()
        for row in cases:
            assert SECRET not in " ".join(str(v) for v in tuple(row))

    def test_writer_handle_can_still_reach_it(self, tmp_path):
        """The asymmetry is the point: teacher-side tools get the answer."""
        build_store([_spec()], tmp_path / "content.db")
        writer = ContentStore(tmp_path / "content.db", read_only=False)
        try:
            assert SECRET in writer.reference_solution("hello")
        finally:
            writer.close()

    def test_missing_solution_reads_as_empty_not_an_error(self, tmp_path):
        build_store([_spec()], tmp_path / "content.db")
        writer = ContentStore(tmp_path / "content.db", read_only=False)
        try:
            assert writer.reference_solution("nope") == ""
        finally:
            writer.close()

    def test_table_counts_refuses_to_guess_the_answer_key(self, store):
        counts = store.table_counts()
        assert counts["problems"] == 1
        # None, not 0: this handle is not allowed to know, and saying 0 would be
        # a claim it cannot support.
        assert counts["solutions"] is None


# ── hidden expectations ───────────────────────────────────────────────


class TestHiddenExpectationsLiveOnlyInTheAnswerKey:
    def test_hidden_expected_is_not_inlined_in_test_cases(self, tmp_path):
        build_store([_spec()], tmp_path / "content.db")
        row = sqlite3.connect(
            f"file:{tmp_path / 'content.db'}?mode=ro", uri=True
        ).execute(
            "SELECT expected FROM test_cases WHERE slug = 'hello' AND is_hidden = 1"
        ).fetchone()
        assert row is not None
        assert row[0] is None, "the hidden answer must not be a second copy in test_cases"

    def test_hidden_expected_is_still_readable_for_grading(self, tmp_path):
        """Splitting the answer key must not break the grader."""
        build_store([_spec()], tmp_path / "content.db")
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            hidden = [tc for tc in store.load_problem("hello").test_cases if tc.is_hidden]
        finally:
            store.close()
        assert hidden and hidden[0].expected == "Hello, Bob!"

    def test_visible_expected_stays_inline(self, tmp_path):
        build_store([_spec()], tmp_path / "content.db")
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            visible = [tc for tc in store.load_problem("hello").test_cases if not tc.is_hidden]
        finally:
            store.close()
        assert visible and visible[0].expected == "Hello, Ada!"


# ── verdicts ─────────────────────────────────────────────────────────


class TestVerdictsAreStoredNotRecomputed:
    def test_verified_status_survives_the_write(self, tmp_path):
        """`build_store` re-runs classify_static, which would undo a verified row.

        classify_static can only ever return UNVERIFIED for a problem that needs
        running, so a store built without the build's verdicts would file every
        verified problem as unverified and serve an empty library.
        """
        spec = _spec()
        classify_static(spec)
        assert spec.status is ContentStatus.UNVERIFIED, "precondition"

        build_store(
            [spec],
            tmp_path / "content.db",
            verdicts={spec.source_id: Verdict(ContentStatus.VERIFIED, "2/2 cases pass", "none")},
        )
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            assert store.count() == 1
            assert store.list_problems()[0]["slug"] == "hello"
            assert store.quarantined() == []
        finally:
            store.close()

    def test_defect_and_detail_travel_with_the_status(self, tmp_path):
        spec = _spec("broken")
        build_store(
            [spec],
            tmp_path / "content.db",
            verdicts={
                spec.source_id: Verdict(
                    ContentStatus.STARTER_BROKEN, "starter_code does not parse", "starter_unparseable"
                )
            },
        )
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            row = store.quarantined()[0]
        finally:
            store.close()
        assert row == {
            "slug": "broken",
            "title": "Say hello",
            "status": "starter_broken",
            "defect": "starter_unparseable",
            "status_detail": "starter_code does not parse",
        }

    def test_defect_dict_still_works_on_its_own(self, tmp_path):
        """The pre-existing call shape must keep working."""
        spec = _spec()
        build_store([spec], tmp_path / "content.db", defects={spec.source_id: "row_unparseable"})
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            assert store.quarantined()[0]["defect"] == "row_unparseable"
        finally:
            store.close()

    def test_nothing_is_dropped(self, tmp_path):
        specs = [_spec("a", source_id=1), _spec("b", source_id=2), _spec("c", source_id=3)]
        build_store(
            specs,
            tmp_path / "content.db",
            verdicts={
                1: Verdict(ContentStatus.VERIFIED, "ok", "none"),
                2: Verdict(ContentStatus.SOLUTION_DISAGREES, "1/2 disagree", "mismatch"),
                3: Verdict(ContentStatus.NO_SOLUTION, "no reference solution", "row_unparseable"),
            },
        )
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            assert store.count(shippable_only=False) == 3
            assert store.count() == 1
            assert len(store.quarantined()) == 2
        finally:
            store.close()

    def test_a_plain_tuple_verdict_is_accepted(self, tmp_path):
        """Verdict is a NamedTuple, so unpacking into it is the other call shape."""
        status, detail, defect = Verdict(
            ContentStatus.VERIFIED, "2/2", "none"
        )
        spec = _spec()
        build_store(
            [spec],
            tmp_path / "content.db",
            verdicts={spec.source_id: Verdict(status, detail, defect)},
        )
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            assert store.count() == 1
        finally:
            store.close()

    def test_a_spec_with_no_verdict_falls_back_to_static_classification(self, tmp_path):
        """The legacy path: no verdict means "decide it here", which is UNVERIFIED."""
        spec = _spec("nostarter")
        spec.solution_code = ""
        build_store([spec], tmp_path / "content.db", verdicts={})
        store = ContentStore(tmp_path / "content.db", read_only=True)
        try:
            row = store.quarantined()[0]
        finally:
            store.close()
        assert row["status"] == "no_solution"


# ── idempotency ──────────────────────────────────────────────────────


class TestIdempotent:
    def test_building_twice_gives_an_identical_file(self, tmp_path):
        def build(path: Path) -> bytes:
            specs = [
                _spec("a", source_id=1),
                _spec("b", source_id=2),
                _spec("c", source_id=3),
            ]
            build_store(
                specs,
                path,
                verdicts={
                    1: Verdict(ContentStatus.VERIFIED, "2/2 cases pass", "none"),
                    2: Verdict(ContentStatus.SOLUTION_ERRORS, "raised", "solution_broken"),
                    3: Verdict(ContentStatus.STARTER_BROKEN, "does not parse", "starter_unparseable"),
                },
            )
            return path.read_bytes()

        first = build(tmp_path / "one.db")
        second = build(tmp_path / "two.db")
        assert first == second

    def test_rebuilding_over_an_existing_file_is_not_an_append(self, tmp_path):
        path = tmp_path / "content.db"
        build_store([_spec("a", source_id=1)], path)
        build_store([_spec("b", source_id=2)], path)
        store = ContentStore(path, read_only=True)
        try:
            slugs = [p["slug"] for p in store.list_problems(status=ContentStatus.UNVERIFIED)]
        finally:
            store.close()
        assert slugs == ["b"]


# ── the build tool ───────────────────────────────────────────────────


def _load_tool(name: str):
    """Import a module from tools/ by path, the way the script is invoked."""
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _upstream_db(path: Path, rows: list[dict]) -> Path:
    """A minimal database shaped like coding_platform, for iter_problems."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE coding_problems (
            id INTEGER PRIMARY KEY, title TEXT, slug TEXT, description TEXT,
            difficulty TEXT, difficulty_percent INTEGER, leetcode_difficulty TEXT,
            unit INTEGER, lesson INTEGER, topic_tier INTEGER, tags TEXT,
            learning_outcomes TEXT, time_complexity TEXT, space_complexity TEXT,
            optimal_approach TEXT, interview_frequency TEXT, companies TEXT,
            function_signature TEXT, starter_code TEXT, solution TEXT, is_active INTEGER
        );
        CREATE TABLE test_cases (
            id INTEGER PRIMARY KEY, problem_id INTEGER, input_data TEXT,
            expected_output TEXT, is_hidden INTEGER, description TEXT, "order" INTEGER
        );
        """
    )
    for row in rows:
        conn.execute(
            """INSERT INTO coding_problems
               (id, title, slug, description, difficulty, difficulty_percent,
                leetcode_difficulty, unit, lesson, topic_tier, tags,
                learning_outcomes, time_complexity, space_complexity, optimal_approach,
                interview_frequency, companies, function_signature, starter_code,
                solution, is_active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], row["title"], row["slug"], row.get("description", ""),
                row.get("difficulty", "Beginner"), row.get("difficulty_percent", 10),
                "", 1, 1, row.get("topic_tier", 1), "[]", "[]", "", "", "", "", "[]",
                row.get("signature", ""), row.get("starter", ""), row.get("solution", ""),
                1,
            ),
        )
        for order, (inp, out, hidden) in enumerate(row["cases"]):
            conn.execute(
                """INSERT INTO test_cases
                   (problem_id, input_data, expected_output, is_hidden, description, "order")
                   VALUES (?,?,?,?,?,?)""",
                (row["id"], inp, out, int(hidden), f"Test {order + 1}", order),
            )
    conn.commit()
    conn.close()
    return path


#: Two problems: one whose reference solution is right, one whose starter does
#: not parse. Enough to exercise a verified row, a quarantined row, a hidden
#: expectation, and the static triage path in one build.
_ROWS = [
    {
        "id": 11,
        "title": "Add two numbers",
        "slug": "add-two",
        "signature": "def add_two(a, b):",
        "starter": "def add_two(a, b):\n    # YOUR CODE HERE\n    pass\n",
        "solution": "def add_two(a, b):\n    return a + b\n",
        "cases": [
            ('{"args": {"a": 1, "b": 2}}', "3", False),
            ('{"args": {"a": -1, "b": 1}}', "0", True),
        ],
    },
    {
        "id": 12,
        "title": "Broken starter",
        "slug": "broken-starter",
        "signature": "def whatever(x):",
        "starter": 'def whatever(x):\n    """never closed\n',
        "solution": "def whatever(x):\n    return x\n",
        "cases": [('{"args": {"x": 1}}', "1", False)],
    },
]


@pytest.fixture
def upstream(tmp_path):
    return _upstream_db(tmp_path / "coding_platform.db", _ROWS)


def _run_build(upstream: Path, out: Path, report: Path, *extra: str) -> dict:
    tool = _load_tool("build_content")
    argv = [
        "build_content.py", "--db", str(upstream), "--out", str(out),
        "--report", str(report), "--quiet", *extra,
    ]
    saved = sys.argv
    sys.argv = argv
    try:
        assert tool.main() == 0, "the build's own audit rejected the database it wrote"
    finally:
        sys.argv = saved
    return json.loads(report.read_text())


class TestBuildTool:
    def test_builds_a_store_the_server_can_serve(self, upstream, tmp_path):
        out = tmp_path / "content.db"
        report = _run_build(upstream, out, tmp_path / "build.json")

        assert report["totals"] == {
            "problems": 2, "shipped": 1, "quarantined": 1,
            "shipped_percent": 50.0, "executed": 1,
        }
        store = ContentStore(out, read_only=True)
        try:
            assert store.count() == 1
            assert [p["slug"] for p in store.list_problems()] == ["add-two"]
            assert [q["slug"] for q in store.quarantined()] == ["broken-starter"]
        finally:
            store.close()

    def test_only_verified_ships(self, upstream, tmp_path):
        """The starter-broken problem must be present but not listed to students."""
        out = tmp_path / "content.db"
        _run_build(upstream, out, tmp_path / "build.json")
        store = ContentStore(out, read_only=True)
        try:
            assert store.count(shippable_only=False) == 2
            assert store.count() == 1
            assert store.quarantined()[0]["defect"] == Defect.STARTER_UNPARSEABLE.value
        finally:
            store.close()

    def test_the_report_and_the_database_agree(self, upstream, tmp_path):
        out = tmp_path / "content.db"
        report = _run_build(upstream, out, tmp_path / "build.json")
        # Counted through a read-write handle, like the build's own: a read-only
        # one is refused the answer key and reports None for it.
        builder = ContentStore(out, read_only=False)
        try:
            rows = builder.table_counts()
        finally:
            builder.close()
        assert report["counts"]["problems"] == rows["problems"] == 2
        assert report["counts"]["test_cases"] == rows["test_cases"] == 3
        assert report["counts"]["hidden_cases"] == rows["hidden_expectations"] == 1
        assert report["counts"]["solutions"] == rows["solutions"] == 2
        assert report["totals"]["shipped"] + report["totals"]["quarantined"] == 2

    def test_built_store_keeps_the_reference_solution_out_of_reach(self, upstream, tmp_path):
        out = tmp_path / "content.db"
        _run_build(upstream, out, tmp_path / "build.json")
        store = ContentStore(out, read_only=True)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                store._conn.execute("SELECT solution_code FROM solutions").fetchall()
        finally:
            store.close()
        writer = ContentStore(out, read_only=False)
        try:
            assert "return a + b" in writer.reference_solution("add-two")
        finally:
            writer.close()

    def test_hidden_answer_is_graded_but_not_served(self, upstream, tmp_path):
        out = tmp_path / "content.db"
        _run_build(upstream, out, tmp_path / "build.json")
        store = ContentStore(out, read_only=True)
        try:
            spec = store.load_problem("add-two")
        finally:
            store.close()
        assert [tc.expected for tc in spec.test_cases if tc.is_hidden] == [0]

    def test_running_twice_gives_the_same_database_and_report(self, upstream, tmp_path):
        first, second = tmp_path / "one.db", tmp_path / "two.db"
        report_a = _run_build(upstream, first, tmp_path / "a.json")
        report_b = _run_build(upstream, second, tmp_path / "b.json")
        assert first.read_bytes() == second.read_bytes()
        # Everything but the run block is a property of the content, not the clock.
        del report_a["run"], report_b["run"]
        assert report_a == report_b
        assert report_a["fingerprint"] == report_b["fingerprint"]

    def test_limit_marks_the_run_partial(self, upstream, tmp_path):
        report = _run_build(upstream, tmp_path / "content.db", tmp_path / "b.json", "--limit", "0")
        assert report["run"]["partial"] is False
        # A limit leaves the unexecuted problems UNVERIFIED, which must not ship.
        report = _run_build(
            upstream, tmp_path / "content.db", tmp_path / "b.json", "--limit", "1", "--jobs", "1"
        )
        assert report["totals"]["shipped"] == 1

    def test_missing_database_is_an_error_not_a_crash(self, tmp_path, capsys):
        tool = _load_tool("build_content")
        saved = sys.argv
        sys.argv = ["build_content.py", "--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o.db")]
        try:
            assert tool.main() == 2
        finally:
            sys.argv = saved
        assert "no such database" in capsys.readouterr().err


class TestBuildToolAgreesWithTheVerificationReport:
    """The build and tools/verify_content.py must not drift apart.

    They read the same database through the same verifier, so any difference in
    their numbers is a bug in one of them -- and the numbers are what a teacher
    uses to decide what to fix. This is the same failure the project has already
    had once, when the repair tool's idea of a placeholder and the verifier's
    diverged and the tool quietly did nothing.
    """

    def test_static_defect_rule_is_shared_not_copied(self):
        """Imported from verify_content, so the two cannot drift."""
        build = _load_tool("build_content")
        shared = sys.modules["verify_content"]
        assert build._static_defect is shared._static_defect
        # The function was defined over there, not here.
        assert build._static_defect.__module__ == "verify_content"

    @pytest.mark.parametrize("status", sorted(ContentStatus))
    def test_static_defect_rule_behaviour(self, status):
        """The rule itself, spelled out, so a change to it is a deliberate diff."""
        rule = _load_tool("build_content")._static_defect
        if status is ContentStatus.STARTER_BROKEN:
            assert rule(status) == Defect.STARTER_UNPARSEABLE.value
        else:
            assert rule(status) == Defect.ROW_UNPARSEABLE.value
        # And it stays inside the Defect vocabulary, so the report's breakdown
        # cannot grow a value the rest of the codebase does not understand.
        assert Defect(rule(status)) is not None

    def test_executed_defects_come_from_the_verifier(self):
        """Not from a second copy of the rule in the tool."""
        import inspect

        build = _load_tool("build_content")
        source = inspect.getsource(build)
        assert "executed.defect" in source
        assert "_classify_defect" not in source, (
            "the tool must take the defect from the VerificationResult, not "
            "re-derive it, or the store and the report can disagree"
        )


class TestFingerprint:
    def test_ignores_the_run_block(self):
        tool = _load_tool("build_content")
        base = {"totals": {"shipped": 1}, "run": {"generated_at": "a", "elapsed_seconds": 1.0}}
        other = {"totals": {"shipped": 1}, "run": {"generated_at": "b", "elapsed_seconds": 9.9}}
        assert tool._fingerprint(base) == tool._fingerprint(other)

    def test_changes_when_the_content_changes(self):
        tool = _load_tool("build_content")
        base = {"totals": {"shipped": 1}}
        other = {"totals": {"shipped": 2}}
        assert tool._fingerprint(base) != tool._fingerprint(other)


# ── sanity on the fixtures themselves ─────────────────────────────────


def test_fixture_spec_is_actually_verified():
    """Guards the two isolation tests above from passing on a broken premise."""
    from ducky.verify import verify_problem

    spec = _spec()
    spec.test_cases[0].expected = 1234
    spec.test_cases[1].expected = 1234
    result = verify_problem(spec)
    assert result.status is ContentStatus.VERIFIED
    assert result.shippable


def test_test_case_defaults_are_unchanged(tmp_path):
    """A zero-argument case is usable, and build_store must not alter it.

    This used to assert ``not case.usable``, which was the bug: a zero-argument
    function like ``def to_uppercase() -> str:`` is legitimately called with no
    arguments, and demanding non-empty args made 920 cases ungradeable. The
    load-bearing part of the test is the round trip below -- build_store must
    not invent or normalise anything -- so that is what it checks now.
    """
    case = TestCase(index=0, expected=1)
    assert case.usable
    build_store(
        [ProblemSpec(slug="p", title="P", solution_code="", test_cases=[case])],
        tmp_path / "content.db",
    ).close()
    store = ContentStore(tmp_path / "content.db", read_only=True)
    try:
        round_tripped = store.load_problem("p").test_cases
        assert [tc.expected for tc in round_tripped] == [1]
        assert [tc.args for tc in round_tripped] == [[]]
        assert [tc.kwargs for tc in round_tripped] == [{}]
    finally:
        store.close()


def test_unparseable_case_is_still_unusable():
    """The narrower rule still holds: a real parse error is not runnable."""
    assert not TestCase(index=0, expected=1, parse_error="bad json").usable
    assert not TestCase(index=0, expected=None, parse_error="no expected").usable
