"""The lesson DAG, and the algorithm that decides what a student does next.

Two facts about the upstream data shape everything here. Both are re-derivable
with ``tools/curriculum_report.py``; see ``docs/UX_DECISIONS.md`` section 5.

**``lessons.prerequisites`` is a bare ``TEXT`` column holding three mutually
incompatible formats.** It is not a foreign key and it is not consistently
serialised. Across the 103 lessons the column holds 26 bare ints, 47 JSON arrays
of which two are ``[]``, and 30 NULL or empty cells -- 71 tokens in all. Of
those, 42 resolve as ``lessons.id``, 13 resolve *only* as a unit-local
``(unit_num, lesson_num)`` pair, and 11 as a slug. Five resolve to nothing at all:
``110`` on four unit 4 lessons and ``114`` on one.

The 13 are the interesting part. Every one of them is a JSON int array, not a
bare int, so the format the audit attributed to bare ints is not the format that
actually needs it -- and no token in the column resolves under both readings, so
the total is the same either way while the unit 2 chain only resolves under the
unit-local one. Numeric tokens are therefore tried as ``lessons.id`` first and
then unit-locally, which is what :meth:`_LessonIndex.resolve` documents.

Nothing is dropped. Every token that could not be resolved is retained on the
lesson as an :class:`UnresolvedPrerequisite` with the reason, because a silently
discarded prerequisite is a student who cannot progress and nobody knows why.
A token that resolves to a *soft-deleted* lesson is kept too, as an edge with
``parent_active=False``: "withdrawn" and "does not exist" are different facts and
only one of them is a data error. The 85-lesson active graph therefore carries
65 of the 66 resolvable edges, plus the same 5 unresolved tokens.

**Raw ``lessons.id`` is not a topological order.** Ids interleave across units,
so 19 of the 64 fully active edges -- 30% -- point backwards under ``id``; under
``(unit_num, lesson_num)`` zero do. That pair is the sort key, but the order is
still *computed* with Kahn's algorithm rather than assumed, because "zero
backward edges today" is a property of today's data, not of the sort. When the
data changes, :attr:`Curriculum.cycles` reports the damage instead of the
ordering quietly lying. The graph has 21 roots and a longest chain of 15
lessons, and no cycles.

The recommendation algorithm is lesson-gated because of the strongest signal in
the library: of the 5,132 imported problems, the 1,491 that some active lesson
links verify 90.3% of the time, and the 3,641 that none does verify 33.0%. So
:func:`recommend_next` never returns a problem from outside the lesson path while
any lesson can serve one. ``MIN_CONTENT`` is the floor that makes that path
usable at all: 9 of the 85 active lessons have zero verified problems and
u1l2/u1l7/u1l8/u1l9 have exactly one, so without a floor a student is handed a
single problem for an entire lesson.
"""

from __future__ import annotations

import json
import keyword
import re
import sqlite3
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from typing import Any

from .content import ContentStatus
from .contentstore import ContentStore

__all__ = [
    "MIN_CONTENT",
    "COMPLETE",
    "CURRICULUM_INDEXES",
    "CURRICULUM_SCHEMA",
    "SOURCE_FALLBACK",
    "SOURCE_LESSON",
    "SOURCE_NEXT_LESSON",
    "STARTED",
    "UNSTARTED",
    "Curriculum",
    "Lesson",
    "LessonProblem",
    "PoolEntry",
    "PrerequisiteEdge",
    "PrerequisiteToken",
    "Progress",
    "Recommendation",
    "UnresolvedPrerequisite",
    "build_curriculum",
    "decode_prerequisites",
    "has_canonical_topic",
    "is_complete",
    "is_vague_title",
    "lesson_key",
    "practice_score",
    "problem_state",
    "recommend_next",
    "verified_slugs",
]

#: Minimum verified, non-vague problems a lesson must hold to be the lesson the
#: algorithm puts in front of a student. Set by the data, not by taste: 9 of the
#: 85 active lessons have none and u1l2/u1l7/u1l8/u1l9 have exactly one, which
#: is a whole lesson for one problem. Those lessons are still listed and still
#: show their emptiness -- they are just never the *next thing*.
MIN_CONTENT = 5


def lesson_key(unit_num: int, lesson_num: int) -> str:
    """``(unit, lesson)`` as a key: ``u2l3``.

    Preferred over ``lessons.id``, which interleaves across units, and over the
    slug, which is free text a human typed.
    """
    return f"u{unit_num}l{lesson_num}"


_KEY = re.compile(r"^u(\d+)l(\d+)$")


# ── Prerequisite decoding ────────────────────────────────────────────


@dataclass(frozen=True)
class PrerequisiteToken:
    """One element of a ``prerequisites`` cell, before it is resolved.

    The cell is not a data structure, it is the log of three import scripts, so
    a token keeps both its literal text and the shape the cell was read as.
    """

    #: Verbatim text of the token, so a report can quote what the column said.
    raw: str
    #: The decoded value: ``str`` for a slug, ``int`` for a number, ``None``
    #: when the cell could not be read at all.
    value: str | int | None
    #: What the token claims to be: ``slug``, ``number`` or ``unreadable``.
    kind: str
    #: How the enclosing cell was shaped: one of ``null``, ``empty``,
    #: ``json-array``, ``json-broken``, ``json-scalar``, ``bare-int``,
    #: ``bare-text``.
    shape: str

    @property
    def readable(self) -> bool:
        return self.kind != "unreadable"


def decode_prerequisites(raw: str | None) -> tuple[PrerequisiteToken, ...]:
    """Split a ``prerequisites`` cell into tokens, whatever shape it is in.

    Handles all three formats the column is known to hold, plus the shapes that
    would be a bug if they turned up. Returns an empty tuple for NULL and for
    ``[]``: an explicit empty prerequisite list is a root, not an error.
    """
    if raw is None:
        return ()
    text = raw.strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            return (PrerequisiteToken(text, None, "unreadable", "json-broken"),)
        if not isinstance(decoded, list):
            return (PrerequisiteToken(text, None, "unreadable", "json-scalar"),)
        return tuple(_token_from_element(element) for element in decoded)
    if text.startswith(("{", '"')):
        # A JSON object or a bare quoted string where a list of ids belongs.
        return (PrerequisiteToken(text, None, "unreadable", "json-scalar"),)
    try:
        return (PrerequisiteToken(text, int(text), "number", "bare-int"),)
    except ValueError:
        return (PrerequisiteToken(text, None, "unreadable", "bare-text"),)


def _token_from_element(element: Any) -> PrerequisiteToken:
    raw = json.dumps(element)
    # bool is a subclass of int, so `True` would otherwise decode as lesson 1.
    if isinstance(element, bool) or not isinstance(element, (int, str)):
        return PrerequisiteToken(raw, None, "unreadable", "json-array")
    kind = "slug" if isinstance(element, str) else "number"
    return PrerequisiteToken(raw, element, kind, "json-array")


@dataclass(frozen=True)
class PrerequisiteEdge:
    """A resolved prerequisite: ``parent`` must come before ``child``."""

    child: str
    parent: str
    #: The token that produced this edge, verbatim.
    raw: str
    #: Which lookup matched: ``slug``, ``id`` or ``lesson_num``.
    resolved_by: str
    #: False when the parent resolved to a soft-deleted lesson. Such an edge is
    #: retained and reported but gates nothing -- see
    #: :meth:`Curriculum.prerequisites_met`.
    parent_active: bool = True


