"""Normalised problem content, and the importer that produces it.

The upstream Python-practice-platform stores problems in SQLite with a schema
shaped around a Flask app. This module converts that into :class:`ProblemSpec`,
which is what the rest of this project reasons about, and it is deliberately the
*only* place that knows about the upstream shape -- nothing downstream should
need to care that the content arrived from another repository.

The awkward part of that schema is the test-case encoding. ``test_cases`` stores
inputs in whichever of four shapes the seed script happened to use, and the
dominant one wraps keyword arguments in a JSON envelope:

===============  ========  ==================================================
Shape            Count     Meaning
===============  ========  ==================================================
``{"args": {}}`` 29,855   JSON object; the value of ``args`` is the kwargs dict
``[...]``           264   JSON array; positional arguments
``a, b``            246   comma-separated Python literals; positional
``x``               146   a single Python literal; one positional argument
===============  ========  ==================================================

``expected_output`` is the ``repr()`` of the expected return value, so it is
parsed with :func:`ast.literal_eval` and falls back to the raw string.

Nothing here trusts the data. Every field is best-effort and every problem
carries a :class:`ContentStatus` recording what was found, because a library
this size will always contain rows that cannot be graded. The alternative --
dropping what does not parse and reporting a count -- loses the ability to
explain the gap later.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ContentStatus(str, Enum):
    """How much we trust a problem, and why.

    These are ordered by severity. :func:`is_shippable` is the gate.
    """

    #: Solution runs and passes every test case. Ships.
    VERIFIED = "verified"
    #: Parsed and looks coherent, but the solution has not been executed yet.
    UNVERIFIED = "unverified"
    #: The reference solution disagrees with at least one expected output. The
    #: tests or the solution are wrong; a student would be marked wrong for being
    #: right. Never ships.
    SOLUTION_DISAGREES = "solution_disagrees"
    #: The starter code does not parse, so a student starts from a broken file.
    STARTER_BROKEN = "starter_broken"
    #: No reference solution, so nothing can be self-verified.
    NO_SOLUTION = "no_solution"
    #: Test cases exist but none of them carry a usable expected output.
    NO_USABLE_TESTS = "no_usable_tests"
    #: The declared signature has no callable name, so no test can be dispatched.
    NO_CALLABLE = "no_callable"
    #: The declared signature's parameters do not match what the tests pass.
    SIGNATURE_MISMATCH = "signature_mismatch"
    #: The solution raised on at least one test case.
    SOLUTION_ERRORS = "solution_errors"
    #: A print-style problem whose expected output was recorded as ``null``.
    #: There is nothing to verify against, so it cannot ship -- but this is a
    #: content gap rather than a wrong answer, and is tracked separately so the
    #: two are not conflated when deciding what to fix.
    EXPECTED_NOT_RECORDED = "expected_not_recorded"
    #: Rejected by the static AST allowlist before any code ran.
    #: Distinct from SOLUTION_ERRORS because the two mean different things to
    #: different readers: during verification the solution is unusable, while a
    #: student who is told "sandbox_violation" learns the rule rather than
    #: concluding their code is broken.
    SANDBOX_REJECTED = "sandbox_rejected"
    #: Row could not be parsed at all.
    UNPARSEABLE = "unparseable"


#: Statuses that must never reach a student.
BLOCKING_STATUSES = frozenset(
    {
        ContentStatus.SOLUTION_DISAGREES,
        ContentStatus.STARTER_BROKEN,
        ContentStatus.NO_SOLUTION,
        ContentStatus.NO_USABLE_TESTS,
        ContentStatus.NO_CALLABLE,
        ContentStatus.SIGNATURE_MISMATCH,
        ContentStatus.SOLUTION_ERRORS,
        ContentStatus.EXPECTED_NOT_RECORDED,
        ContentStatus.SANDBOX_REJECTED,
        ContentStatus.UNPARSEABLE,
    }
)

#: The only status that may ship. UNVERIFIED is the default for every freshly
#: imported problem, so treating "not yet checked" as "fine" would put the entire
#: library in front of students on import. It has to be an explicit allowlist, not
#: a denylist of the known-bad.
SHIPPABLE_STATUSES = frozenset({ContentStatus.VERIFIED})


def is_shippable(status: ContentStatus) -> bool:
    return status in SHIPPABLE_STATUSES


class Defect(str, Enum):
    """What a human would have to fix, independently of *whether* it must be fixed.

    A :class:`ContentStatus` answers "can this ship?". A :class:`Defect` answers
    "what is wrong?", which is the question you need answered in order to fix a
    few thousand problems in a reasonable order. They are kept separate because
    the same status can have several unrelated causes -- 640 problems were
    initially reported as ``SOLUTION_ERRORS`` when almost none of them had a
    broken solution; they had bad test data, and the solution was the only thing
    telling us so.
    """

    NONE = "none"
    #: The stored expected value is a human label ("Zero", "Empty list") rather
    #: than an answer. Reachable only by editing the seed data.
    EXPECTED_IS_PROSE = "expected_is_prose"
    #: The stored expected value is absent or a placeholder such as ``result``.
    EXPECTED_IS_PLACEHOLDER = "expected_is_placeholder"
    #: A test passes a value of the wrong kind -- a string to a numeric
    #: parameter -- so a correct solution raises. The tests are wrong.
    TEST_INPUT_WRONG_TYPE = "test_input_wrong_type"
    #: The reference solution genuinely does not work.
    SOLUTION_BROKEN = "solution_broken"
    #: The reference solution and the expected outputs disagree and neither is
    #: obviously the placeholder.
    MISMATCH = "mismatch"
    #: The starter file does not parse, so a student starts from broken code.
    STARTER_UNPARSEABLE = "starter_unparseable"
    #: Row shape could not be decoded at all.
    ROW_UNPARSEABLE = "row_unparseable"
    #: The declared signature names a function the solution does not define, and
    #: the solution defines exactly one function under a different name. Usually
    #: the signature and the solution drifted apart, sometimes across two
    #: different problems entirely -- the tests then carry keywords belonging to
    #: neither. Detectable without guessing, and fixable by renaming.
    NAME_MISMATCH = "name_mismatch"
    #: The code reached for a blocked module, builtin, or dunder. Detectable
    #: statically; fixable by using plain Python.
    BLOCKED_OPERATION = "blocked_operation"
    #: Not enough information yet to say more.
    UNKNOWN = "unknown"


#: Prose labels that turn up in expected_output. Matched case-insensitively as a
#: whole phrase, because a legitimate answer can contain the word "empty".
_PROSE_LABELS = frozenset(
    {
        "zero", "one", "none", "empty list", "empty", "multiple elements",
        "single element", "not found", "found", "true", "false", "yes", "no",
        "positive", "negative", "even", "odd", "empty string", "error",
        "multiple", "some", "all", "none found", "not empty",
    }
)


@dataclass
class TestCase:
    """One input/expected pair, normalised to a callable invocation."""

    # Not a pytest test class, despite the name. Same guard Ducky's own
    # dataclasses use; without it pytest tries to collect it.
    __test__ = False

    #: Position within the problem, 0-based, stable across imports.
    index: int
    #: Positional arguments.
    args: list[Any] = field(default_factory=list)
    #: Keyword arguments.
    kwargs: dict[str, Any] = field(default_factory=dict)
    #: The expected return value, already parsed.
    expected: Any = None
    #: True when the upstream row marked this hidden.
    is_hidden: bool = False
    #: Human label, e.g. "Test 3".
    description: str = ""
    #: Set when the row could not be decoded. Such a case is dropped.
    parse_error: str = ""

    @property
    def usable(self) -> bool:
        """Whether this case can actually be run.

        A clean parse is the only requirement. An earlier version also demanded
        non-empty ``args``/``kwargs``, which made every zero-argument function
        untestable: ``def to_uppercase() -> str:`` is called as ``to_uppercase()``,
        so an empty argument list is the *correct* shape, not a malformed row.
        That silently excluded 920 of 30,511 cases -- 3% of the library's tests --
        and left those problems with nothing to grade against.

        Rows that are genuinely unusable still fail on ``parse_error``: an empty
        ``input_data``, an unparseable expected value, a JSON object with no
        ``args`` key.
        """
        return not self.parse_error

    def call_args(self) -> tuple[list[Any], dict[str, Any]]:
        return list(self.args), dict(self.kwargs)


@dataclass
class ProblemSpec:
    """A single problem, normalised."""

    slug: str
    title: str
    description: str = ""
    difficulty: str = ""
    difficulty_percent: int | None = None
    leetcode_difficulty: str = ""
    unit: int = 0
    lesson: int = 0
    topic_tier: int | None = None
    tags: list[str] = field(default_factory=list)
    learning_outcomes: list[str] = field(default_factory=list)
    time_complexity: str = ""
    space_complexity: str = ""
    optimal_approach: str = ""
    interview_frequency: str = ""
    companies: list[str] = field(default_factory=list)

    #: The function a student must implement, e.g. "two_sum".
    function_name: str = ""
    #: Parameters declared by ``function_signature``, in order.
    parameters: list[str] = field(default_factory=list)
    #: Verbatim signature, for display.
    function_signature: str = ""

    starter_code: str = ""
    #: Never shown to a student. Used only to verify the tests.
    solution_code: str = ""

    test_cases: list[TestCase] = field(default_factory=list)

    status: ContentStatus = ContentStatus.UNVERIFIED
    #: Why it landed in ``status``. Free text, for the quarantine report.
    status_detail: str = ""
    #: Upstream primary key, for traceability back to the source repo.
    source_id: int = 0

    @property
    def shippable(self) -> bool:
        return is_shippable(self.status) and bool(self.test_cases)

    def visible_cases(self) -> list[TestCase]:
        return [tc for tc in self.test_cases if not tc.is_hidden]

    def hidden_cases(self) -> list[TestCase]:
        return [tc for tc in self.test_cases if tc.is_hidden]


# ── Parsing ──────────────────────────────────────────────────────────


_DEF_NAME = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", re.M)
_BARE_NAME = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(", re.M)


def parse_function_name(signature: str) -> str:
    """Pull the callable name out of a signature.

    The upstream column is inconsistent: some rows are ``def two_sum(nums, k):``
    and some are just ``two_sum(nums, k)``. Handle both, and prefer the ``def``
    form when both somehow appear.
    """
    if not signature:
        return ""
    match = _DEF_NAME.search(signature)
    if match:
        return match.group(1)
    match = _BARE_NAME.search(signature)
    if match:
        return match.group(1)
    return ""


def parse_parameters(signature: str) -> list[str]:
    """Parameter names from a signature, ignoring ``self``/``*``/``**`` forms.

    Best-effort: a signature that will not parse yields an empty list, which the
    caller treats as "unknown" rather than "takes no arguments". Conflating those
    two would misclassify every problem with a malformed signature as a
    zero-argument one.
    """
    if not signature:
        return []
    # Try to make it parseable so the stdlib does the work.
    candidate = signature.strip()
    if not candidate.startswith("def "):
        candidate = f"def {candidate}"
    if candidate.count("(") != candidate.count(")"):
        return []
    # Strip the signature's own trailing colon before adding a return annotation.
    # Leaving it in place and appending "-> None" produces "def f(a: -> None",
    # which is a SyntaxError, and every signature then parsed as "unknown".
    candidate = candidate.rstrip()
    if candidate.endswith(":"):
        candidate = candidate[:-1].rstrip()
    if "->" not in candidate:
        candidate += " -> None"
    try:
        tree = ast.parse(candidate + ":\n    pass\n")
    except SyntaxError:
        return []
    if not tree.body or not isinstance(tree.body[0], ast.FunctionDef):
        return []
    args = tree.body[0].args
    names = [a.arg for a in (list(args.posonlyargs) + list(args.args))]
    return [n for n in names if n not in ("self", "cls")]


def _literal(text: str) -> tuple[Any, bool]:
    """Parse a Python/JSON literal. Returns ``(value, ok)``."""
    text = text.strip()
    if text == "":
        return None, False
    # JSON first: it handles true/false/null, which Python literals spell
    # differently, and the upstream expected_output column is JSON-flavoured.
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        pass
    try:
        return ast.literal_eval(text), True
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None, False


def _split_positional(text: str) -> list[Any] | None:
    """Split ``a, b`` into literals, respecting nesting.

    A plain ``text.split(",")`` corrupts ``[[1, 2], [3, 4]]``, so bracket depth
    and string state are tracked before splitting.
    """
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    current: list[str] = []
    for ch in text:
        if escape:
            current.append(ch)
            escape = False
            continue
        if quote:
            current.append(ch)
            if ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current))

    values: list[Any] = []
    for part in parts:
        part = part.strip()
        if not part:
            return None
        value, ok = _literal(part)
        if not ok:
            return None
        values.append(value)
    return values or None


def parse_test_case(
    index: int,
    input_data: str | None,
    expected_output: str | None,
    *,
    is_hidden: bool = False,
    description: str = "",
) -> TestCase:
    """Decode one upstream test-case row into a :class:`TestCase`."""
    raw = (input_data or "").strip()
    expected_raw = (expected_output or "").strip()

    expected, expected_ok = _literal(expected_raw)
    if not expected_ok:
        # An unparseable expectation is not a usable test. Recording the reason
        # lets the quarantine report say "4,092 cases had no readable expected
        # output" instead of just "some failed".
        return TestCase(
            index=index,
            is_hidden=is_hidden,
            description=description or f"Test {index + 1}",
            parse_error=f"unparseable expected_output: {expected_raw[:60]!r}",
        )

    if raw == "":
        return TestCase(
            index=index,
            is_hidden=is_hidden,
            description=description or f"Test {index + 1}",
            parse_error="empty input_data",
        )

    # Shape 1: {"args": {...}} -- the dominant encoding.
    if raw.startswith("{"):
        try:
            envelope = json.loads(raw)
        except ValueError as exc:
            return TestCase(
                index=index, expected=expected, is_hidden=is_hidden,
                description=description or f"Test {index + 1}",
                parse_error=f"unparseable JSON object: {exc}",
            )
        if isinstance(envelope, dict) and "args" in envelope:
            payload = envelope["args"]
            if isinstance(payload, dict):
                return TestCase(index=index, kwargs=dict(payload), expected=expected,
                                is_hidden=is_hidden, description=description)
            if isinstance(payload, list):
                return TestCase(index=index, args=list(payload), expected=expected,
                                is_hidden=is_hidden, description=description)
            return TestCase(index=index, expected=expected, is_hidden=is_hidden,
                            description=description,
                            parse_error=f"'args' was {type(payload).__name__}, want object or array")
        # A bare object with no envelope: treat the whole thing as kwargs.
        if isinstance(envelope, dict) and envelope:
            return TestCase(index=index, kwargs=dict(envelope), expected=expected,
                            is_hidden=is_hidden, description=description)
        return TestCase(index=index, expected=expected, is_hidden=is_hidden,
                        description=description, parse_error="input_data object had no 'args' key")

    # Shape 2: a JSON array of positional arguments. Note that a leading "[" is
    # not sufficient to commit to this shape -- "[1, 2], [3, 4]" is two
    # positional arguments, not a malformed array -- so a parse failure here
    # falls through to comma splitting rather than rejecting the row.
    if raw.startswith("["):
        value, ok = _literal(raw)
        if ok and isinstance(value, list):
            return TestCase(index=index, args=value, expected=expected,
                            is_hidden=is_hidden, description=description)

    # Shape 3: comma-separated positional literals.
    if "," in raw:
        values = _split_positional(raw)
        if values is not None:
            return TestCase(index=index, args=values, expected=expected,
                            is_hidden=is_hidden, description=description)
        return TestCase(index=index, expected=expected, is_hidden=is_hidden,
                        description=description, parse_error="unparseable comma-separated args")

    # Shape 4: a single positional argument.
    value, ok = _literal(raw)
    if ok:
        return TestCase(index=index, args=[value], expected=expected,
                        is_hidden=is_hidden, description=description)
    return TestCase(index=index, expected=expected, is_hidden=is_hidden,
                    description=description, parse_error=f"unparseable single arg: {raw[:60]!r}")


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    return []


# ── SQLite import ────────────────────────────────────────────────────


_PROBLEM_SQL = """
SELECT p.id, p.title, p.slug, p.description, p.difficulty, p.difficulty_percent,
       p.leetcode_difficulty, p.unit, p.lesson, p.topic_tier, p.tags,
       p.learning_outcomes, p.time_complexity, p.space_complexity,
       p.optimal_approach, p.interview_frequency, p.companies,
       p.function_signature, p.starter_code, p.solution
