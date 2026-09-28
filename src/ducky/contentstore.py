"""SQLite content store for the practice server.

Holds the verified problem library in a form the web layer can query cheaply.
Kept separate from the upstream schema on purpose: the source database has 5,132
problems of which 2,529 are quarantined, and it stores content in four
unsynchronised ways. This store holds the whole library -- what has been
verified *and*, with its status, what has not, so a teacher-facing queue can show
the quarantined rows instead of the rows having simply vanished.

Slugs are the primary key because they are the only field that is both unique and
stable across all 7,371 source rows: 392 *titles* collide across the soft-delete
boundary, and problem ids get re-imported. See ``docs/UX_DECISIONS.md``.

What the store is allowed to answer
-----------------------------------
The web server is handed a ``read_only=True`` store and may read problems, test
cases, and hidden expectations -- it has to, since grading compares against the
hidden answers. It may not read reference solutions. That is enforced rather than
documented: :data:`_ANSWER_KEY_TABLES` are denied to the read-only connection by
a SQLite authorizer, so *no* query on that connection can return a solution, no
matter which future route or debug helper is written. A build tool opens the same
file read-write to get at solutions deliberately, and that asymmetry is the whole
design. See ``docs/CONTENT_BUILD.md``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

from .content import ContentStatus, ProblemSpec, classify_static

__all__ = ["ContentStore", "Verdict", "SCHEMA"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    slug                 TEXT PRIMARY KEY,
    source_id            INTEGER,
    title                TEXT NOT NULL,
    description          TEXT,
    difficulty           TEXT,
    difficulty_percent   INTEGER,
    topic_tier           INTEGER,
    function_name        TEXT,
    function_signature   TEXT,
    parameters           TEXT,      -- JSON array
    starter_code         TEXT,
    tags                 TEXT,      -- JSON array
    learning_outcomes    TEXT,      -- JSON array
    time_complexity      TEXT,
    space_complexity     TEXT,
    optimal_approach     TEXT,
    status               TEXT NOT NULL,
    defect               TEXT,
    status_detail        TEXT
);

CREATE TABLE IF NOT EXISTS test_cases (
    slug            TEXT NOT NULL REFERENCES problems(slug) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    args            TEXT NOT NULL,   -- JSON array
    kwargs          TEXT NOT NULL,   -- JSON object
    expected        TEXT,            -- JSON, null when unrecordable
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    description     TEXT,
    parse_error     TEXT,
    PRIMARY KEY (slug, idx)
);

-- Hidden expectations are the answer key for the hidden tests. They are kept in
-- a separate table so that reading the student-facing problem is a query that
-- structurally cannot return them, rather than a filter someone can forget. The
-- `expected` column of test_cases above is therefore left NULL for a hidden case;
-- keeping a second copy here would defeat the split.
CREATE TABLE IF NOT EXISTS hidden_expectations (
    slug    TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    expected TEXT,
    PRIMARY KEY (slug, idx)
);

-- Reference solutions. Stored because the repair and audit tools need them and
-- because dropping them at build time would make the source database the only
-- copy of the answer, but unreachable from the read-only connection the server
-- uses. See _ANSWER_KEY_TABLES.
CREATE TABLE IF NOT EXISTS solutions (
    slug          TEXT PRIMARY KEY,
    source_id     INTEGER,
    function_name TEXT,
    solution_code TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_problems_difficulty ON problems(difficulty);
CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status);
CREATE INDEX IF NOT EXISTS idx_cases_slug ON test_cases(slug);
"""

#: Tables a read-only :class:`ContentStore` may not read. The reference solution
#: is the answer to every problem, and a student with a code submission and an
#: access code is exactly the person who must not be able to read it. Denial is by
#: authorizer rather than by an absent column so that the guarantee survives a
#: ``SELECT *`` on ``problems`` growing a new column, or a new route reaching for
#: the table directly.
_ANSWER_KEY_TABLES = frozenset({"solutions"})