@dataclass(frozen=True)
class UnresolvedPrerequisite:
    """A token that named a lesson which does not exist.

    Retained rather than dropped. Five of the 71 tokens in the live data are in
    this state, and each is a lesson that may be unreachable for a reason nobody
    can see.
    """

    child: str
    raw: str
    value: str | int | None
    kind: str
    shape: str
    #: Plain-language explanation, e.g. "no lesson has id 110 and no lesson is
    #: numbered 110 in unit 4".
    reason: str


# ── Problem quality predicates ───────────────────────────────────────


#: Titles that cannot be used as a label. 488 of the 5,132 imported problems
#: (9.5%) are of this shape, and 3,254 distinct titles cover the whole library,
#: so a browse UI that leads with the title is unusable for nearly half of it.
_VAGUE_TITLE = re.compile(r"\b(?:problem|variant|practice)\s*#?\s*\d+\b", re.IGNORECASE)


def is_vague_title(title: str) -> bool:
    """True for ``Problem 3`` / ``Variant 2`` / ``Practice 7`` shaped titles.

    Only the three forms the audit actually counted, because widening the
    pattern would exclude real content on a guess.
    """
    return bool(_VAGUE_TITLE.search(title or ""))


#: Tags that carry no information: a difficulty restatement, or a curation
#: state. Measured on the imported library -- every one of the 5,132 problems
#: carries a ``tierN`` tag, and ``stabilized``, ``capstone``,
#: ``restricted-compatible``, ``varied-practice`` and ``diversified`` are
#: import-batch bookkeeping.
_METADATA_TAGS = frozenset(
    {
        "capstone",
        "diversified",
        "restricted-compatible",
        "stabilized",
        "varied-practice",
    }
)
_TIER_TAG = re.compile(r"^tier\d+$")

#: Language keywords plus the builtins that turn up as tags. 38 of the 545
#: distinct tags are one of these; they name the syntax, not the idea.
_SYNTAX_TAGS = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist) | frozenset(
    {
        "abs", "bool", "dict", "enumerate", "filter", "float", "input", "int",
        "len", "list", "map", "max", "min", "print", "range", "repr", "round",
        "set", "sorted", "str", "sum", "tuple", "type", "zip",
    }
)


def has_canonical_topic(tags: Iterable[str]) -> bool:
    """True when at least one tag names a subject rather than a mechanism.

    There is no curated taxonomy: ``concepts`` and ``problem_concepts`` exist
    with the right columns and hold zero rows, and only 70 of the 110
    mechanically derived topics survive removing 28 syntax tokens and 7
    difficulty words. This is the mechanical half of that rule, and the honest
    one -- it can say a problem has no topic, and it cannot say a topic is the
    *right* one.
    """
    for tag in tags or ():
        normalised = str(tag or "").strip().lower()
        if not normalised or _TIER_TAG.match(normalised):
            continue
        if normalised in _METADATA_TAGS or normalised in _SYNTAX_TAGS:
            continue
        return True
    return False


def practice_score(*, topic_tier: int | None, tags: Iterable[str], title: str) -> float:
    """Fallback ranking: ``3/tier + 2*(has canonical topic) + 1*(title not vague)``.

    A missing tier scores as tier 1 rather than raising, because the fallback
    pool is drawn from verified problems and every one of those in the live
    library has a tier anyway.
    """
    tier = topic_tier if isinstance(topic_tier, int) and topic_tier > 0 else 1
    return 3.0 / tier + 2.0 * has_canonical_topic(tags) + (0.0 if is_vague_title(title) else 1.0)


# ── Lessons ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LessonProblem:
    """A problem linked to a lesson, in the order the lesson presents it."""

    slug: str
    title: str = ""
    display_order: int | None = None
    topic_tier: int | None = None
    tags: tuple[str, ...] = ()
    #: The content store says this problem's tests were executed and pass.
    verified: bool = False
    #: Position in the lesson after the deterministic sort. ``display_order``
    #: has 23 duplicated values across the live data, so the sort is not stable
    #: on its own and this is what the algorithm actually walks.
    position: int = 0

    @property
    def vague_title(self) -> bool:
        return is_vague_title(self.title)

    @property
    def usable(self) -> bool:
        """Verified *and* labelled: the only kind the algorithm hands over.

        126 of the 1,446 verified problems linked to an active lesson carry a
        ``Problem 3``-shaped title. They are real problems, but a lesson made
        of them reads as noise, so they do not count towards ``MIN_CONTENT``.
        """
        return self.verified and not self.vague_title


@dataclass(frozen=True)
class Lesson:
    """One active lesson and everything the graph knows about it."""

    key: str
    lesson_id: int
    unit_num: int
    lesson_num: int
    title: str
    slug: str
    description: str = ""
    tier: int | None = None
    unlocks_at_mastery: float | None = None
    estimated_minutes: int | None = None
    difficulty_level: str = ""
    #: The ``prerequisites`` column exactly as stored, for the record.
    prerequisites_raw: str = ""
    tokens: tuple[PrerequisiteToken, ...] = ()
    #: Resolved parents that are active, in declaration order.
    prerequisites: tuple[str, ...] = ()
    #: Every resolved parent, including soft-deleted ones.
    prerequisite_ids: tuple[str, ...] = ()
    #: Resolved parents that are soft-deleted.
    inactive_prerequisites: tuple[str, ...] = ()
    unresolved: tuple[UnresolvedPrerequisite, ...] = ()
    problems: tuple[LessonProblem, ...] = ()
    #: Length of the longest prerequisite chain ending at this lesson, counting
    #: itself. 0 means the lesson is not in the acyclic part of the graph.
    depth: int = 1

    # -- derived views ------------------------------------------------

    @property
    def verified_problems(self) -> tuple[LessonProblem, ...]:
        return tuple(p for p in self.problems if p.verified)

    @property
    def usable_problems(self) -> tuple[LessonProblem, ...]:
        return tuple(p for p in self.problems if p.usable)

    @property
    def usable_slugs(self) -> frozenset[str]:
        return frozenset(p.slug for p in self.problems if p.usable)

    @property
    def verified_count(self) -> int:
        return sum(1 for p in self.problems if p.verified)

    @property
    def usable_count(self) -> int:
        return sum(1 for p in self.problems if p.usable)

    def meets_min_content(self, minimum: int = MIN_CONTENT) -> bool:
        return self.usable_count >= minimum

    def is_empty(self, minimum: int = MIN_CONTENT) -> bool:
        """True when the lesson is too thin to be anybody's next step.

        The "coming soon" signal, not a reason to hide the lesson: the
        emptiness badge is how a student learns the content is not there yet.
        """
        return not self.meets_min_content(minimum)


# ── Progress ─────────────────────────────────────────────────────────

UNSTARTED = "unstarted"
STARTED = "started"
COMPLETE = "complete"

#: Values that mean "the student finished this".
_COMPLETE_STATES = frozenset(
    {"complete", "completed", "done", "finished", "passed", "solved", "correct", "true"}
)
#: Values that mean "touched but not finished".
_STARTED_STATES = frozenset(
    {"started", "attempted", "in_progress", "in-progress", "partial", "wip", "false"}
)

Progress = Mapping[str, Any]