FROM coding_problems p
WHERE p.is_active = 1
ORDER BY p.id
"""

_TESTCASE_SQL = """
SELECT problem_id, input_data, expected_output, is_hidden, description, "order"
FROM test_cases
WHERE problem_id IN (SELECT id FROM coding_problems WHERE is_active = 1)
ORDER BY problem_id, COALESCE("order", id)
"""


def iter_problems(db_path: str | Path) -> Iterator[ProblemSpec]:
    """Stream every active problem, with its test cases attached.

    Streams rather than materialising because the full set is 5,132 problems and
    ~30,000 test cases; holding them all as objects costs far more than the query
    twice, and the verifier wants to report progress as it goes.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cases_by_problem: dict[int, list[TestCase]] = {}
        for row in conn.execute(_TESTCASE_SQL):
            bucket = cases_by_problem.setdefault(row["problem_id"], [])
            bucket.append(
                parse_test_case(
                    len(bucket),
                    row["input_data"],
                    row["expected_output"],
                    is_hidden=bool(row["is_hidden"]),
                    description=(row["description"] or ""),
                )
            )

        for row in conn.execute(_PROBLEM_SQL):
            pid = row["id"]
            signature = row["function_signature"] or ""
            spec = ProblemSpec(
                slug=row["slug"] or f"problem-{pid}",
                title=row["title"] or "",
                description=row["description"] or "",
                difficulty=row["difficulty"] or "",
                difficulty_percent=row["difficulty_percent"],
                leetcode_difficulty=row["leetcode_difficulty"] or "",
                unit=row["unit"] or 0,
                lesson=row["lesson"] or 0,
                topic_tier=row["topic_tier"],
                tags=_json_list(row["tags"]),
                learning_outcomes=_json_list(row["learning_outcomes"]),
                time_complexity=row["time_complexity"] or "",
                space_complexity=row["space_complexity"] or "",
                optimal_approach=row["optimal_approach"] or "",
                interview_frequency=row["interview_frequency"] or "",
                companies=_json_list(row["companies"]),
                function_name=parse_function_name(signature),
                parameters=parse_parameters(signature),
                function_signature=signature,
                starter_code=row["starter_code"] or "",
                solution_code=row["solution"] or "",
                test_cases=cases_by_problem.get(pid, []),
                source_id=pid,
            )
            yield spec
    finally:
        conn.close()