def _deny_answer_key(action: int, arg1: str | None, arg2: str | None,
                     db_name: str | None, trigger: str | None) -> int:
    """SQLite authorizer: refuse reads of the answer key, allow everything else.

    Only ``SQLITE_READ`` is denied. Schema introspection stays open, because a
    table *name* is not a secret and a tool that cannot see the schema cannot
    report on the store.
    """
    if action == sqlite3.SQLITE_READ and arg1 in _ANSWER_KEY_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class Verdict(NamedTuple):
    """One problem's build outcome, keyed by ``source_id`` when written.

    Status, detail, and defect travel together because a row with only some of
    them is not a decision: ``defect`` without ``status`` is a guess, and
    ``status`` without ``detail`` gives a teacher nothing to act on.
    """

    status: ContentStatus | str
    detail: str = ""
    defect: str = ""


class ContentStore:
    """Read-mostly store for the verified library.

    Opened read-only for the server: the web process never writes content, so a
    grading request cannot mutate the library it is grading against. Read-only
    also means the reference solutions are unreachable from this handle -- see
    :func:`_deny_answer_key`.
    """

    def __init__(self, path: str | Path, *, read_only: bool = True) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"no content database at {self.path}. Build one with "
                "`python tools/build_content.py`."
            )
        self._read_only = read_only
        uri = f"file:{self.path}?mode=ro" if read_only else f"file:{self.path}"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if read_only:
            self._conn.set_authorizer(_deny_answer_key)

    def close(self) -> None:
        self._conn.close()

    # -- reads --------------------------------------------------------

    def count(self, *, shippable_only: bool = True) -> int:
        if shippable_only:
            row = self._conn.execute(
                "SELECT COUNT(*) n FROM problems WHERE status = ?",
                (ContentStatus.VERIFIED.value,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) n FROM problems").fetchone()
        return int(row["n"])

    def list_problems(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: ContentStatus = ContentStatus.VERIFIED,
        topic: str = "",
        difficulty: str = "",
        search: str = "",
    ) -> list[dict[str, Any]]:
        """List browsable problems, filtered and paged in SQL.

        Paging in SQL rather than in the caller matters here. This used to take
        a bare `limit` with a default of 500, and the web layer sliced the result
        to 200 -- so of 2,603 problems a student could only ever reach 200, and
        the route had no way to know 2,403 more existed because the count was
        thrown away before it got there.
        """
        clauses = ["status = ?"]
        params: list[Any] = [status.value]
        if difficulty:
            clauses.append("difficulty = ?")
            params.append(difficulty)
        if topic:
            # tags is a JSON array, so match the quoted element.
            clauses.append("tags LIKE ?")
            params.append(f'%"{topic}"%')
        if search:
            clauses.append("LOWER(title) LIKE ?")
            params.append(f"%{search.lower()}%")
        params.extend([limit, offset])
        rows = self._conn.execute(
            f"""SELECT slug, title, difficulty, topic_tier, tags
               FROM problems WHERE {' AND '.join(clauses)}
               ORDER BY topic_tier, title LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [
            {
                "slug": r["slug"],
                "title": r["title"],
                "difficulty": r["difficulty"],
                "topic_tier": r["topic_tier"],
                "tags": _loads(r["tags"], []),
            }
            for r in rows
        ]

    def count_problems(
        self,
        *,
        status: ContentStatus = ContentStatus.VERIFIED,
        topic: str = "",
        difficulty: str = "",
        search: str = "",
    ) -> int:
        """How many problems match the same filters as :meth:`list_problems`.

        The total has to come from the database, not from ``len(list_problems())``
        -- the latter is the page size, and reporting it as the total is how a
        list ends up claiming "200 problems" when there are 2,603.
        """
        clauses = ["status = ?"]
        params: list[Any] = [status.value]
        if difficulty:
            clauses.append("difficulty = ?")
            params.append(difficulty)
        if topic:
            clauses.append("tags LIKE ?")
            params.append(f'%"{topic}"%')
        if search:
            clauses.append("LOWER(title) LIKE ?")
            params.append(f"%{search.lower()}%")
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM problems WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return int(row["n"])

    def topics(self) -> list[str]:
        """Distinct tags, most used first.

        Derived from the data rather than a curated list because none exists. The
        audit found 545 raw tag values of which 18% are metadata (`tier4`,
        `capstone`, `stabilized`) and that singular/plural pairs are *disjoint*
        because they come from two import batches -- so this is a starting point
        for a human to curate, not a finished taxonomy. See UX_DECISIONS.md.
        """
        counts: dict[str, int] = {}
        for row in self._conn.execute("SELECT tags FROM problems WHERE status = ?",
                                      (ContentStatus.VERIFIED.value,)):
            for tag in _loads(row["tags"], []):
                counts[tag] = counts.get(tag, 0) + 1
        return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def load_problem(self, slug: str) -> ProblemSpec | None:
        row = self._conn.execute("SELECT * FROM problems WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return None
        spec = ProblemSpec(
            slug=row["slug"],
            title=row["title"],
            description=row["description"] or "",
            difficulty=row["difficulty"] or "",
            difficulty_percent=row["difficulty_percent"],
            topic_tier=row["topic_tier"],
            tags=_loads(row["tags"], []),
            learning_outcomes=_loads(row["learning_outcomes"], []),
            time_complexity=row["time_complexity"] or "",
            space_complexity=row["space_complexity"] or "",
            optimal_approach=row["optimal_approach"] or "",
            function_name=row["function_name"] or "",
            function_signature=row["function_signature"] or "",
            parameters=_loads(row["parameters"], []),
            starter_code=row["starter_code"] or "",
            source_id=row["source_id"] or 0,
            status=ContentStatus(row["status"]),
            status_detail=row["status_detail"] or "",
        )
        for case in self._conn.execute(
            "SELECT * FROM test_cases WHERE slug = ? ORDER BY idx", (slug,)
        ):
            spec.test_cases.append(
                _case_from_row(slug, case, self._conn)
            )
        return spec

    def quarantined(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Problems kept out of the student UI, with why.

        Teacher-facing only. A student must not be able to reach a problem whose
        tests are broken: the outcome is being marked wrong for being right.
        """
        rows = self._conn.execute(
            """SELECT slug, title, status, defect, status_detail
               FROM problems WHERE status != ?
               ORDER BY status, slug LIMIT ?""",
            (ContentStatus.VERIFIED.value, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def table_counts(self) -> dict[str, int | None]:
        """Row count per table, read back from the file.

        A build report is only worth something if it describes the database that
        was actually written, so it reads the counts back rather than reporting
        the counts it intended to write.

        Answer-key tables report ``None`` on a read-only store, not ``0``: the
        authorizer refuses even ``COUNT(*)`` on them, and reporting a number this
        handle is not allowed to know would be worse than reporting that it does
        not know it.
        """
        names = ("problems", "test_cases", "hidden_expectations", "solutions")
        out: dict[str, int | None] = {}
        for name in names:
            if self._read_only and name in _ANSWER_KEY_TABLES:
                out[name] = None
                continue
            row = self._conn.execute(f"SELECT COUNT(*) n FROM {name}").fetchone()
            out[name] = int(row["n"])
        return out

    def reference_solution(self, slug: str) -> str:
        """The reference solution for a problem, or ``""``.

        Refused on a read-only store. The server has no legitimate reason to hold
        the answer, and this is the check that makes that a property of the object
        rather than a convention every caller has to remember.
        """
        if self._read_only:
            raise PermissionError(
                "a read-only ContentStore does not serve reference solutions; "
                "open the store with read_only=False from a teacher-side tool"
            )
        row = self._conn.execute(
            "SELECT solution_code FROM solutions WHERE slug = ?", (slug,)
        ).fetchone()
        return row["solution_code"] if row else ""


def _case_from_row(slug: str, row: sqlite3.Row, conn: sqlite3.Connection):
    from .content import TestCase

    is_hidden = bool(row["is_hidden"])
    expected = _loads(row["expected"], None)
    if is_hidden and expected is None:
        # Only reach into the answer-key table when there is a value to fetch.
        hidden = conn.execute(
            "SELECT expected FROM hidden_expectations WHERE slug = ? AND idx = ?",
            (slug, row["idx"]),
        ).fetchone()
        if hidden is not None:
            expected = _loads(hidden["expected"], None)
    return TestCase(
        index=row["idx"],
        args=_loads(row["args"], []),
        kwargs=_loads(row["kwargs"], {}),
        expected=expected,
        is_hidden=is_hidden,
        description=row["description"] or "",
        parse_error=row["parse_error"] or "",
    )


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def build_store(
    specs: list[ProblemSpec],
    path: str | Path,
    *,
    defects: dict[int, str] | None = None,
    verdicts: dict[int, Verdict] | None = None,
) -> ContentStore:
    """Write a content database. Used by tools/build_content.py, not the server.

    Every spec is written, not only the shippable ones: a quarantined problem that
    is dropped is a problem a teacher cannot see, cannot prioritise, and cannot
    tell apart from one that was never imported. The status is what separates
    them, and :meth:`ContentStore.quarantined` is what lists them.

    ``verdicts`` is the build's decision per problem, keyed by ``source_id``, and
    it wins over both the spec's own status and ``defects``. Without it the
    status is recomputed here by :func:`classify_static`, which is right for a
    hand-built fixture and wrong for a real build: ``classify_static`` leaves
    anything that needs running at ``UNVERIFIED``, so a store built without
    verdicts would file all 2,550 verified problems as unverified and serve an
    empty library. ``defects`` is kept for the callers that only pass a defect.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(str(target))
    conn.executescript(SCHEMA)
    defects = defects or {}
    for spec in specs:
        verdict = (verdicts or {}).get(spec.source_id)
        if verdict is None:
            classify_static(spec)
        else:
            spec.status = ContentStatus(verdict.status)
            spec.status_detail = verdict.detail
        conn.execute(
            """INSERT OR REPLACE INTO problems
               (slug, source_id, title, description, difficulty, difficulty_percent,
                topic_tier, function_name, function_signature, parameters,
                starter_code, tags, learning_outcomes, time_complexity,
                space_complexity, optimal_approach, status, defect, status_detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                spec.slug, spec.source_id, spec.title, spec.description,
                spec.difficulty, spec.difficulty_percent, spec.topic_tier,
                spec.function_name, spec.function_signature,
                json.dumps(spec.parameters), spec.starter_code,
                json.dumps(spec.tags), json.dumps(spec.learning_outcomes),
                spec.time_complexity, spec.space_complexity, spec.optimal_approach,
                spec.status.value, verdict.defect if verdict else defects.get(spec.source_id, ""),
                spec.status_detail,
            ),
        )
        if spec.solution_code:
            # Quarantined problems keep their solution too: it is the input the
            # repair tool needs, and withholding it would make the upstream
            # database the only copy of the answer.
            conn.execute(
                """INSERT OR REPLACE INTO solutions
                   (slug, source_id, function_name, solution_code) VALUES (?,?,?,?)""",
                (spec.slug, spec.source_id, spec.function_name, spec.solution_code),
            )
        for case in spec.test_cases:
            conn.execute(
                """INSERT OR REPLACE INTO test_cases
                   (slug, idx, args, kwargs, expected, is_hidden, description, parse_error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    spec.slug, case.index, json.dumps(case.args),
                    json.dumps(case.kwargs),
                    # A hidden case's expectation lives only in
                    # hidden_expectations. _case_from_row reads it back from
                    # there, so grading is unaffected and a query against
                    # test_cases cannot return an answer.
                    None if case.is_hidden else _dump(case.expected),
                    int(case.is_hidden), case.description, case.parse_error,
                ),
            )
            if case.is_hidden:
                conn.execute(
                    "INSERT OR REPLACE INTO hidden_expectations (slug, idx, expected) VALUES (?,?,?)",
                    (spec.slug, case.index, _dump(case.expected)),
                )
    conn.commit()
    conn.close()
    return ContentStore(target, read_only=False)


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return json.dumps(repr(value))