def problem_state(progress: Progress | None, slug: str) -> str:
    """Classify one problem as unstarted, started or complete.

    A value that is neither a known state string nor a bool reads as
    *started*: an unrecognised record means the student has touched the
    problem, and re-serving it is worse than moving on.
    """
    if not progress or slug not in progress:
        return UNSTARTED
    value = progress[slug]
    if value is True:
        return COMPLETE
    if value is False or value is None:
        return UNSTARTED
    text = str(value).strip().lower()
    if not text:
        return UNSTARTED
    if text in _COMPLETE_STATES:
        return COMPLETE
    return STARTED


def is_complete(progress: Progress | None, slug: str) -> bool:
    return problem_state(progress, slug) == COMPLETE


# ── Recommendations ──────────────────────────────────────────────────

#: Step 3: the lesson itself has an unstarted problem.
SOURCE_LESSON = "lesson"
#: Step 4: the lesson is finished, so move to the next lesson's first problem.
SOURCE_NEXT_LESSON = "next_lesson"
#: Step 5: no lesson can serve this student, so fall back to the practice pool.
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class Recommendation:
    """What the student should do next, and why."""

    slug: str
    title: str
    source: str
    #: Populated for every lesson-gated pick; ``None`` for a fallback pick.
    lesson_key: str | None = None
    lesson_slug: str | None = None
    display_order: int | None = None
    topic_tier: int | None = None
    score: float | None = None
    reason: str = ""

    @property
    def lesson_gated(self) -> bool:
        """True when the problem came from the lesson path.

        The recommendation rests on this: a lesson-linked problem verifies 90.3%
        of the time and an unlinked one 33.0%, so anything not ``lesson_gated``
        is a weaker promise and is labelled as such rather than presented as the
        next step of a lesson.
        """
        return self.lesson_key is not None


@dataclass(frozen=True)
class PoolEntry:
    """A verified problem available to the fallback, with its score."""

    slug: str
    title: str
    topic_tier: int | None
    tags: tuple[str, ...] = ()
    score: float = 0.0
    #: Whether some active lesson links this problem. ``False`` means the
    #: fallback had to leave the lesson path, which the UI should say out loud.
    reachable: bool = False


# ── The graph ────────────────────────────────────────────────────────

_LESSON_COLUMNS = """
    id, unit_num, lesson_num, title, slug,
    COALESCE(description, '') AS description,
    tier, unlocks_at_mastery, COALESCE(prerequisites, '') AS prerequisites,
    estimated_minutes, COALESCE(difficulty_level, '') AS difficulty_level,
    is_active
"""

# LEFT JOIN, deliberately: 264 problems linked to an active lesson are
# soft-deleted upstream, and a link to a problem that is gone is a fact about
# the lesson worth keeping rather than a row to drop.
_LINK_COLUMNS = """
    lp.lesson_id AS lesson_id, lp.display_order AS display_order,
    cp.slug AS slug, cp.title AS title, cp.topic_tier AS topic_tier, cp.tags AS tags
"""


