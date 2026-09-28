"""Tests for the curriculum graph and the "what should I do next" algorithm.

The fixtures here are miniature but they copy the *shapes* the real database
uses, because the shapes are the problem: ``prerequisites`` holds three
incompatible formats, ``lessons.id`` interleaves across units so it is not a
valid topological order, and the thin lessons are what the ``MIN_CONTENT`` floor
exists for. A fixture that used clean data would pass while the loader stayed
broken.

The numbers quoted in the docstrings are re-derived from the source database by
``tools/curriculum_report.py``; see ``docs/UX_DECISIONS.md`` section 5.
"""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

from ducky.content import ContentStatus, ProblemSpec, TestCase
from ducky.contentstore import SCHEMA as CONTENT_SCHEMA
from ducky.contentstore import build_store
from ducky.curriculum import (
    MIN_CONTENT,
    Curriculum,
    Lesson,
    PoolEntry,
    build_curriculum,
    decode_prerequisites,
    has_canonical_topic,
    is_complete,
    is_vague_title,
    lesson_key,
    practice_score,
    problem_state,
    recommend_next,
)

# ── Fixtures ────────────────────────────────────────────────────────

SOURCE_SCHEMA = """
CREATE TABLE lessons (
    id                 INTEGER NOT NULL PRIMARY KEY,
    unit_num           INTEGER NOT NULL,
    lesson_num         INTEGER NOT NULL,
    title              VARCHAR(200) NOT NULL,
    slug               VARCHAR(200) NOT NULL,
    description        TEXT,
    teaching_content   TEXT,
    functions_taught   TEXT,
    tier               INTEGER,
    unlocks_at_mastery FLOAT,
    prerequisites      TEXT,
    estimated_minutes  INTEGER,
    difficulty_level   VARCHAR(20),
    is_active          BOOLEAN,
    UNIQUE (unit_num, lesson_num)
);

CREATE TABLE lesson_problems (
    id             INTEGER NOT NULL PRIMARY KEY,
    lesson_id      INTEGER NOT NULL,
    problem_id     INTEGER NOT NULL,
    variation_type VARCHAR(30),
    display_order  INTEGER,
    UNIQUE (lesson_id, problem_id)
);

CREATE TABLE coding_problems (
    id           INTEGER NOT NULL PRIMARY KEY,
    title        VARCHAR(200) NOT NULL,
    slug         VARCHAR(200) NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    difficulty   VARCHAR(20) NOT NULL DEFAULT 'Beginner',
    topic_tier   INTEGER,
    tags         TEXT,
    is_active    BOOLEAN
);
"""