def expectation_defect(value: Any) -> Defect | None:
    """Classify a stored expectation: which defect it is, or ``None`` if fine.

    Single source of truth for this judgement, shared by the verifier's defect
    classifier and the repair tool. They have to agree: when they drifted, the
    classifier called ``"Zero"`` a prose label while the repair tool only knew
    about ``"result"``, so the tool declined to fix the very problems it was
    written to fix and quietly did almost nothing.

    Deliberately narrow. A loose list would rewrite real tests, and rewriting a
    real test to match whatever the solution happened to do is how a library
    quietly stops testing anything.
    """
    if value is None:
        return Defect.EXPECTED_IS_PLACEHOLDER
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in _PROSE_LABELS:
        return Defect.EXPECTED_IS_PROSE
    if text in _PLACEHOLDER_TOKENS:
        return Defect.EXPECTED_IS_PLACEHOLDER
    # "result1", "result_2" -- a bare token with no content.
    if text.startswith("result") and text[len("result"):].strip("0123456789_ ") == "":
        return Defect.EXPECTED_IS_PLACEHOLDER
    return None


def expectation_is_unusable(value: Any) -> bool:
    """True when a stored expectation cannot possibly be a correct answer."""
    return expectation_defect(value) is not None


#: Tokens that are scaffolding rather than answers.
_PLACEHOLDER_TOKENS = frozenset(
    {
        "result", "results", "output", "answer", "expected", "value",
        "expected_output", "expected output", "expected result", "correct answer",
        "your answer", "test", "todo", "none yet", "placeholder", "xxx", "tbd",
    }
)