class Curriculum:
    """The active lesson DAG, and the queries the navigation needs.

    Build it with :meth:`from_source`. Every question the UI asks -- what are the
    roots, what unlocks this, what is next -- is a method here rather than SQL in
    a route, because the answer depends on the decoded prerequisite edges and on
    verification status, and those are the two things a route would get subtly
    wrong.
    """

    def __init__(
        self,
        lessons: Sequence[Lesson],
        edges: Sequence[PrerequisiteEdge] = (),
        unresolved: Sequence[UnresolvedPrerequisite] = (),
        *,
        cycles: Sequence[Sequence[str]] = (),
        order: Sequence[str] = (),
        depths: Mapping[str, int] | None = None,
        store: ContentStore | None = None,
        verified: Container[str] = frozenset(),
        min_content: int = MIN_CONTENT,
        stats: Mapping[str, Any] | None = None,
    ) -> None:
        self._order = tuple(order)
        self._rank = {key: position for position, key in enumerate(self._order)}
        depths = depths or {}
        located = [
            replace(lesson, depth=depths.get(lesson.key, lesson.depth)) for lesson in lessons
        ]
        self._lessons = tuple(sorted(located, key=self._position))
        self._edges = tuple(edges)
        self._unresolved = tuple(unresolved)
        self._cycles = tuple(tuple(cycle) for cycle in cycles)
        self._store = store
        self._verified = verified
        self.min_content = min_content
        self._stats = dict(stats or {})
        self._by_key = {lesson.key: lesson for lesson in self._lessons}
        self._by_slug = {lesson.slug: lesson for lesson in self._lessons}
        self._by_id = {lesson.lesson_id: lesson for lesson in self._lessons}
        self._place = {lesson.key: index for index, lesson in enumerate(self._lessons)}
        self._children: dict[str, tuple[str, ...]] = {}
        self._parents: dict[str, tuple[str, ...]] = {}
        self._build_adjacency()
        self._pool: tuple[PoolEntry, ...] | None = None

    def _position(self, lesson: Lesson) -> tuple[int, int, int, str]:
        return (
            self._rank.get(lesson.key, len(self._rank)),
            lesson.unit_num,
            lesson.lesson_num,
            lesson.key,
        )

    def _build_adjacency(self) -> None:
        children: dict[str, list[str]] = {lesson.key: [] for lesson in self._lessons}
        parents: dict[str, list[str]] = {lesson.key: [] for lesson in self._lessons}
        for edge in self._edges:
            if edge.child == edge.parent or edge.child not in children:
                continue
            if not edge.parent_active or edge.parent not in children:
                # A soft-deleted prerequisite cannot gate a student, and gating
                # on one would strand every lesson behind it. The edge stays in
                # `edges` and in the export; it just does not block.
                continue
            children[edge.parent].append(edge.child)
            parents[edge.child].append(edge.parent)
        self._children = {key: tuple(value) for key, value in children.items()}
        self._parents = {key: tuple(value) for key, value in parents.items()}

    # -- construction -------------------------------------------------

    @classmethod
    def from_source(
        cls,
        conn: sqlite3.Connection,
        *,
        store: ContentStore | None = None,
        verified: Iterable[str] | Callable[[str], bool] | None = None,
        min_content: int = MIN_CONTENT,
    ) -> Curriculum:
        """Load the graph from an upstream database.

        ``conn`` is the source database -- ``lessons``, ``lesson_problems`` and
        ``coding_problems`` -- read-only. Only ``is_active = 1`` lessons load:
        103 exist, 85 are active, and the 18 soft-deleted ones are still
        referenced by 100 ``lesson_problems`` rows, so a query without the filter
        offers students lessons that were withdrawn.

        Prerequisite tokens are resolved against *all* 103 lessons, not just the
        active ones, because "that lesson was withdrawn" and "that lesson does not
        exist" are different facts and only one of them is a data error. A token
        naming a soft-deleted lesson becomes an edge with ``parent_active=False``:
        reported, exported, and gating nothing.

        ``verified`` decides which problems may be recommended: a set of slugs, a
        predicate, or ``None``. It is separate from ``store`` because
        verification status lives in the content store while lesson membership
        lives in the source database, and because a caller holding a
        verification report should not need a store to walk the graph. With
        nothing supplied and no store, nothing is verified and no lesson can be
        recommended -- which is the honest answer, because verification is a
        fact and not a default.
        """
        every = _fetch(conn, f"SELECT {_LESSON_COLUMNS} FROM lessons ORDER BY unit_num, lesson_num")
        rows = _fetch(
            conn,
            f"SELECT {_LESSON_COLUMNS} FROM lessons WHERE is_active = 1 "
            "ORDER BY unit_num, lesson_num",
        )
        index = _LessonIndex(every)
        verified_set = _as_verified_set(verified, store)

        shapes: dict[str, int] = {}
        cells: dict[str, int] = {}
        edges: list[PrerequisiteEdge] = []
        unresolved: list[UnresolvedPrerequisite] = []
        tokens_by_key: dict[str, tuple[PrerequisiteToken, ...]] = {}
        edges_by_key: dict[str, list[PrerequisiteEdge]] = {}
        missing_by_key: dict[str, list[UnresolvedPrerequisite]] = {}
        without_prerequisites = 0
        for row in rows:
            key = lesson_key(int(row["unit_num"]), int(row["lesson_num"]))
            tokens = decode_prerequisites(row["prerequisites"])
            tokens_by_key[key] = tokens
            for token in tokens:
                shapes[token.shape] = shapes.get(token.shape, 0) + 1
            label = _cell_shape(row["prerequisites"], tokens)
            cells[label] = cells.get(label, 0) + 1
            if not tokens:
                without_prerequisites += 1
            lesson_edges, lesson_unresolved = _resolve_tokens(key, tokens, index)
            edges_by_key[key] = lesson_edges
            missing_by_key[key] = lesson_unresolved
            edges.extend(lesson_edges)
            unresolved.extend(lesson_unresolved)

        problems, link_stats = _load_problems(conn, rows, verified_set)

        lessons: list[Lesson] = []
        for row in rows:
            key = lesson_key(int(row["unit_num"]), int(row["lesson_num"]))
            lesson_edges = edges_by_key[key]
            lessons.append(
                Lesson(
                    key=key,
                    lesson_id=int(row["id"]),
                    unit_num=int(row["unit_num"]),
                    lesson_num=int(row["lesson_num"]),
                    title=row["title"] or "",
                    slug=row["slug"] or "",
                    description=row["description"] or "",
                    tier=row["tier"],
                    unlocks_at_mastery=row["unlocks_at_mastery"],
                    estimated_minutes=row["estimated_minutes"],
                    difficulty_level=row["difficulty_level"] or "",
                    prerequisites_raw=row["prerequisites"] or "",
                    tokens=tokens_by_key[key],
                    prerequisites=tuple(
                        edge.parent for edge in lesson_edges if edge.parent_active
                    ),
                    prerequisite_ids=tuple(edge.parent for edge in lesson_edges),
                    inactive_prerequisites=tuple(
                        edge.parent for edge in lesson_edges if not edge.parent_active
                    ),
                    unresolved=tuple(missing_by_key[key]),
                    problems=problems[key],
                )
            )

        keys = [lesson.key for lesson in lessons]
        # Self-loops are included here on purpose: a lesson that lists itself as
        # its own prerequisite is a cycle of length one, and the adjacency used
        # for queries drops self-loops so that `parents()` stays free of them.
        acyclic = [(edge.child, edge.parent) for edge in edges if edge.parent_active]
        order, cycles, depths = _topological_order(
            acyclic,
            nodes=keys,
            sort_key=lambda key: (
                index.by_key[key]["unit_num"],
                index.by_key[key]["lesson_num"],
            ),
        )
        stats: dict[str, Any] = {
            "lessons_in_source": len(every),
            "active_lessons": len(lessons),
            "prerequisite_cells": dict(sorted(cells.items())),
            "lessons_without_prerequisites": without_prerequisites,
            "tokens_by_shape": dict(sorted(shapes.items())),
            "edges_resolved": len(edges),
            "edges_by_lookup": _count_by(edges, lambda e: e.resolved_by),
            "edges_active_active": sum(
                1 for e in edges if e.parent_active and e.child != e.parent
            ),
            "edges_to_inactive": sum(1 for e in edges if not e.parent_active),
            "prerequisites_unresolved": len(unresolved),
            "self_references": sum(1 for e in edges if e.child == e.parent),
            "cycle_count": len(cycles),
            "cycles": [list(cycle) for cycle in cycles],
            "max_depth": max(depths.values(), default=0),
        }
        stats.update(link_stats)
        return cls(
            lessons,
            edges,
            unresolved,
            cycles=cycles,
            order=order,
            depths=depths,
            store=store,
            verified=verified_set,
            min_content=min_content,
            stats=stats,
        )

    # -- graph queries ------------------------------------------------

    @property
    def lessons(self) -> tuple[Lesson, ...]:
        """Every active lesson, in curriculum order.

        Topological first, and within that by ``(unit_num, lesson_num)``. A
        lesson caught in a cycle sorts after the acyclic ones instead of being
        dropped, because a lesson nobody can reach is still a lesson a student
        can see.
        """
        return self._lessons

    @property
    def order(self) -> tuple[str, ...]:
        """The topological order, by lesson key. Excludes cyclic lessons."""
        return self._order

    @property
    def edges(self) -> tuple[PrerequisiteEdge, ...]:
        return self._edges

    @property
    def unresolved(self) -> tuple[UnresolvedPrerequisite, ...]:
        return self._unresolved

    @property
    def cycles(self) -> tuple[tuple[str, ...], ...]:
        """Cycles in the resolved graph. Empty on the live data."""
        return self._cycles

    @property
    def max_depth(self) -> int:
        """Longest prerequisite chain, counting lessons. 15 on the live data."""
        return max((lesson.depth for lesson in self._lessons), default=0)

    @property
    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    @property
    def verified_slugs(self) -> Container[str]:
        return self._verified

    def roots(self) -> list[Lesson]:
        """Lessons with no active prerequisite, in curriculum order.

        21 of the 85 active lessons are roots, and 12 of them are unit 3 -- which
        is what a root count of 21 against 85 lessons says about this curriculum:
        it is a set of chains, not a single path.
        """
        return [lesson for lesson in self._lessons if not self._parents[lesson.key]]

    def lesson(self, key: str | int | Lesson) -> Lesson | None:
        """Look a lesson up by key (``u2l3``), slug, upstream id, or itself.

        Accepting a :class:`Lesson` keeps the internal call sites -- which walk
        keys they already hold -- from unwrapping and re-wrapping on every hop.
        """
        if isinstance(key, Lesson):
            return key
        if isinstance(key, int):
            return self._by_id.get(key)
        if key in self._by_key:
            return self._by_key[key]
        if key in self._by_slug:
            return self._by_slug[key]
        match = _KEY.match(key or "")
        if match:
            return self._by_key.get(lesson_key(int(match.group(1)), int(match.group(2))))
        return None

    def children(self, key: str | int | Lesson) -> list[Lesson]:
        """Lessons whose prerequisites include this one."""
        lesson = self.lesson(key)
        if lesson is None:
            return []
        return [self._by_key[child] for child in self._children.get(lesson.key, ())]

    def parents(self, key: str | int | Lesson) -> list[Lesson]:
        """Active prerequisites of a lesson, in declaration order."""
        lesson = self.lesson(key)
        if lesson is None:
            return []
        return [self._by_key[parent] for parent in self._parents[lesson.key]]

    def successor(self, key: str | int | Lesson) -> Lesson | None:
        """The next lesson in curriculum order. Step 4 of the algorithm.

        Not :meth:`next_lesson`, which asks which lesson is *ready* rather than
        which one comes next.
        """
        lesson = self.lesson(key)
        if lesson is None:
            return None
        position = self._place.get(lesson.key)
        if position is None:
            # A Lesson from another Curriculum. Not ours to place.
            return None
        following = position + 1
        return self._lessons[following] if following < len(self._lessons) else None

    def prerequisites_met(self, key: str | int | Lesson, progress: Progress | None = None) -> bool:
        """Whether every active prerequisite of a lesson is finished.

        A lesson with no usable problems is vacuously met. That is deliberate:
        9 active lessons have no verified content, and if they blocked their
        successors then a content gap would become a wall, which is worse than a
        thin lesson.
        """
        lesson = self.lesson(key)
        if lesson is None:
            return False
        return all(self.lesson_complete(parent, progress) for parent in self._parents[lesson.key])

    def lesson_complete(self, key: str | int | Lesson, progress: Progress | None = None) -> bool:
        """A lesson is complete when every usable problem in it is.

        ``progress`` may also carry an explicit ``lesson:<key or slug>`` entry,
        for a student who did a lesson outside the app.
        """
        lesson = self.lesson(key)
        if lesson is None:
            return False
        if progress:
            for alias in (f"lesson:{lesson.key}", f"lesson:{lesson.slug}"):
                if alias in progress and bool(progress[alias]):
                    return True
        return all(is_complete(progress, problem.slug) for problem in lesson.usable_problems)

    def next_lesson(self, progress: Progress | None = None) -> Lesson | None:
        """Step 2 of the algorithm: the lesson to work on right now.

        The first lesson in curriculum order whose resolved prerequisites are all
        complete *and* which holds at least :data:`MIN_CONTENT` usable problems.
        ``None`` means no lesson can serve this student, which is the signal to
        fall back to the practice pool.
        """
        for lesson in self._lessons:
            if not self.prerequisites_met(lesson, progress):
                continue
            if not lesson.meets_min_content(self.min_content):
                continue
            return lesson
        return None

    def empty_lessons(self) -> list[Lesson]:
        """Active lessons with fewer than ``MIN_CONTENT`` usable problems."""
        return [
            lesson for lesson in self._lessons if not lesson.meets_min_content(self.min_content)
        ]

    # -- the practice pool --------------------------------------------

    def practice_pool(self) -> tuple[PoolEntry, ...]:
        """Verified problems ranked for the fallback, best first.

        Needs a :class:`ContentStore`, because verification status lives there
        and nowhere else. Returns an empty tuple without one rather than
        guessing, so a misconfigured caller gets an explicit "no
        recommendation" instead of a problem nobody can be graded on.
        """
        if self._pool is not None:
            return self._pool
        if self._store is None:
            return ()
        reachable: set[str] = set()
        for lesson in self._lessons:
            reachable.update(problem.slug for problem in lesson.problems)
        entries = [
            PoolEntry(
                slug=row["slug"],
                title=row.get("title") or "",
                topic_tier=row.get("topic_tier"),
                tags=tuple(row.get("tags") or ()),
                score=practice_score(
                    topic_tier=row.get("topic_tier"),
                    tags=row.get("tags") or (),
                    title=row.get("title") or "",
                ),
                reachable=row["slug"] in reachable,
            )
            for row in self._store.list_problems(
                limit=self._store.count(shippable_only=True) + 1,
                status=ContentStatus.VERIFIED,
            )
        ]
        entries.sort(key=lambda e: (-e.score, e.topic_tier or 99, e.title, e.slug))
        self._pool = tuple(entries)
        return self._pool

    # -- export --------------------------------------------------------

    def to_content_store(self, conn: sqlite3.Connection) -> None:
        """Write the graph into a content database for the web layer to read.

        Additive, in the same shape as ``classroom.ClassroomDB._migrate_tables``:
        the tables are created if absent and any column an earlier version lacked
        is added, so a content database written by an older build keeps working
        instead of being rebuilt. Only the ``curriculum_*`` tables are touched,
        and those are replaced wholesale -- a stale prerequisite edge is worse
        than a missing one.

        The order is load-bearing: tables, then the missing columns, then the
        indexes. A content database that already has ``curriculum_lessons``
        without ``below_min_content`` fails at ``CREATE INDEX`` if the index
        script runs first, which is the whole failure mode an additive migration
        exists to avoid.
        """
        conn.executescript(CURRICULUM_SCHEMA)
        for table, columns in _CURRICULUM_MIGRATIONS.items():
            existing = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.executescript(CURRICULUM_INDEXES)
        for table in _CURRICULUM_TABLES:
            conn.execute(f"DELETE FROM {table}")
        for lesson in self._lessons:
            conn.execute(
                """INSERT OR REPLACE INTO curriculum_lessons
                   (key, lesson_id, unit_num, lesson_num, title, slug, description,
                    tier, unlocks_at_mastery, estimated_minutes, difficulty_level,
                    prerequisites_raw, depth, topo_rank, problem_count,
                    verified_count, usable_count, below_min_content)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lesson.key,
                    lesson.lesson_id,
                    lesson.unit_num,
                    lesson.lesson_num,
                    lesson.title,
                    lesson.slug,
                    lesson.description,
                    lesson.tier,
                    lesson.unlocks_at_mastery,
                    lesson.estimated_minutes,
                    lesson.difficulty_level,
                    lesson.prerequisites_raw,
                    lesson.depth,
                    self._rank.get(lesson.key),
                    len(lesson.problems),
                    lesson.verified_count,
                    lesson.usable_count,
                    int(not lesson.meets_min_content(self.min_content)),
                ),
            )
            for position, problem in enumerate(lesson.problems):
                conn.execute(
                    """INSERT OR REPLACE INTO curriculum_lesson_problems
                       (lesson_key, position, slug, title, display_order, topic_tier,
                        verified, vague_title, usable)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        lesson.key,
                        position,
                        problem.slug,
                        problem.title,
                        problem.display_order,
                        problem.topic_tier,
                        int(problem.verified),
                        int(problem.vague_title),
                        int(problem.usable),
                    ),
                )
        for edge in self._edges:
            conn.execute(
                """INSERT OR REPLACE INTO curriculum_edges
                   (child_key, parent_key, raw, resolved_by, parent_active)
                   VALUES (?,?,?,?,?)""",
                (
                    edge.child,
                    edge.parent,
                    edge.raw,
                    edge.resolved_by,
                    int(edge.parent_active),
                ),
            )
        for item in self._unresolved:
            conn.execute(
                """INSERT OR REPLACE INTO curriculum_unresolved
                   (child_key, raw, value, kind, shape, reason)
                   VALUES (?,?,?,?,?,?)""",
                (
                    item.child,
                    item.raw,
                    None if item.value is None else str(item.value),
                    item.kind,
                    item.shape,
                    item.reason,
                ),
            )
        meta = {
            "min_content": self.min_content,
            "lessons": len(self._lessons),
            "edges": len(self._edges),
            "unresolved": len(self._unresolved),
            "roots": len(self.roots()),
            "max_depth": self.max_depth,
            "cycle_count": len(self._cycles),
            "empty_lessons": len(self.empty_lessons()),
        }
        meta.update(self._stats)
        for key, value in meta.items():
            conn.execute(
                "INSERT OR REPLACE INTO curriculum_meta (key, value) VALUES (?,?)",
                (key, str(value)),
            )
        conn.commit()