def make_source_db(
    lessons: list[dict], problems: list[dict] | None = None, links: list[tuple] | None = None
) -> sqlite3.Connection:
    """A source database with the real column names and the real quirks.

    ``lessons`` entries are dicts of column values; ``prerequisites`` is stored
    verbatim, so a test can put ``"[1]"`` or ``"110"`` or ``None`` in it and get
    the same decoding path the live data takes.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SOURCE_SCHEMA)
    for lesson in lessons:
        columns = (
            "id", "unit_num", "lesson_num", "title", "slug", "description",
            "tier", "unlocks_at_mastery", "prerequisites", "estimated_minutes",
            "difficulty_level", "is_active",
        )
        conn.execute(
            f"INSERT INTO lessons ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            tuple(lesson.get(column) for column in columns),
        )
    for problem in problems or []:
        conn.execute(
            "INSERT INTO coding_problems (id, title, slug, topic_tier, tags, is_active) "
            "VALUES (?,?,?,?,?,?)",
            (
                problem["id"],
                problem.get("title", problem["slug"]),
                problem["slug"],
                problem.get("topic_tier", 2),
                json.dumps(problem.get("tags", ["loops"])),
                problem.get("is_active", 1),
            ),
        )
    for lesson_id, problem_id, order in links or []:
        conn.execute(
            "INSERT INTO lesson_problems (lesson_id, problem_id, display_order) VALUES (?,?,?)",
            (lesson_id, problem_id, order),
        )
    conn.commit()
    return conn


def lesson_row(
    lesson_id: int,
    unit: int,
    number: int,
    *,
    prerequisites: str | None = None,
    active: int = 1,
    title: str | None = None,
) -> dict:
    return {
        "id": lesson_id,
        "unit_num": unit,
        "lesson_num": number,
        "title": title or f"unit {unit} lesson {number}",
        "slug": f"u{unit}-l{number}",
        "description": "",
        "tier": 1,
        "unlocks_at_mastery": 0.6,
        "prerequisites": prerequisites,
        "estimated_minutes": 30,
        "difficulty_level": "beginner",
        "is_active": active,
    }


def add_problems(
    conn: sqlite3.Connection,
    lesson_id: int,
    slugs: list[str],
    *,
    first_id: int | None = None,
    start_order: int = 1,
    titles: dict[str, str] | None = None,
) -> list[str]:
    """Link problems to a lesson. ``first_id`` keeps repeat calls from colliding."""
    base = lesson_id * 1000 if first_id is None else first_id
    for offset, slug in enumerate(slugs):
        conn.execute(
            "INSERT INTO coding_problems (id, title, slug, topic_tier, tags, is_active) "
            "VALUES (?,?,?,?,?,1)",
            (
                base + offset,
                (titles or {}).get(slug, slug.replace("-", " ").title()),
                slug,
                2,
                json.dumps(["loops", "tier2"]),
            ),
        )
        conn.execute(
            "INSERT INTO lesson_problems (lesson_id, problem_id, display_order) VALUES (?,?,?)",
            (lesson_id, base + offset, start_order + offset),
        )
    conn.commit()
    return list(slugs)


#: A graph that reproduces the real database's shape: unit 1 chains by slug,
#: unit 2 chains by unit-local lesson_num inside a JSON int array, unit 4 chains
#: by ``lessons.id`` and points at a lesson number that exists nowhere.
REAL_SHAPES = [
    lesson_row(139, 1, 1, prerequisites="[]"),
    lesson_row(140, 1, 2, prerequisites='["u1-l1"]'),
    lesson_row(141, 1, 3, prerequisites='["u1-l2"]'),
    lesson_row(151, 2, 1, prerequisites="[]"),
    lesson_row(177, 2, 2, prerequisites="[1]"),
    lesson_row(178, 2, 3, prerequisites=None),
    lesson_row(179, 2, 4, prerequisites="[2]"),
    lesson_row(180, 2, 5, prerequisites="[4]"),
    lesson_row(133, 4, 1, prerequisites="[114]"),
    lesson_row(134, 4, 2, prerequisites="[133]"),
    lesson_row(135, 4, 3, prerequisites="[134]"),
    lesson_row(50, 4, 8, prerequisites="110"),
    lesson_row(138, 5, 7, prerequisites="[55]", active=0),
    lesson_row(55, 5, 6, prerequisites="[]"),
    lesson_row(48, 6, 1, prerequisites="[138]"),
    lesson_row(119, 9, 1, prerequisites=None),
    lesson_row(120, 9, 2, prerequisites="119"),
]


@pytest.fixture
def real_shapes_conn() -> sqlite3.Connection:
    return make_source_db(REAL_SHAPES)


@pytest.fixture
def real_shapes(real_shapes_conn: sqlite3.Connection) -> Curriculum:
    return Curriculum.from_source(real_shapes_conn, verified=set())


# ── The three prerequisite formats ───────────────────────────────────


class TestPrerequisiteFormats:
    def test_json_string_array_resolves_by_slug(self, real_shapes):
        lesson = real_shapes.lesson("u1l2")
        assert lesson.prerequisites == ("u1l1",)
        assert [e.resolved_by for e in real_shapes.edges if e.child == "u1l2"] == ["slug"]

    def test_json_int_array_resolves_by_id(self, real_shapes):
        # [133] is a JSON int array and it names lessons.id, not a lesson number.
        assert real_shapes.lesson("u4l2").prerequisites == ("u4l1",)
        edge = next(e for e in real_shapes.edges if e.child == "u4l2")
        assert (edge.raw, edge.resolved_by) == ("133", "id")

    def test_json_int_array_falls_back_to_unit_local_lesson_num(self, real_shapes):
        # [1] in unit 2: there is no lesson with id 1 anywhere, and there is a
        # lesson numbered 1 in unit 2. All 13 tokens that need this reading are
        # of this shape, not of the bare-int shape the audit assumed.
        assert real_shapes.lesson("u2l2").prerequisites == ("u2l1",)
        edge = next(e for e in real_shapes.edges if e.child == "u2l2")
        assert (edge.raw, edge.resolved_by) == ("1", "lesson_num")

    def test_bare_int_resolves_by_id(self, real_shapes):
        assert real_shapes.lesson("u9l2").prerequisites == ("u9l1",)
        edge = next(e for e in real_shapes.edges if e.child == "u9l2")
        assert (edge.raw, edge.resolved_by) == ("119", "id")

    def test_id_wins_when_both_readings_would_match(self):
        # A deliberate tie-break, because a token can in principle be a valid id
        # *and* a valid unit-local lesson number. The id is the global key, so it
        # wins; the alternative is tested by the unit-local case above.
        conn = make_source_db(
            [
                lesson_row(1, 2, 7, prerequisites="[]"),
                lesson_row(200, 3, 1, prerequisites="[1]"),
            ]
        )
        curriculum = Curriculum.from_source(conn)
        assert curriculum.lesson("u3l1").prerequisites == ("u2l7",)
        assert next(iter(curriculum.edges)).resolved_by == "id"

    def test_soft_deleted_parent_is_an_edge_that_gates_nothing(self, real_shapes):
        # u6l1 requires u5l7, which is is_active = 0. It resolved, so it is an
        # edge; it is not a prerequisite, so it cannot strand the lesson.
        lesson = real_shapes.lesson("u6l1")
        assert lesson.inactive_prerequisites == ("u5l7",)
        assert lesson.prerequisites == ()
        edge = next(e for e in real_shapes.edges if e.child == "u6l1")
        assert edge.parent_active is False
        assert real_shapes.prerequisites_met("u6l1")

    def test_unresolvable_tokens_are_retained_and_labelled(self, real_shapes):
        by_child = {item.child: item for item in real_shapes.unresolved}
        assert set(by_child) == {"u4l1", "u4l8"}
        assert by_child["u4l8"].raw == "110"
        assert "no lesson has id 110" in by_child["u4l8"].reason
        assert "no lesson is numbered 110 in unit 4" in by_child["u4l8"].reason
        assert by_child["u4l1"].raw == "114"
        # The lesson still exists and is still walkable; only the edge is missing.
        assert real_shapes.lesson("u4l1").unresolved == (by_child["u4l1"],)
        assert real_shapes.lesson("u4l1") in real_shapes.roots()

    def test_unresolved_slug_is_reported_not_dropped(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites='["no-such-lesson"]')])
        curriculum = Curriculum.from_source(conn)
        (item,) = curriculum.unresolved
        assert item.reason == "no lesson has slug 'no-such-lesson'"
        assert curriculum.edges == ()

    def test_duplicate_token_is_one_edge(self):
        conn = make_source_db(
            [lesson_row(1, 1, 1), lesson_row(2, 1, 2, prerequisites="[1, 1]")]
        )
        curriculum = Curriculum.from_source(conn)
        assert curriculum.lesson("u1l2").prerequisites == ("u1l1",)
        assert len(curriculum.edges) == 1


class TestPrerequisiteDecoding:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, ()),
            ("", ()),
            ("   ", ()),
            ("[]", ()),
            ("110", (("110", 110, "number", "bare-int"),)),
            (" 42 ", (("42", 42, "number", "bare-int"),)),
            ('["a-b"]', (('"a-b"', "a-b", "slug", "json-array"),)),
            ("[7]", (("7", 7, "number", "json-array"),)),
            ('[7, "a-b"]', (("7", 7, "number", "json-array"), ('"a-b"', "a-b", "slug", "json-array"))),
        ],
    )
    def test_readable_shapes(self, raw, expected):
        tokens = decode_prerequisites(raw)
        assert tuple((t.raw, t.value, t.kind, t.shape) for t in tokens) == expected
        assert all(t.readable for t in tokens)

    @pytest.mark.parametrize(
        ("raw", "shape"),
        [
            ("[1,", "json-broken"),
            ("not a number", "bare-text"),
            ('{"lesson": 1}', "json-scalar"),
            ('"u1-l1"', "json-scalar"),
            ("[1.5]", "json-array"),
            ("[true]", "json-array"),
            ("[null]", "json-array"),
            ("[[1]]", "json-array"),
        ],
    )
    def test_unreadable_shapes_are_kept_as_one_token(self, raw, shape):
        tokens = decode_prerequisites(raw)
        assert len(tokens) == 1
        assert tokens[0].shape == shape
        assert not tokens[0].readable
        assert tokens[0].value is None

    def test_boolean_is_not_lesson_one(self):
        # bool is a subclass of int in Python, so this is a real trap.
        (token,) = decode_prerequisites("[true]")
        assert token.kind == "unreadable"

    def test_float_is_not_lesson_one(self):
        (token,) = decode_prerequisites("[1.0]")
        assert token.kind == "unreadable"


# ── Cycles ──────────────────────────────────────────────────────────


class TestCycleDetection:
    def test_two_cycle_is_reported_and_excluded_from_the_order(self):
        conn = make_source_db(
            [
                lesson_row(1, 1, 1, prerequisites="[2]"),
                lesson_row(2, 1, 2, prerequisites="[1]"),
                lesson_row(3, 2, 1, prerequisites="[]"),
            ]
        )
        curriculum = Curriculum.from_source(conn)
        assert curriculum.cycles == (("u1l1", "u1l2"),)
        assert curriculum.order == ("u2l1",)
        # The cyclic lessons are not lost: a lesson nobody can reach is still a
        # lesson a student can see.
        assert len(curriculum.lessons) == 3
        assert {lesson.key for lesson in curriculum.lessons} == {"u1l1", "u1l2", "u2l1"}
        assert curriculum.lesson("u1l1").depth == 0

    def test_self_reference_is_a_cycle_of_one(self):
        conn = make_source_db([lesson_row(7, 1, 1, prerequisites="[7]")])
        curriculum = Curriculum.from_source(conn)
        assert curriculum.cycles == (("u1l1",),)
        assert curriculum.stats["self_references"] == 1
        assert curriculum.order == ()
        # The edge is retained rather than quietly deleted: a self-referencing
        # prerequisite is real data damage, and hiding it hides the fix.
        assert len(curriculum.edges) == 1
        edge = curriculum.edges[0]
        assert (edge.child, edge.parent, edge.raw, edge.resolved_by) == (
            "u1l1",
            "u1l1",
            "7",
            "id",
        )

    def test_acyclic_graph_reports_no_cycles(self, real_shapes):
        assert real_shapes.cycles == ()
        assert real_shapes.stats["cycles"] == []

    def test_three_cycle_is_found_once(self):
        conn = make_source_db(
            [
                lesson_row(1, 1, 1, prerequisites="[2]"),
                lesson_row(2, 1, 2, prerequisites="[3]"),
                lesson_row(3, 1, 3, prerequisites="[1]"),
            ]
        )
        curriculum = Curriculum.from_source(conn)
        assert curriculum.cycles == (("u1l1", "u1l2", "u1l3"),)

    def test_a_unit_local_token_pointing_at_its_own_lesson_is_a_self_cycle(self):
        # The trap the two numeric readings leave open: [4] in a unit whose
        # lesson 4 is the lesson holding it. It resolves -- to itself -- so a
        # decoder that only checked "did it resolve" would call it fine.
        conn = make_source_db(
            [lesson_row(179, 2, 4, prerequisites="[4]"), lesson_row(151, 2, 1)]
        )
        curriculum = Curriculum.from_source(conn)
        assert curriculum.lesson("u2l4").prerequisite_ids == ("u2l4",)
        assert curriculum.cycles == (("u2l4",),)
        assert curriculum.order == ("u2l1",)
        assert curriculum.lesson("u2l4").depth == 0

    def test_cycles_are_reported_in_prerequisite_order(self):
        # u1l1 requires u1l3, u1l3 requires u1l2, u1l2 requires u1l1, so the
        # cycle reads "this requires that" rather than the reverse.
        conn = make_source_db(
            [
                lesson_row(1, 1, 1, prerequisites="[3]"),
                lesson_row(2, 1, 2, prerequisites="[1]"),
                lesson_row(3, 1, 3, prerequisites="[2]"),
            ]
        )
        assert Curriculum.from_source(conn).cycles == (("u1l1", "u1l3", "u1l2"),)


# ── Topological order ───────────────────────────────────────────────


class TestTopologicalOrder:
    #: Ids deliberately out of order, exactly as the live database has them:
    #: unit 4's lessons sit between unit 1's and unit 5's in id space.
    INTERLEAVED = [
        lesson_row(10, 1, 1, prerequisites="[]"),
        lesson_row(30, 1, 2, prerequisites='["u1-l1"]'),
        lesson_row(12, 2, 1, prerequisites="[]"),
        lesson_row(11, 3, 1, prerequisites="[]"),
        lesson_row(20, 4, 1, prerequisites="[10]"),
        lesson_row(21, 4, 2, prerequisites="[12]"),
        lesson_row(13, 4, 3, prerequisites="[20]"),
    ]

    @pytest.fixture
    def curriculum(self) -> Curriculum:
        return Curriculum.from_source(make_source_db(self.INTERLEAVED))

    def test_raw_id_is_not_a_topological_order(self, curriculum):
        # The reason the sort key is (unit_num, lesson_num) and not id: under id,
        # u1l2 (id 30) has to come after u4l1 (id 20) which requires it.
        by_id = sorted(curriculum.lessons, key=lambda lesson: lesson.lesson_id)
        position = {lesson.key: index for index, lesson in enumerate(by_id)}
        backward = [
            (edge.child, edge.parent)
            for edge in curriculum.edges
            if position[edge.parent] >= position[edge.child]
        ]
        assert backward, "fixture no longer exercises the id-ordering hazard"

    def test_order_puts_every_parent_first(self, curriculum):
        position = {key: index for index, key in enumerate(curriculum.order)}
        for edge in curriculum.edges:
            if edge.parent_active and edge.child != edge.parent:
                assert position[edge.parent] < position[edge.child], edge

    def test_order_is_sorted_by_unit_then_lesson_number(self, curriculum):
        pairs = [
            (curriculum.lesson(key).unit_num, curriculum.lesson(key).lesson_num)
            for key in curriculum.order
        ]
        assert pairs == sorted(pairs)
        # u4l3 depends on u4l1, which is id 20, so id order would have put u4l3
        # (id 13) first. The order follows the curriculum, not the primary key.
        assert curriculum.order.index("u4l1") < curriculum.order.index("u4l3")

    def test_lesson_list_is_the_topological_order(self, curriculum):
        assert tuple(lesson.key for lesson in curriculum.lessons) == curriculum.order

    def test_children_and_parents_agree_with_the_edges(self, curriculum):
        for edge in curriculum.edges:
            if not edge.parent_active or edge.child == edge.parent:
                continue
            assert edge.child in {lesson.key for lesson in curriculum.children(edge.parent)}
            assert edge.parent in {lesson.key for lesson in curriculum.parents(edge.child)}

    def test_depth_counts_the_longest_prerequisite_chain(self, curriculum):
        # u4l1 <- u1l1 and u4l2 <- u2l1, so u4l3 is three lessons deep.
        assert curriculum.lesson("u1l1").depth == 1
        assert curriculum.lesson("u4l3").depth == 3
        assert curriculum.max_depth == 3

    def test_roots_are_the_lessons_nothing_blocks(self, curriculum):
        assert {lesson.key for lesson in curriculum.roots()} == {"u1l1", "u2l1", "u3l1"}

    def test_look_up_by_key_slug_and_id(self, curriculum):
        assert curriculum.lesson("u4l3").slug == "u4-l3"
        assert curriculum.lesson("u4-l3").key == "u4l3"
        assert curriculum.lesson(13).key == "u4l3"
        assert curriculum.lesson("nope") is None


# ── The content floor ───────────────────────────────────────────────


class TestMinContentFloor:
    @pytest.fixture
    def curriculum(self) -> Curriculum:
        conn = make_source_db(
            [
                lesson_row(1, 1, 1, prerequisites="[]"),
                lesson_row(2, 1, 2, prerequisites="[1]"),
                lesson_row(3, 2, 1, prerequisites="[]"),
            ]
        )
        # u1l1: four usable, one short. u1l2: none at all. u2l1: exactly the floor.
        add_problems(conn, 1, ["a1", "a2", "a3", "a4"])
        add_problems(conn, 2, ["b1", "b2"])
        add_problems(conn, 3, ["c1", "c2", "c3", "c4", "c5"])
        return Curriculum.from_source(
            conn, verified={f"a{i}" for i in range(1, 5)} | {f"c{i}" for i in range(1, 6)}
        )

    def test_a_lesson_one_short_of_the_floor_is_not_next(self, curriculum):
        assert curriculum.lesson("u1l1").usable_count == MIN_CONTENT - 1
        assert curriculum.lesson("u1l1").is_empty()
        assert curriculum.next_lesson({}).key == "u2l1"

    def test_exactly_the_floor_qualifies(self, curriculum):
        assert curriculum.lesson("u2l1").meets_min_content()
        assert curriculum.lesson("u2l1").usable_count == MIN_CONTENT

    def test_a_lesson_with_no_verified_problems_never_blocks_its_children(self, curriculum):
        # u1l2 has two linked problems and none of them verified, so it is
        # complete the moment it is reached: a content gap must not become a wall
        # for the lesson behind it.
        assert len(curriculum.lesson("u1l2").problems) == 2
        assert curriculum.lesson("u1l2").usable_count == 0
        assert curriculum.lesson_complete("u1l2")
        assert curriculum.prerequisites_met("u1l2", {f"a{i}": "complete" for i in range(1, 5)})

    def test_thin_lessons_stay_visible(self, curriculum):
        keys = {lesson.key for lesson in curriculum.lessons}
        assert keys == {"u1l1", "u1l2", "u2l1"}
        assert {lesson.key for lesson in curriculum.empty_lessons()} == {"u1l1", "u1l2"}
        assert curriculum.lesson("u1l1") in curriculum.roots()

    def test_unverified_problems_do_not_count_towards_the_floor(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        add_problems(conn, 1, [f"p{i}" for i in range(1, 7)])
        # Six linked, none verified: a quarantined problem cannot be recommended,
        # so it cannot make a lesson look ready.
        curriculum = Curriculum.from_source(conn, verified=set())
        assert len(curriculum.lesson("u1l1").problems) == 6
        assert curriculum.lesson("u1l1").usable_count == 0
        assert curriculum.next_lesson({}) is None

    def test_vague_titles_do_not_count_towards_the_floor(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        slugs = [f"p{i}" for i in range(1, 8)]
        add_problems(
            conn,
            1,
            slugs,
            titles={f"p{i}": f"Problem {i}" for i in range(3, 8)},
        )
        curriculum = Curriculum.from_source(conn, verified=set(slugs))
        lesson = curriculum.lesson("u1l1")
        assert lesson.verified_count == 7
        assert lesson.usable_count == 2
        assert lesson.is_empty()

    def test_falls_back_when_no_lesson_can_serve(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        add_problems(conn, 1, ["only-one"])
        curriculum = Curriculum.from_source(conn, verified={"only-one"})
        assert curriculum.next_lesson({}) is None
        rec = recommend_next({}, curriculum, pool=[PoolEntry("only-one", "Only One", 2)])
        assert rec is not None
        assert rec.source == "fallback"
        assert rec.lesson_key is None
        # Still visible, with the emptiness badge, rather than hidden.
        assert [lesson.key for lesson in curriculum.empty_lessons()] == ["u1l1"]

    def test_the_floor_is_configurable(self, curriculum):
        relaxed = Curriculum(
            curriculum.lessons, curriculum.edges, curriculum.unresolved, min_content=2
        )
        assert relaxed.next_lesson({}).key == "u1l1"


# ── Never recommend an orphan ───────────────────────────────────────


def assert_never_an_orphan(rec, curriculum: Curriculum) -> None:
    """The property the whole design rests on, as an assertion.

    A lesson-linked problem verifies 90.3% of the time; an unlinked one 33.0%. So
    a recommendation is either inside the lesson path, or it is explicitly
    labelled as a fallback and only happens when no lesson could serve.
    """
    if rec is None:
        return
    if rec.source != "fallback":
        assert rec.lesson_gated, rec
        lesson = curriculum.lesson(rec.lesson_key)
        assert lesson is not None, rec
        assert rec.slug in lesson.usable_slugs, (
            f"{rec.slug} is not in {lesson.key}, so it is outside the lesson path"
        )
    else:
        assert rec.lesson_key is None
        assert rec.lesson_slug is None
        assert "no lesson" in rec.reason


def _exhausted(curriculum: Curriculum) -> dict[str, str]:
    """Progress in which every usable problem in the graph is finished."""
    return {
        problem.slug: "complete"
        for lesson in curriculum.lessons
        for problem in lesson.usable_problems
    }


class TestNeverRecommendsAnOrphan:
    @pytest.fixture
    def curriculum(self) -> Curriculum:
        """A graph where every branch is short, thin, or gated.

        u1l1 is a root with exactly the floor. u1l2 is its child and holds one
        problem -- the u1l2/u1l7/u1l8/u1l9 shape. u2l1 is a second root with
        nothing verified at all. u3l1 is gated behind u1l2, so it only opens once
        the thin lesson is done, which is the case where re-applying the floor
        inside step 4 would strand its content.
        """
        conn = make_source_db(
            [
                lesson_row(1, 1, 1, prerequisites="[]"),
                lesson_row(2, 1, 2, prerequisites="[1]"),
                lesson_row(3, 2, 1, prerequisites="[]"),
                lesson_row(4, 3, 1, prerequisites="[2]"),
            ]
        )
        good = [f"good-{i}" for i in range(1, 7)]
        add_problems(conn, 1, good)
        add_problems(conn, 2, ["thin-1"])
        add_problems(conn, 3, ["broken-1", "broken-2"])
        add_problems(conn, 4, [f"deep-{i}" for i in range(1, 8)])
        return Curriculum.from_source(
            conn,
            verified=set(good) | {"thin-1"} | {f"deep-{i}" for i in range(1, 8)},
        )

    def test_the_whole_walk_stays_inside_the_lesson_path(self, curriculum):
        progress: dict[str, str] = {}
        served: list[str] = []
        for _ in range(60):
            rec = recommend_next(progress, curriculum)
            if rec is None or rec.source == "fallback":
                break
            assert_never_an_orphan(rec, curriculum)
            assert rec.slug not in progress, "the algorithm re-served finished work"
            progress[rec.slug] = "complete"
            served.append(rec.slug)
        assert served, "the walk made no progress at all"
        # A quarantined problem is never on the path, whatever the progress.
        assert "broken-1" not in served and "broken-2" not in served

    def test_the_walk_covers_every_usable_problem(self, curriculum):
        progress: dict[str, str] = {}
        while True:
            rec = recommend_next(progress, curriculum)
            if rec is None or rec.source == "fallback":
                break
            progress[rec.slug] = "complete"
        usable = {
            problem.slug for lesson in curriculum.lessons for problem in lesson.problems
            if problem.usable
        }
        assert usable <= set(progress)

    def test_a_thin_lesson_does_not_strand_its_children(self, curriculum):
        # u1l2 holds one problem, below the floor, and u3l1 waits on it. The
        # forward walk in step 4 has to serve u1l2 or u3l1 never opens.
        progress = {f"good-{i}": "complete" for i in range(1, 7)}
        rec = recommend_next(progress, curriculum)
        assert rec.lesson_key == "u1l2"
        assert rec.source == "next_lesson"
        progress[rec.slug] = "complete"
        rec = recommend_next(progress, curriculum)
        assert rec.lesson_key == "u3l1"

    def test_property_holds_across_many_progress_states(self, curriculum):
        slugs = [
            problem.slug
            for lesson in curriculum.lessons
            for problem in lesson.problems
            if problem.usable
        ]
        rng = random.Random(20260926)
        for _ in range(300):
            progress = {
                slug: rng.choice(["complete", "started", True, False])
                for slug in slugs
                if rng.random() < 0.5
            }
            rec = recommend_next(progress, curriculum)
            assert_never_an_orphan(rec, curriculum)
            if rec is not None and rec.source != "fallback":
                assert curriculum.lesson(rec.lesson_key).usable_slugs
            if rec is not None and rec.source == "fallback":
                assert curriculum.next_lesson(progress) is None, (
                    "the fallback fired while a lesson could still serve"
                )

    def test_a_quarantined_problem_is_never_recommended(self, curriculum):
        progress: dict[str, str] = {}
        while True:
            rec = recommend_next(progress, curriculum)
            if rec is None or rec.source == "fallback":
                break
            assert rec.slug not in {"broken-1", "broken-2"}
            progress[rec.slug] = "complete"

    def test_the_fallback_never_recommends_finished_work(self, curriculum):
        pool = [
            PoolEntry("broken-1", "Broken One", 2, ("loops",), 4.5),
            PoolEntry("orphan-1", "Orphan One", 1, ("loops",), 6.0),
        ]
        exhausted = _exhausted(curriculum)
        # Ranked by score, not by the order the caller happened to pass them in.
        assert recommend_next(exhausted, curriculum, pool=list(reversed(pool))).slug == "orphan-1"
        assert recommend_next(
            {**exhausted, "broken-1": "complete"}, curriculum, pool=pool
        ).slug == "orphan-1"
        assert (
            recommend_next(
                {**exhausted, "broken-1": True, "orphan-1": True}, curriculum, pool=pool
            )
            is None
        )

    def test_the_fallback_is_used_only_when_no_lesson_can_serve(self, curriculum):
        pool = [PoolEntry("orphan-1", "Orphan One", 1)]
        # A lesson can still serve, so the pool is not consulted.
        assert recommend_next({}, curriculum, pool=pool).lesson_gated
        rec = recommend_next(_exhausted(curriculum), curriculum, pool=pool)
        assert rec.source == "fallback" and not rec.lesson_gated

    def test_without_a_pool_there_is_no_recommendation_rather_than_a_guess(self, curriculum):
        # Nothing is reachable and no ContentStore is attached, so the honest
        # answer is None -- not a problem nobody can be graded on.
        assert curriculum.practice_pool() == ()
        assert recommend_next(_exhausted(curriculum), curriculum) is None


# ── Ordering inside a lesson, and the steps ─────────────────────────


class TestRecommendationSteps:
    @pytest.fixture
    def curriculum(self) -> Curriculum:
        conn = make_source_db(
            [
                lesson_row(1, 1, 1, prerequisites="[]"),
                lesson_row(2, 1, 2, prerequisites="[1]"),
                lesson_row(3, 1, 3, prerequisites="[2]"),
            ]
        )
        for lesson_id in (1, 2, 3):
            add_problems(conn, lesson_id, [f"p{lesson_id}-{i}" for i in range(1, 7)])
        slugs = {f"p{lesson}-{i}" for lesson in (1, 2, 3) for i in range(1, 7)}
        return Curriculum.from_source(conn, verified=slugs)

    def test_step_three_resumes_the_problem_the_student_started(self, curriculum):
        # The specification says "unstarted"; taken literally it abandons a
        # half-finished problem for good, so step 3 returns the first problem that
        # is not complete.
        rec = recommend_next({"p1-1": "complete", "p1-2": "started"}, curriculum)
        assert (rec.slug, rec.source, rec.lesson_key) == ("p1-2", "lesson", "u1l1")
        assert recommend_next({"p1-1": "complete"}, curriculum).slug == "p1-2"

    def test_a_prerequisite_must_be_complete_before_its_child_opens(self, curriculum):
        progress = {f"p1-{i}": "complete" for i in range(1, 7)}
        rec = recommend_next(progress, curriculum)
        assert (rec.lesson_key, rec.source) == ("u1l2", "next_lesson")

    def test_a_partly_done_lesson_is_not_complete(self, curriculum):
        progress = {f"p1-{i}": "complete" for i in range(1, 7)}
        progress["p2-1"] = "started"
        assert not curriculum.lesson_complete("u1l2", progress)
        assert not curriculum.prerequisites_met("u1l3", progress)

    def test_step_four_moves_on_past_finished_and_blocked_lessons(self, curriculum):
        progress = {f"p1-{i}": "complete" for i in range(1, 7)}
        progress.update({f"p2-{i}": "complete" for i in range(1, 7)})
        rec = recommend_next(progress, curriculum)
        assert (rec.lesson_key, rec.source, rec.slug) == ("u1l3", "next_lesson", "p3-1")

    def test_the_fallback_ranks_by_tier_then_topic_then_title(self, curriculum):
        progress = {f"p{lesson}-{i}": "complete" for lesson in (1, 2, 3) for i in range(1, 7)}
        pool = [
            PoolEntry("hard-topic", "Hard Topic", 5, ("graphs",), 1.6),
            PoolEntry("easy-anon", "Easy Anonymous", 1, ("tier1", "capstone"), 3.0),
            PoolEntry("easy-topic", "Easy Topic", 1, ("loops",), 6.0),
            PoolEntry("easy-vague", "Problem 7", 1, ("loops",), 5.0),
        ]
        rec = recommend_next(progress, curriculum, pool=pool)
        assert rec.slug == "easy-topic"
        assert rec.score == pytest.approx(6.0)
        assert rec.reason

    def test_duplicate_display_order_is_broken_deterministically(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        slugs = ["zebra", "apple", "mango", "kiwi", "pear", "fig"]
        add_problems(conn, 1, slugs)
        conn.execute("UPDATE lesson_problems SET display_order = 1")
        conn.commit()
        curriculum = Curriculum.from_source(conn, verified=set(slugs))
        order = [problem.slug for problem in curriculum.lesson("u1l1").problems]
        assert order == sorted(slugs)
        assert recommend_next({}, curriculum).slug == "apple"

    def test_problems_with_no_display_order_sort_last(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        add_problems(conn, 1, ["first", "second"])
        conn.execute("UPDATE lesson_problems SET display_order = NULL WHERE problem_id = ?", (1002,))
        conn.commit()
        curriculum = Curriculum.from_source(conn, verified={"first", "second"})
        assert [p.slug for p in curriculum.lesson("u1l1").problems] == ["first", "second"]


# ── Progress ────────────────────────────────────────────────────────


class TestProgress:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "complete"),
            (False, "unstarted"),
            (None, "unstarted"),
            ("complete", "complete"),
            ("Completed", "complete"),
            ("done", "complete"),
            ("solved", "complete"),
            ("started", "started"),
            ("in_progress", "started"),
            ("partial", "started"),
            ("", "unstarted"),
            ("something-else", "started"),
            (3, "started"),
        ],
    )
    def test_problem_state(self, value, expected):
        assert problem_state({"x": value}, "x") == expected

    def test_absent_progress_reads_as_unstarted(self):
        assert problem_state(None, "x") == "unstarted"
        assert problem_state({}, "x") == "unstarted"
        assert not is_complete(None, "x")

    def test_an_explicit_lesson_entry_completes_a_lesson(self, curriculum=None):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        add_problems(conn, 1, [f"p{i}" for i in range(1, 6)])
        curriculum = Curriculum.from_source(conn, verified={f"p{i}" for i in range(1, 6)})
        assert not curriculum.lesson_complete("u1l1", {})
        assert curriculum.lesson_complete("u1l1", {"lesson:u1l1": True})
        assert curriculum.lesson_complete("u1l1", {"lesson:u1-l1": "complete"})


# ── Quality predicates ──────────────────────────────────────────────


class TestQualityPredicates:
    @pytest.mark.parametrize(
        ("title", "vague"),
        [
            ("Problem 3", True),
            ("problem 12", True),
            ("Variant 2", True),
            ("Practice 7", True),
            ("Problem #4", True),
            ("Reverse a string", False),
            ("Count digits in a string", False),
            ("", False),
            ("Problem Solving", False),
        ],
    )
    def test_vague_titles(self, title, vague):
        assert is_vague_title(title) is vague

    def test_canonical_topic_needs_a_word_that_is_not_metadata_or_syntax(self):
        assert has_canonical_topic(["tier4", "capstone", "stabilized"]) is False
        assert has_canonical_topic(["tier2", "if", "elif", "else", "len"]) is False
        assert has_canonical_topic(["tier2", "if", "conditionals"]) is True
        assert has_canonical_topic([]) is False

    def test_practice_score_matches_the_specified_formula(self):
        assert practice_score(topic_tier=1, tags=["loops"], title="Loops") == pytest.approx(6.0)
        assert practice_score(topic_tier=2, tags=["loops"], title="Loops") == pytest.approx(4.5)
        assert practice_score(topic_tier=1, tags=["tier1"], title="Loops") == pytest.approx(4.0)
        assert practice_score(topic_tier=1, tags=["loops"], title="Problem 2") == pytest.approx(5.0)
        # A missing tier must not raise: the pool is verified content and a
        # division by zero here would take out the whole fallback.
        assert practice_score(topic_tier=None, tags=["loops"], title="Loops") == pytest.approx(6.0)


# ── Export ──────────────────────────────────────────────────────────


def _content_db(tmp_path) -> tuple[sqlite3.Connection, object]:
    """A real content database, then reopened for writing."""
    spec = ProblemSpec(
        slug="greet",
        title="Say hello",
        topic_tier=2,
        tags=["loops"],
        function_name="greet",
        function_signature="def greet(name):",
        starter_code="def greet(name):\n    pass\n",
        solution_code="def greet(name):\n    return 'Hello'\n",
    )
    spec.test_cases.append(TestCase(index=0, args=["Ada"], expected="Hello"))
    store = build_store([spec], tmp_path / "content.db")
    conn = sqlite3.connect(tmp_path / "content.db")
    # The verification pipeline is what writes this; a fixture has to do it by
    # hand to stand in for a run of `verify.py`.
    conn.execute("UPDATE problems SET status = ?", (ContentStatus.VERIFIED.value,))
    conn.commit()
    return conn, store


class TestExportToContentStore:
    @pytest.fixture
    def curriculum(self, real_shapes_conn) -> Curriculum:
        for lesson_id in (140, 151, 134):
            add_problems(real_shapes_conn, lesson_id, [f"p{lesson_id}-{i}" for i in range(1, 7)])
        return Curriculum.from_source(
            real_shapes_conn,
            verified={f"p151-{i}" for i in range(1, 7)},
        )

    def test_graph_is_written_and_readable(self, curriculum, tmp_path):
        conn, _ = _content_db(tmp_path)
        curriculum.to_content_store(conn)
        rows = conn.execute(
            "SELECT key, unit_num, lesson_num, depth, verified_count, usable_count,"
            " below_min_content FROM curriculum_lessons ORDER BY unit_num, lesson_num"
        ).fetchall()
        # 17 lessons in the fixture, one of them soft-deleted.
        assert len(rows) == 16
        assert rows[0][:3] == ("u1l1", 1, 1)
        usable_by_key = {row[0]: row[5] for row in rows}
        assert usable_by_key["u2l1"] == 6
        assert rows[-1][1] == 9
        u2l1 = conn.execute(
            "SELECT depth, verified_count, below_min_content FROM curriculum_lessons"
            " WHERE key = 'u2l1'"
        ).fetchone()
        assert u2l1 == (1, 6, 0)
        assert conn.execute(
            "SELECT COUNT(*) FROM curriculum_lesson_problems WHERE lesson_key = 'u2l1'"
        ).fetchone()[0] == 6
        assert conn.execute(
            "SELECT slug, position, verified, usable FROM curriculum_lesson_problems"
            " WHERE lesson_key = 'u2l1' ORDER BY position LIMIT 1"
        ).fetchone() == ("p151-1", 0, 1, 1)

    def test_edges_carry_the_token_and_the_lookup_used(self, curriculum, tmp_path):
        conn, _ = _content_db(tmp_path)
        curriculum.to_content_store(conn)
        rows = conn.execute(
            "SELECT child_key, parent_key, raw, resolved_by, parent_active"
            " FROM curriculum_edges ORDER BY child_key, parent_key"
        ).fetchall()
        by_child = {row[0]: row for row in rows}
        assert by_child["u1l2"][1:] == ("u1l1", '"u1-l1"', "slug", 1)
        assert by_child["u2l2"][1:] == ("u2l1", "1", "lesson_num", 1)
        assert by_child["u4l2"][1:] == ("u4l1", "133", "id", 1)
        # u6l1's parent is soft-deleted: the row is kept and flagged.
        assert by_child["u6l1"][1:] == ("u5l7", "138", "id", 0)

    def test_unresolved_prerequisites_are_persisted(self, curriculum, tmp_path):
        conn, _ = _content_db(tmp_path)
        curriculum.to_content_store(conn)
        rows = conn.execute(
            "SELECT child_key, raw, kind, reason FROM curriculum_unresolved"
            " ORDER BY child_key"
        ).fetchall()
        assert [row[0] for row in rows] == ["u4l1", "u4l8"]
        assert "no lesson has id 110" in rows[1][3]

    def test_meta_carries_the_numbers_a_route_needs(self, curriculum, tmp_path):
        conn, _ = _content_db(tmp_path)
        curriculum.to_content_store(conn)
        meta = dict(conn.execute("SELECT key, value FROM curriculum_meta").fetchall())
        assert meta["min_content"] == str(MIN_CONTENT)
        assert meta["lessons"] == "16"
        assert meta["unresolved"] == "2"
        assert meta["cycle_count"] == "0"

    def test_export_is_idempotent_and_leaves_the_content_tables_alone(self, curriculum, tmp_path):
        conn, _ = _content_db(tmp_path)
        curriculum.to_content_store(conn)
        conn.execute("INSERT INTO curriculum_lessons (key, lesson_id, unit_num, lesson_num, title, slug)"
                     " VALUES ('stale', 1, 9, 9, 'Stale', 'stale')")
        conn.commit()
        curriculum.to_content_store(conn)
        # Re-exporting replaces the derived graph rather than accumulating it.
        assert conn.execute(
            "SELECT COUNT(*) FROM curriculum_lessons WHERE key = 'stale'"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0] == 1

    def test_an_older_content_database_is_upgraded_not_rebuilt(self, curriculum, tmp_path):
        # The additive-migration path: a build that shipped curriculum_lessons
        # without depth or usable_count must be ALTERed, not rejected.
        conn, _ = _content_db(tmp_path)
        conn.execute(
            """CREATE TABLE curriculum_lessons (
                   key TEXT PRIMARY KEY, lesson_id INTEGER NOT NULL,
                   unit_num INTEGER NOT NULL, lesson_num INTEGER NOT NULL,
                   title TEXT NOT NULL, slug TEXT NOT NULL, description TEXT)"""
        )
        conn.execute("CREATE TABLE curriculum_edges (child_key TEXT, parent_key TEXT, raw TEXT,"
                     " resolved_by TEXT, PRIMARY KEY (child_key, parent_key))")
        conn.commit()
        curriculum.to_content_store(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(curriculum_lessons)")}
        assert {"depth", "usable_count", "below_min_content", "unlocks_at_mastery"} <= columns
        assert conn.execute("SELECT COUNT(*) FROM curriculum_lessons").fetchone()[0] == 16

    def test_a_read_only_reader_sees_the_graph(self, curriculum, tmp_path):
        conn, _ = _content_db(tmp_path)
        curriculum.to_content_store(conn)
        conn.close()
        reader = sqlite3.connect(f"file:{tmp_path / 'content.db'}?mode=ro", uri=True)
        try:
            assert reader.execute("SELECT COUNT(*) FROM curriculum_lessons").fetchone()[0] == 16
            assert reader.execute(
                "SELECT value FROM curriculum_meta WHERE key = 'min_content'"
            ).fetchone()[0] == str(MIN_CONTENT)
        finally:
            reader.close()


# ── Store-backed verification ───────────────────────────────────────


class TestVerifiedFromContentStore:
    def test_the_status_column_is_the_verification_verdict(self, tmp_path):
        conn, store = _content_db(tmp_path)
        try:
            curriculum = Curriculum.from_source(make_source_db([lesson_row(1, 1, 1)]), store=store)
            assert curriculum.verified_slugs == frozenset({"greet"})
            # And the fallback pool is drawn from the same place.
            pool = curriculum.practice_pool()
            assert [entry.slug for entry in pool] == ["greet"]
            assert pool[0].reachable is False
            assert pool[0].score == pytest.approx(1.5 + 2.0 + 1.0)
        finally:
            conn.close()
            store.close()

    def test_nothing_is_verified_without_a_store(self):
        curriculum = Curriculum.from_source(make_source_db([lesson_row(1, 1, 1)]))
        assert curriculum.verified_slugs == frozenset()
        assert curriculum.practice_pool() == ()
        assert curriculum.next_lesson({}) is None

    def test_a_predicate_can_decide_verification(self):
        conn = make_source_db([lesson_row(1, 1, 1, prerequisites="[]")])
        add_problems(conn, 1, [f"p{i}" for i in range(1, 6)])
        curriculum = Curriculum.from_source(conn, verified=lambda slug: slug != "p5")
        assert curriculum.lesson("u1l1").usable_count == 4
        assert curriculum.lesson("u1l1").verified_count == 4


# ── Construction helpers ─────────────────────────────────────────────


class TestConstruction:
    def test_build_curriculum_is_the_functional_spelling(self, real_shapes_conn):
        assert isinstance(build_curriculum(real_shapes_conn, verified=set()), Curriculum)

    def test_lessons_can_be_built_in_memory_without_a_database(self):
        lesson = Lesson(
            key=lesson_key(1, 1),
            lesson_id=1,
            unit_num=1,
            lesson_num=1,
            title="In memory",
            slug="in-memory",
        )
        curriculum = Curriculum([lesson])
        assert curriculum.lesson("u1l1").title == "In memory"
        assert curriculum.roots() == [lesson]
        assert curriculum.children("u1l1") == []

    def test_unknown_keys_answer_empty_rather_than_raising(self, real_shapes):
        assert real_shapes.lesson("u99l99") is None
        assert real_shapes.children("u99l99") == []
        assert real_shapes.parents("u99l99") == []
        assert real_shapes.successor("u99l99") is None
        assert real_shapes.lesson_complete("u99l99") is False
        assert not real_shapes.prerequisites_met("u99l99")

    def test_the_content_schema_is_created_idempotently(self, tmp_path):
        path = tmp_path / "empty.db"
        conn = sqlite3.connect(path)
        conn.executescript(CONTENT_SCHEMA)
        conn.executescript(CONTENT_SCHEMA)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"problems", "test_cases", "hidden_expectations"} <= tables
        conn.close()