# ── Static triage ────────────────────────────────────────────────────


def classify_static(spec: ProblemSpec) -> ProblemSpec:
    """Assign a status from inspection alone, before anything is executed.

    Deliberately conservative: this only rejects what is provably unusable, and
    leaves anything that needs running as :attr:`ContentStatus.UNVERIFIED`. The
    expensive verdict comes from actually executing the solution.
    """
    usable = [tc for tc in spec.test_cases if tc.usable]

    if not spec.solution_code.strip():
        spec.status = ContentStatus.NO_SOLUTION
        spec.status_detail = "no reference solution, so the tests cannot be self-verified"
        return spec

    if not usable:
        spec.status = ContentStatus.NO_USABLE_TESTS
        detail = sorted({tc.parse_error for tc in spec.test_cases if tc.parse_error})
        spec.status_detail = (
            f"{len(spec.test_cases)} test cases, none usable"
            + (f": {detail[0]}" if detail else "")
        )
        return spec

    if not spec.function_name:
        spec.status = ContentStatus.NO_CALLABLE
        spec.status_detail = f"cannot find a function name in signature {spec.function_signature[:60]!r}"
        return spec

    if spec.starter_code.strip():
        try:
            ast.parse(spec.starter_code)
        except SyntaxError as exc:
            spec.status = ContentStatus.STARTER_BROKEN
            spec.status_detail = f"starter_code does not parse: {exc.msg} at line {exc.lineno}"
            return spec

    # A declared parameter list that cannot possibly accept what the tests pass.
    if spec.parameters:
        required = set(spec.parameters)
        for tc in usable:
            supplied = set(tc.kwargs)
            positional = tc.args
            if not supplied and len(positional) > len(required):
                spec.status = ContentStatus.SIGNATURE_MISMATCH
                spec.status_detail = (
                    f"test passes {len(positional)} positional args but "
                    f"{spec.function_name}() declares {len(required)}"
                )
                return spec
            if supplied and not required:
                spec.status = ContentStatus.SIGNATURE_MISMATCH
                spec.status_detail = (
                    f"test passes kwargs {sorted(supplied)} but "
                    f"{spec.function_name}() declares no parameters"
                )
                return spec
            unknown = supplied - required
            if unknown:
                spec.status = ContentStatus.SIGNATURE_MISMATCH
                spec.status_detail = (
                    f"test passes unknown keyword(s) {sorted(unknown)}; "
                    f"{spec.function_name}() declares {sorted(required)}"
                )
                return spec

    spec.status = ContentStatus.UNVERIFIED
    spec.status_detail = f"{len(usable)}/{len(spec.test_cases)} test cases parsed"
    return spec