def build_curriculum(
    conn: sqlite3.Connection,
    *,
    store: ContentStore | None = None,
    verified: Iterable[str] | Callable[[str], bool] | None = None,
    min_content: int = MIN_CONTENT,
) -> Curriculum:
    """Functional spelling of :meth:`Curriculum.from_source`."""
    return Curriculum.from_source(
        conn, store=store, verified=verified, min_content=min_content
    )


# ── The algorithm ────────────────────────────────────────────────────


def recommend_next(
    progress: Progress | None,
    curriculum: Curriculum,
    *,
    pool: Sequence[PoolEntry] | None = None,
) -> Recommendation | None:
    """What this student should do next, or ``None`` if nothing can be said.

    The five steps of ``docs/UX_DECISIONS.md`` section 5, in order:

    1. the 85 active lessons, resolved-prerequisite graph, sorted by
       ``(unit_num, lesson_num)`` -- :attr:`Curriculum.lessons`;
    2. ``L`` = the first lesson whose resolved prerequisites are all complete and
       which holds at least :data:`MIN_CONTENT` usable problems --
       :meth:`Curriculum.next_lesson`;
    3. if ``L`` has an unstarted usable problem, return it by ``display_order``;
    4. if ``L`` is complete, return the next lesson's first usable problem;
    5. otherwise the highest-scoring verified problem in the practice pool.

    Steps 3 and 4 keep the student inside the lesson path, which is the whole
    point: a lesson-linked problem is 90.3% likely to be gradable and an unlinked
    one 33.0%. Step 5 admits that the path has run out, and is labelled as such
    on the result so the UI can say so rather than presenting an orphan as the
    next step of a lesson.

    Two places where the specification had to be read rather than transcribed,
    both forced by the data and both visible in the tests:

    * **Step 3 resumes rather than skips.** Written as "the first *unstarted*
      problem" it would abandon a problem the student has already started: begin
      problem 2, fail it, and it is never offered again -- not while the other
      five are outstanding, and not afterwards, because by then the lesson is
      done. So step 3 returns the first problem that is not *complete*, and a
      started problem is the strongest candidate for "the next thing" rather than
      the one most likely to be skipped.
    * **Step 4 skips forward, and does not re-apply the floor.** Written as "the
      next lesson's first verified problem" it re-serves a problem the student has
      already finished: on the live data, finishing u2l1 hands back u2l2's first
      problem, the student completes it, and the next call hands back the same one,
      forever. So the walk continues past lessons that are finished and returns
      the first problem that is not complete, in the first later lesson that can
      serve one.

      The floor is not re-applied inside the walk, because the specification puts
      it in step 2 only, and because re-applying it strands content: u1l2, u1l7,
      u1l8 and u1l9 sit below the floor and are each a prerequisite of the next
      unit 1 lesson, so a walk that skipped them would leave verified problems in
      lessons that can never become ready. The prerequisite *gate* is kept -- that
      is the DAG's whole purpose.
    """
    lesson = curriculum.next_lesson(progress)
    if lesson is not None:
        for problem in lesson.usable_problems:
            if not is_complete(progress, problem.slug):
                return Recommendation(
                    slug=problem.slug,
                    title=problem.title,
                    source=SOURCE_LESSON,
                    lesson_key=lesson.key,
                    lesson_slug=lesson.slug,
                    display_order=problem.display_order,
                    topic_tier=problem.topic_tier,
                    reason=(
                        f"{problem.position + 1} of {len(lesson.usable_problems)} in "
                        f"{lesson.title} ({lesson.key})"
                    ),
                )
        following = curriculum.successor(lesson)
        while following is not None:
            if curriculum.prerequisites_met(following, progress):
                for problem in following.usable_problems:
                    if is_complete(progress, problem.slug):
                        continue
                    return Recommendation(
                        slug=problem.slug,
                        title=problem.title,
                        source=SOURCE_NEXT_LESSON,
                        lesson_key=following.key,
                        lesson_slug=following.slug,
                        display_order=problem.display_order,
                        topic_tier=problem.topic_tier,
                        reason=(
                            f"{lesson.title} is finished; the first unstarted problem in "
                            f"{following.title} ({following.key})"
                        ),
                    )
            following = curriculum.successor(following)
    return _fallback(progress, curriculum, pool)


def _fallback(
    progress: Progress | None,
    curriculum: Curriculum,
    pool: Sequence[PoolEntry] | None,
) -> Recommendation | None:
    """Step 5: the best verified problem that is not already finished.

    Ranked by ``3/tier + 2*(has topic) + 1*(usable title)``, and re-sorted here
    rather than trusted from the caller: ``pool`` is a public argument, and
    "best remaining verified problem" has to be true of whatever list is handed
    in, not only of one this module built. ``topic_tier`` is not treated as an
    independent axis anywhere else: the audit found it to be a perfect alias of
    ``difficulty``, and ``coding_problems.problem_order`` is NULL for 66% of rows,
    so ``lesson_problems.display_order`` is the only ordering that exists.
    """
    entries = sorted(
        curriculum.practice_pool() if pool is None else tuple(pool),
        key=lambda entry: (-entry.score, entry.topic_tier or 99, entry.title, entry.slug),
    )
    for entry in entries:
        if is_complete(progress, entry.slug):
            continue
        return Recommendation(
            slug=entry.slug,
            title=entry.title,
            source=SOURCE_FALLBACK,
            lesson_key=None,
            lesson_slug=None,
            display_order=None,
            topic_tier=entry.topic_tier,
            score=entry.score,
            reason=(
                "no lesson has enough verified problems to be next; best remaining "
                "verified problem"
            ),
        )
    return None


# ── Internals ────────────────────────────────────────────────────────


class _LessonIndex:
    """The lookups a prerequisite token can resolve against."""

    def __init__(self, rows: Sequence[sqlite3.Row]) -> None:
        self.by_id = {int(row["id"]): row for row in rows}
        self.by_slug = {row["slug"]: row for row in rows}
        self.by_unit_lesson = {
            (int(row["unit_num"]), int(row["lesson_num"])): row for row in rows
        }
        self.by_key = {
            lesson_key(int(row["unit_num"]), int(row["lesson_num"])): row for row in rows
        }

    def resolve(self, token: PrerequisiteToken, unit_num: int) -> tuple[sqlite3.Row | None, str]:
        """Resolve one token. Returns the lesson row and how it matched.

        Numeric tokens are tried as ``lessons.id`` first and then as a unit-local
        ``(unit_num, lesson_num)``. The audit attributed the unit-local reading
        to bare ints, but all 13 tokens that *need* it are JSON int arrays -- all
        of them in unit 2 -- while 42 need the id reading instead, and no token in
        the column resolves under both. So the two orderings agree on the total,
        and the one implemented is the one that also resolves the unit 2 chain,
        which is the chain a beginner walks.

        Resolved against every lesson, active or not: a token naming a
        soft-deleted lesson has resolved, and reporting it as missing would be a
        different and wrong claim.
        """
        if token.kind == "slug" and isinstance(token.value, str):
            return self.by_slug.get(token.value), "slug"
        if token.kind == "number" and isinstance(token.value, int):
            row = self.by_id.get(token.value)
            if row is not None:
                return row, "id"
            return self.by_unit_lesson.get((unit_num, token.value)), "lesson_num"
        return None, ""


def _resolve_tokens(
    child: str,
    tokens: Sequence[PrerequisiteToken],
    index: _LessonIndex,
) -> tuple[list[PrerequisiteEdge], list[UnresolvedPrerequisite]]:
    unit_num = int(index.by_key[child]["unit_num"])
    edges: list[PrerequisiteEdge] = []
    unresolved: list[UnresolvedPrerequisite] = []
    seen: set[str] = set()
    for token in tokens:
        row, how = index.resolve(token, unit_num)
        if row is None:
            unresolved.append(_unresolved_record(child, token, unit_num, how))
            continue
        parent = lesson_key(int(row["unit_num"]), int(row["lesson_num"]))
        if parent in seen:
            # The same prerequisite twice is one edge, not two.
            continue
        seen.add(parent)
        edges.append(
            PrerequisiteEdge(
                child=child,
                parent=parent,
                raw=token.raw,
                resolved_by=how,
                parent_active=bool(row["is_active"]),
            )
        )
    return edges, unresolved


def _unresolved_record(
    child: str,
    token: PrerequisiteToken,
    unit_num: int,
    how: str,
) -> UnresolvedPrerequisite:
    if token.kind == "slug":
        reason = f"no lesson has slug {token.value!r}"
    elif token.kind == "number":
        reason = (
            f"no lesson has id {token.value} and no lesson is numbered "
            f"{token.value} in unit {unit_num}"
        )
    elif token.shape == "json-broken":
        reason = f"prerequisites cell is not valid JSON: {token.raw!r}"
    elif token.shape == "bare-text":
        reason = f"prerequisites cell is neither JSON nor an integer: {token.raw!r}"
    else:
        reason = (
            f"prerequisites cell holds an unusable value: {token.raw!r} "
            f"({how or token.shape})"
        )
    return UnresolvedPrerequisite(
        child=child,
        raw=token.raw,
        value=token.value,
        kind=token.kind,
        shape=token.shape,
        reason=reason,
    )


def _load_problems(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    verified: Container[str],
) -> tuple[dict[str, tuple[LessonProblem, ...]], dict[str, Any]]:
    """Load every active lesson's linked problems, in a deterministic order."""
    linked: dict[int, list[tuple[Any, ...]]] = {}
    dangling = 0
    lesson_ids = [int(row["id"]) for row in rows]
    for start in range(0, len(lesson_ids), 400):
        chunk = lesson_ids[start : start + 400]
        placeholders = ",".join("?" * len(chunk))
        for row in _fetch(
            conn,
            f"""SELECT {_LINK_COLUMNS}
                FROM lesson_problems lp
                LEFT JOIN coding_problems cp ON cp.id = lp.problem_id
                WHERE lp.lesson_id IN ({placeholders})""",
            tuple(chunk),
        ):
            if row["slug"] is None:
                # A link to a problem that no longer exists at all.
                dangling += 1
                continue
            linked.setdefault(int(row["lesson_id"]), []).append(
                (
                    row["slug"],
                    row["title"] or "",
                    row["display_order"],
                    row["topic_tier"],
                    _tags(row["tags"]),
                )
            )
    problems: dict[str, tuple[LessonProblem, ...]] = {}
    total = 0
    unverified = 0
    verified_linked = 0
    for row in rows:
        entries = linked.get(int(row["id"]), [])
        # display_order has 23 duplicated values across the live data, so the
        # slug breaks the tie and the order the algorithm walks is reproducible.
        entries.sort(key=lambda item: (_order_key(item[2]), item[0]))
        built = []
        for position, (slug, title, order, tier, tags) in enumerate(entries):
            is_verified = slug in verified
            total += 1
            if is_verified:
                verified_linked += 1
            else:
                unverified += 1
            built.append(
                LessonProblem(
                    slug=slug,
                    title=title,
                    display_order=order,
                    topic_tier=tier,
                    tags=tags,
                    verified=is_verified,
                    position=position,
                )
            )
        problems[lesson_key(int(row["unit_num"]), int(row["lesson_num"]))] = tuple(built)
    return problems, {
        "linked_problems": total,
        "dangling_problem_links": dangling,
        "linked_problems_not_verified": unverified,
        "linked_problems_verified": verified_linked,
    }


def _order_key(display_order: int | None) -> tuple[int, int]:
    if display_order is None:
        return (1, 0)
    return (0, int(display_order))


def _tags(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


class _Predicate:
    """A ``verified`` argument given as a callable rather than a set."""

    def __init__(self, predicate: Callable[[str], bool]) -> None:
        self._predicate = predicate

    def __contains__(self, slug: object) -> bool:
        return self._predicate(str(slug))

    def __len__(self) -> int:
        raise TypeError("a predicate cannot report its size")


def _as_verified_set(
    verified: Iterable[str] | Callable[[str], bool] | None,
    store: ContentStore | None,
) -> Container[str]:
    if verified is None:
        return verified_slugs(store)
    if callable(verified):
        return _Predicate(verified)
    return frozenset(verified)


def verified_slugs(store: ContentStore | None) -> frozenset[str]:
    """The slugs the content store is willing to serve, from its status column.

    ``ContentStatus.VERIFIED`` is written by ``verify.py`` only when the
    reference solution ran and every parsed case passed, and a problem with no
    usable case is rejected as ``NO_USABLE_TESTS`` before that
    (``src/ducky/verify.py:430`` and ``:545``). So the status column is exactly
    :attr:`ProblemSpec.shippable`, and reading it as a set avoids loading every
    test case in the library to ask the same question.
    """
    if store is None:
        return frozenset()
    return frozenset(
        row["slug"]
        for row in store.list_problems(
            limit=store.count(shippable_only=True) + 1,
            status=ContentStatus.VERIFIED,
        )
    )


def _topological_order(
    edges: Sequence[tuple[str, str]],
    *,
    nodes: Sequence[str],
    sort_key: Callable[[str], Any],
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], dict[str, int]]:
    """Kahn's algorithm, tie-broken by the sort key.

    Returns the order, the cycles among the nodes it could not release, and each
    node's depth. Nodes in a cycle get depth 0, which is how a caller can tell
    "no prerequisites" from "unreachable because of a cycle" -- a distinction
    the live data does not currently need, but one the loader must not lose.
    """
    parents: dict[str, list[str]] = {node: [] for node in nodes}
    children: dict[str, list[str]] = {node: [] for node in nodes}
    indegree: dict[str, int] = {node: 0 for node in nodes}
    for child, parent in edges:
        if child not in indegree or parent not in indegree:
            continue
        parents[child].append(parent)
        children[parent].append(child)
        indegree[child] += 1

    ready: list[tuple[Any, str]] = [
        (sort_key(node), node) for node, count in indegree.items() if count == 0
    ]
    ready.sort()
    order: list[str] = []
    depth: dict[str, int] = {node: 1 for node in nodes}
    while ready:
        _, node = heappop(ready)
        order.append(node)
        for child in children[node]:
            depth[child] = max(depth.get(child, 1), depth[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                heappush(ready, (sort_key(child), child))
    released = set(order)
    # Walked through `parents`, so a cycle reads "this lesson requires that one",
    # which is the direction a human needs to see it in.
    cycles = _find_cycles([node for node in nodes if node not in released], parents)
    for node in nodes:
        if node not in released:
            depth[node] = 0
    return tuple(order), cycles, depth


def _find_cycles(
    nodes: Sequence[str], parents: Mapping[str, Sequence[str]]
) -> tuple[tuple[str, ...], ...]:
    """Every cycle reachable from ``nodes``, each rotated to start at its lowest key.

    One cycle per group of mutually-unreachable lessons: a node already covered by
    a reported cycle is not walked again, so a node sitting in two loops yields
    the first one. The purpose is to say the graph is broken and name the
    lessons involved, not to enumerate every loop -- and the live graph has no
    cycles at all.

    Iterative on purpose: a prerequisite cycle is exactly the input that makes a
    recursive walk overflow.
    """
    found: list[tuple[str, ...]] = []
    covered: set[str] = set()
    for start in nodes:
        if start in covered:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = [start]
        on_path = {start}
        while stack:
            node, edge_index = stack[-1]
            edges = parents.get(node, ())
            if edge_index >= len(edges):
                stack.pop()
                on_path.discard(path.pop())
                continue
            stack[-1] = (node, edge_index + 1)
            nxt = edges[edge_index]
            if nxt == start:
                cycle = tuple(path)
                covered.update(cycle)
                pivot = min(range(len(cycle)), key=lambda index: cycle[index])
                found.append(cycle[pivot:] + cycle[:pivot])
                break
            if nxt in on_path or nxt not in parents:
                continue
            path.append(nxt)
            on_path.add(nxt)
            stack.append((nxt, 0))
    return tuple(found)


def _cell_shape(raw: str | None, tokens: Sequence[PrerequisiteToken]) -> str:
    """How one ``prerequisites`` cell was shaped, for the stats block.

    Distinct from counting tokens: a cell holding ``[]`` is a shape with no
    tokens in it, and 30 of the live cells are NULL, which is the difference
    between "this lesson has no prerequisites" and "this column was never filled
    in for this lesson".
    """
    if raw is None:
        return "null"
    if not raw.strip():
        return "empty"
    return tokens[0].shape if tokens else "json-array-empty"


def _fetch(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> list[sqlite3.Row]:
    """Query through a private cursor so the caller's ``row_factory`` is untouched."""
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        return cursor.execute(sql, tuple(params)).fetchall()
    finally:
        cursor.close()


def _count_by(items: Iterable[Any], key: Callable[[Any], Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        name = str(key(item))
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


# ── Content-store schema ─────────────────────────────────────────────

CURRICULUM_SCHEMA = """
CREATE TABLE IF NOT EXISTS curriculum_lessons (
    key                 TEXT PRIMARY KEY,
    lesson_id           INTEGER NOT NULL,
    unit_num            INTEGER NOT NULL,
    lesson_num          INTEGER NOT NULL,
    title               TEXT NOT NULL,
    slug                TEXT NOT NULL,
    description         TEXT,
    tier                INTEGER,
    unlocks_at_mastery  REAL,
    estimated_minutes   INTEGER,
    difficulty_level    TEXT,
    prerequisites_raw   TEXT,
    depth               INTEGER NOT NULL DEFAULT 1,
    topo_rank           INTEGER,
    problem_count       INTEGER NOT NULL DEFAULT 0,
    verified_count      INTEGER NOT NULL DEFAULT 0,
    usable_count        INTEGER NOT NULL DEFAULT 0,
    below_min_content   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS curriculum_edges (
    child_key     TEXT NOT NULL,
    parent_key    TEXT NOT NULL,
    raw           TEXT NOT NULL,
    resolved_by   TEXT NOT NULL,
    parent_active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (child_key, parent_key)
);

-- The prerequisites that name nothing. Kept in the content database and not
-- only in a report, because the only way to fix one is to know it is there.
CREATE TABLE IF NOT EXISTS curriculum_unresolved (
    child_key TEXT NOT NULL,
    raw       TEXT NOT NULL,
    value     TEXT,
    kind      TEXT NOT NULL,
    shape     TEXT NOT NULL DEFAULT '',
    reason    TEXT NOT NULL,
    PRIMARY KEY (child_key, raw)
);

CREATE TABLE IF NOT EXISTS curriculum_lesson_problems (
    lesson_key    TEXT NOT NULL,
    position      INTEGER NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT,
    display_order INTEGER,
    topic_tier    INTEGER,
    verified      INTEGER NOT NULL DEFAULT 0,
    vague_title   INTEGER NOT NULL DEFAULT 0,
    usable        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lesson_key, position)
);

CREATE TABLE IF NOT EXISTS curriculum_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Applied after the additive column migration, never before: an older
#: ``curriculum_lessons`` may not have the columns these indexes read.
CURRICULUM_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_curriculum_lessons_unit
    ON curriculum_lessons(unit_num, lesson_num);
CREATE INDEX IF NOT EXISTS idx_curriculum_lessons_min
    ON curriculum_lessons(below_min_content);
CREATE INDEX IF NOT EXISTS idx_curriculum_edges_parent
    ON curriculum_edges(parent_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_curriculum_problems_slug
    ON curriculum_lesson_problems(lesson_key, slug);
"""

_CURRICULUM_TABLES = (
    "curriculum_lessons",
    "curriculum_edges",
    "curriculum_unresolved",
    "curriculum_lesson_problems",
    "curriculum_meta",
)

#: The full current column set of every curriculum table, minus the primary key
#: columns -- those are what identifies the table, so a database without them is
#: not an older version of this schema but a different one. Anything missing from
#: an existing table is added with ``ALTER TABLE``, so a content database written
#: by an older build is upgraded rather than rebuilt. Listing the whole shape
#: rather than only the recently added columns is deliberate: a partial list goes
#: stale silently, and the failure shows up as a migration error on somebody
#: else's content database.
_CURRICULUM_MIGRATIONS: dict[str, dict[str, str]] = {
    "curriculum_lessons": {
        "unit_num": "INTEGER NOT NULL DEFAULT 0",
        "lesson_num": "INTEGER NOT NULL DEFAULT 0",
        "title": "TEXT NOT NULL DEFAULT ''",
        "slug": "TEXT NOT NULL DEFAULT ''",
        "description": "TEXT",
        "tier": "INTEGER",
        "unlocks_at_mastery": "REAL",
        "estimated_minutes": "INTEGER",
        "difficulty_level": "TEXT",
        "prerequisites_raw": "TEXT",
        "depth": "INTEGER NOT NULL DEFAULT 1",
        "topo_rank": "INTEGER",
        "problem_count": "INTEGER NOT NULL DEFAULT 0",
        "verified_count": "INTEGER NOT NULL DEFAULT 0",
        "usable_count": "INTEGER NOT NULL DEFAULT 0",
        "below_min_content": "INTEGER NOT NULL DEFAULT 0",
    },
    "curriculum_edges": {
        "raw": "TEXT NOT NULL DEFAULT ''",
        "resolved_by": "TEXT NOT NULL DEFAULT ''",
        "parent_active": "INTEGER NOT NULL DEFAULT 1",
    },
    "curriculum_unresolved": {
        "raw": "TEXT NOT NULL DEFAULT ''",
        "value": "TEXT",
        "kind": "TEXT NOT NULL DEFAULT ''",
        "shape": "TEXT NOT NULL DEFAULT ''",
        "reason": "TEXT NOT NULL DEFAULT ''",
    },
    "curriculum_lesson_problems": {
        "slug": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT",
        "display_order": "INTEGER",
        "topic_tier": "INTEGER",
        "verified": "INTEGER NOT NULL DEFAULT 0",
        "vague_title": "INTEGER NOT NULL DEFAULT 0",
        "usable": "INTEGER NOT NULL DEFAULT 0",
    },
    "curriculum_meta": {"value": "TEXT NOT NULL DEFAULT ''"},
}
