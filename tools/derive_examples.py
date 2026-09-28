#!/usr/bin/env python3
"""Derive worked examples and edge cases from test data, so explanations can be
built from facts instead of invented.

The premise: a problem's test cases are the most specific material available for
explaining it. If a test asserts ``count_occurrences(["a", "b", "a"], "a") == 2``,
that one line tells a student more than any paragraph generated from the title
and the tags. This tool turns those test cases into a machine-readable fact base
-- a rendered call, a rendered expected value, a classification of the input
shape, and the set of detectable edge cases -- which a rewriting agent or a
teacher can then write *about*.

Nothing here parses the upstream database. It consumes :class:`ducky.content.
ProblemSpec` and :class:`~ducky.content.TestCase` as
:mod:`ducky.content` already normalised them, because re-decoding the four
``test_cases`` encodings a second time is a second set of bugs.

Three properties are load-bearing:

1. **Truncation is always explicit.** A rendered value that did not fit says so
   with ``...``. Silently shortening a test case would let a rewriter assert that
   a function receives a 12-element list when it receives 500.

2. **Hidden cases never emit their expected value.** This is the same rule
   :meth:`ducky.verify.CaseResult.student_view` enforces, and it is enforced
   *here*, at derivation, rather than at the point of display -- a fact base
   written to disk is exactly the kind of thing that later gets served to
   someone. The redaction extends one step further than that: expected-derived
   edge reasons ("not found", "already sorted") are withheld from hidden cases
   too, because the reason is itself a statement about the answer.

3. **"Edge case" means detected, not judged.** Every reason this tool reports is
   a property of the data that can be read off it -- an empty container, a
   repeated element, a zero, a negative, a length. Nothing is inferred from a
   problem's title, difficulty, or topic tag.

Usage:
    python tools/derive_examples.py --db /tmp/probe.db \\
        --out reports/examples.json [--slug SLUG] [--limit N] --top K
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ducky.content import (  # noqa: E402
    Defect,
    ProblemSpec,
    TestCase,
    expectation_defect,
    iter_problems,
)

# ── Vocabulary ────────────────────────────────────────────────────────
#
# These constants are the tool's contract with whatever consumes the fact base.
# They are spelled out rather than computed so that a rewriter can pattern-match
# on them, and so that adding a value kind is a deliberate act.

#: Default render width, in characters. Chosen to fit a worked example on one
#: line of a terminal-derived JSON report without wrapping the interesting part.
DEFAULT_WIDTH = 60

#: Below this a width stops being able to hold any structural marker, and the
#: output stops being readable. Rejected rather than clamped: silently widening
#: a caller's request is the sort of thing that hides a bug in the caller.
MIN_WIDTH = 8

#: Marker for "this was cut". Always emitted, never implied.
ELLIPSIS = "..."

#: Stand-in for a hidden case's expected value. The same string
#: :meth:`ducky.verify.CaseResult.student_view` uses, so a consumer that already
#: handles redaction there handles it here.
EXPECTED_HIDDEN = "<hidden>"

#: A container or string longer than this counts as "very long". A chosen
#: threshold, not a discovered one; the report prints how often it fires so the
#: number can be argued with.
LONG_VALUE_MAX = 20

#: Cap on the lines of a multi-line rendering. A 500-element list rendered one
#: item per line is a worse fact base than an elided one, and ``...`` in the
#: middle still says there was more.
BLOCK_MAX_LINES = 12

#: How many items of one container are considered when rendering inline. Above
#: this the middle is elided without being rendered at all.
INLINE_ITEM_CAP = 120

#: How many worked examples and edge cases one problem's fact base carries. The
#: counts of what was dropped are recorded alongside, so nothing disappears
#: quietly.
MAX_WORKED = 6
MAX_EDGES = 8

#: Every value ``input_kind`` can take. Order is the precedence order used by
#: :func:`input_kind`; the docstring there explains why.
VALUE_KINDS = (
    "none",
    "empty",
    "nested",
    "mixed",
    "list",
    "dict",
    "boolean",
    "numbers",
    "strings",
)

#: Every edge reason this tool can emit, most specific first. The order *is* the
#: contract: ``primary_reason`` is the first of a case's reasons to appear in
#: this list, so the rarer, sharper facts win the headline slot. A case that is
#: both empty and sorted is reported as "empty list".
REASON_PRIORITY = (
    "empty list",
    "empty string",
    "empty dictionary",
    "empty tuple",
    "empty set",
    "empty result",
    "single element",
    "duplicate elements",
    "not found",
    "no expected value recorded",
    "zero",
    "negative",
    "boundary value",
    "already sorted",
    "very long",
)

#: Reasons derived from the *expected value* of a case. Withheld for a hidden
#: case: knowing the answer was "not found" is most of knowing the answer, and a
#: reason string is just as quotable as an expected value.
OUTPUT_EDGE_REASONS = frozenset({"not found", "empty result", "no expected value recorded"})

#: The subset that is safe to report for a hidden case, because the input of a
#: hidden case is not secret -- the content store serves it
#: (:mod:`ducky.contentstore` moves only the expectation aside) and the reasons
#: below are all read off the input.
INPUT_EDGE_REASONS = tuple(r for r in REASON_PRIORITY if r not in OUTPUT_EDGE_REASONS)

#: Strings that mean "there is no such thing". Deliberately narrow, and it does
#: not include a bare ``None``: upstream writes ``null`` for a print-style
#: problem whose answer was never captured, and calling that "not found" would be
#: a guess about intent. That case gets its own reason instead.
_NOT_FOUND_STRINGS = ("-1", "not found", "not_found", "notfound", "n/a", "none found")


# ── Rendering ─────────────────────────────────────────────────────────


def render_value(value: Any, width: int = DEFAULT_WIDTH) -> str:
    """Render ``value`` as a readable Python literal, truncating explicitly.

    One line if it fits in ``width``; one item per line if it does not. Strings
    are quoted, containers are ``[...]``/``{...}``, and anything that had to be
    cut ends in :data:`ELLIPSIS`.

    Two elision conventions, deliberately distinguishable:

    * a cut *string* ends inside its own quotes -- ``'the quick br...`` -- and
      means "this literal is longer than what is shown";
    * a cut *container* keeps its closing bracket and puts ``...`` where the
      dropped items were -- ``[1, 2, 3, ...]`` -- and is only ever cut at an
      item boundary, so a quoted string inside it is never cut in half.

    Never raises. A value with no sane ``repr`` renders as ``<unrepresentable>``,
    because one bizarre test case must not take down the report of 5,132 others.
    """
    return _render(value, _check_width(width))[0]


def _check_width(width: int) -> int:
    if width < MIN_WIDTH:
        raise ValueError(f"width must be at least {MIN_WIDTH}, got {width}")
    return width


def _render(value: Any, width: int) -> tuple[str, bool]:
    """Render, and report whether any *data* was elided.

    Note the distinction from layout: a list that did not fit goes multi-line
    without anything being lost, and that is not truncation. Truncation means a
    ``...`` stands in for values the reader cannot see.
    """
    if isinstance(value, str):
        # Strings get their own bounded path so the result is always a
        # well-formed quoted literal. Clipping a rendered string could leave an
        # unterminated quote, which is the opposite of unambiguous.
        return _inline_str(value, width)

    if _is_container(value):
        inline, elided = _inline(value, width)
        if not elided and len(inline) <= width:
            return inline, False
        block = _block(value, width)
        if block is not None:
            return block
        return _clip_container(inline, _open_char(value), _close_char(value), width)

    return _clip(_scalar(value), width)


def _is_container(value: Any) -> bool:
    return isinstance(value, (list, tuple, set, frozenset, dict))


def _open_char(value: Any) -> str:
    if isinstance(value, dict):
        return "{"
    if isinstance(value, tuple):
        return "("
    if isinstance(value, (set, frozenset)):
        return "{"
    return "["


def _close_char(value: Any) -> str:
    if isinstance(value, dict):
        return "}"
    if isinstance(value, tuple):
        return ")"
    if isinstance(value, (set, frozenset)):
        return "}"
    return "]"


def _scalar(value: Any) -> str:
    """Render a non-container, tolerating a value that cannot represent itself."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, float) and not math.isfinite(value):
        # ``nan`` and ``inf`` are not Python literals; a reader copying them
        # into a test gets a NameError instead of the value.
        return f"float('{value!r}')"
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"


def _inline_str(text: str, budget: int) -> tuple[str, bool]:
    """Quote a string, cutting the content rather than the quotes."""
    full = _scalar(text)
    if len(full) <= budget:
        return full, False
    # Content budget: two quotes plus the marker. Then shrink until the rendered
    # prefix fits, because repr() can escape characters and grow.
    keep = max(1, budget - 5)
    for _ in range(4):
        head = repr(text[:keep])
        candidate = f"{head[:-1]}{ELLIPSIS}{head[-1]}"
        if len(candidate) <= budget or keep <= 1:
            return candidate, True
        keep = max(1, keep - (len(candidate) - budget))
    return candidate, True


def _inline(value: Any, budget: int) -> tuple[str, bool]:
    """Render on a single line, eliding the middle of a container if needed.

    The elided flag is an OR over the whole subtree, not just this level. A
    child's ``...`` is this level's ``...`` too: reporting "not truncated"
    because the *outer* bracket happened to fit would be a lie about the text
    the reader is looking at.
    """
    if isinstance(value, str):
        return _inline_str(value, budget)
    if isinstance(value, dict):
        share = max(MIN_WIDTH, budget // max(2, len(value) * 2))
        rendered = [(_inline(k, share), _inline(v, share)) for k, v in value.items()]
        parts = [f"{key[0]}: {item[0]}" for key, item in rendered]
        text, cut = _join(parts, "{", "}", budget)
        return text, cut or any(k[1] or v[1] for k, v in rendered)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        share = max(MIN_WIDTH, budget // max(2, len(items)))
        elided = False
        if len(items) > INLINE_ITEM_CAP:
            # Too many to render usefully. Show the ends; the middle of a
            # 121+-element list is not what makes it an interesting test case.
            half = INLINE_ITEM_CAP // 2
            shown = [_inline(v, share) for v in items[:half]]
            shown += [_inline(v, share) for v in items[-half:]]
            parts = [pair[0] for pair in shown]
            elided = True
        else:
            rendered = [_inline(v, share) for v in items]
            parts = [pair[0] for pair in rendered]
        text, cut = _join(parts, _open_char(value), _close_char(value), budget)
        children = shown if elided else rendered
        return text, cut or elided or any(pair[1] for pair in children)
    return _scalar(value), False


def _join(parts: list[str], open_ch: str, close_ch: str, budget: int) -> tuple[str, bool]:
    full = f"{open_ch}{', '.join(parts)}{close_ch}" if parts else f"{open_ch}{close_ch}"
    if len(full) <= budget:
        return full, False
    if len(parts) <= 1:
        return full, True
    keep = 1 if len(parts) <= 4 else max(2, len(parts) // 4)
    head = parts[:keep]
    tail = parts[-keep:] if keep * 2 < len(parts) else []
    out = f"{open_ch}{', '.join(head + [ELLIPSIS] + tail)}{close_ch}"
    if len(out) <= budget:
        # Items were dropped even though the result happens to fit. Reporting
        # "not truncated" here would tell a consumer that the ``...`` it can see
        # is part of the data.
        return out, True
    return _clip_container(out, open_ch, close_ch, budget)


def _block(value: Any, width: int) -> tuple[str, bool] | None:
    """One item per line. ``None`` when there is nothing to gain from it."""
    items = list(value.keys()) + list(value.values()) if isinstance(value, dict) else list(value)
    if not items:
        return None
    inner = max(MIN_WIDTH, width - 4)
    if isinstance(value, dict):
        rendered = [(_inline(k, inner), _inline(v, inner)) for k, v in value.items()]
        lines = [f"{key[0]}: {item[0]}," for key, item in rendered]
        cut = any(key[1] or item[1] for key, item in rendered)
    else:
        rendered = [_inline(item, inner) for item in items]
        lines = [f"{pair[0]}," for pair in rendered]
        cut = any(pair[1] for pair in rendered)
    if len(lines) > BLOCK_MAX_LINES:
        half = (BLOCK_MAX_LINES - 1) // 2
        lines = lines[:half] + [f"{ELLIPSIS},"] + lines[-half:]
        cut = True
    body = "\n".join("    " + line for line in lines)
    return f"{_open_char(value)}\n{body}\n{_close_char(value)}", cut


def _clip(text: str, width: int) -> tuple[str, bool]:
    """Cut a scalar rendering, preferring a break after a separator."""
    if len(text) <= width:
        return text, False
    keep = max(1, width - len(ELLIPSIS))
    head = text[:keep]
    for sep in (", ", " ", ","):
        cut = head.rfind(sep)
        if cut > keep // 2:
            head = head[: cut + len(sep)]
            break
    return f"{head.rstrip()}{ELLIPSIS}", True


def _clip_container(text: str, open_ch: str, close_ch: str, width: int) -> tuple[str, bool]:
    """Cut a container rendering but keep its closing bracket.

    Only ever called when a one-line container did not fit and a block could not
    be produced. Prefers a break after ``", "`` so the cut lands between items
    and never inside a quoted string.
    """
    if len(text) <= width:
        return text, False
    room = max(1, width - len(ELLIPSIS) - len(close_ch) - 1)
    head = text[:room]
    cut = head.rfind(", ")
    if cut > 0:
        head = head[: cut + 1]
    head = head.rstrip()
    if head.endswith(ELLIPSIS):
        # Already elided by _join. One marker is enough; two reads as a nested
        # omission, which is not a thing.
        head = head[: -len(ELLIPSIS)].rstrip()
    return f"{head}{ELLIPSIS}{close_ch}", True


# ── Value kinds ───────────────────────────────────────────────────────


def _type_group(value: Any) -> str:
    """Coarse type used for classification: number/string/boolean/none/list/dict."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "list"
    return "other"


def _shape_name(value: Any) -> str:
    if isinstance(value, dict):
        return "dictionary"
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, (set, frozenset)):
        return "set"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    return _type_group(value)


def _is_empty(value: Any) -> bool:
    return isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)) and len(value) == 0


def _elements(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return []


def _has_nested(value: Any) -> bool:
    return any(_is_container(item) for item in _elements(value))


def input_kind(case: TestCase) -> str:
    """Classify a case's input shape. One of :data:`VALUE_KINDS`.

    Precedence, and why:

    1. ``none`` -- no arguments at all, or every argument is ``None``.
    2. ``empty`` -- arguments exist and every one is an empty container or string.
    3. ``nested`` -- a container inside a container. The most specific structural
       fact available, so it outranks everything below it.
    4. ``mixed`` -- two container kinds at once, or two scalar kinds at once.
    5. ``list`` / ``dict`` -- a container is present. A container names the shape
       of the data, so it wins over a scalar travelling alongside it:
       ``(["a", "b", "a"], "a")`` is ``list``, not ``mixed``, because what the
       function operates *on* is a list.
    6. ``numbers`` / ``strings`` / ``boolean`` -- one scalar kind.

    Deterministic and total: it never raises and never returns a kind outside
    :data:`VALUE_KINDS`.
    """
    values = _case_values(case)
    if not values or all(v is None for v in values):
        return "none"
    if all(_is_empty(v) for v in values):
        return "empty"
    if any(_has_nested(v) for v in values):
        return "nested"
    groups = {_type_group(v) for v in values}
    containers = groups & {"list", "dict"}
    if len(containers) > 1:
        return "mixed"
    if containers:
        return "list" if "list" in containers else "dict"
    if "other" in groups:
        return "mixed"
    if len(groups) == 1:
        return {"number": "numbers", "string": "strings", "boolean": "boolean"}[groups.pop()]
    return "mixed"


def _case_values(case: TestCase) -> list[Any]:
    return list(case.args) + list(case.kwargs.values())


def describe_inputs(case: TestCase, kind: str | None = None) -> str:
    """A short phrase for the input, derived from it: "a list of numbers".

    This is the payload the ``input_kind`` classification exists for. It names
    what the data *is* and stops there -- it will say "a list of strings" and
    not "a list of words", because a string in this library is as likely to be a
    sentence as a word, and guessing would be exactly the invented detail this
    whole tool exists to avoid.
    """
    values = _case_values(case)
    kind = kind or input_kind(case)
    if kind == "none":
        return "no arguments"
    phrases = [_phrase(v) for v in values if not (v is None and len(values) > 1)]
    phrases = [p for p in phrases if p]
    if not phrases:
        return "no arguments"
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    if len(phrases) == 3:
        return f"{phrases[0]}, {phrases[1]} and {phrases[2]}"
    return f"{phrases[0]}, {phrases[1]}, and {len(phrases) - 2} more"


def _phrase(value: Any) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "a flag"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "an empty string" if not value else "a string"
    if _is_empty(value):
        return f"an empty {_shape_name(value)}"
    if isinstance(value, dict):
        inner = {_type_group(v) for v in value.values()}
        if len(inner) == 1:
            only = inner.pop()
            if only in ("number", "string", "boolean"):
                return f"a dictionary of {only}s"
        return "a dictionary"
    inner = {_type_group(v) for v in _elements(value)}
    shape = _shape_name(value)
    if len(inner) == 1:
        only = inner.pop()
        if only in ("number", "string", "boolean"):
            return f"a {shape} of {only}s"
        if only == "list":
            return f"a {shape} of lists"
        if only == "dict":
            return f"a {shape} of dictionaries"
    if len(value) == 1:
        return f"a {shape} with one element"
    return f"a {shape}"


# ── Edge detection ────────────────────────────────────────────────────


def _hashable_key(value: Any) -> str:
    """A key that works for unhashable elements.

    ``repr`` rather than ``str``: ``1`` and ``"1"`` must not count as duplicates,
    and ``str`` cannot tell them apart.
    """
    return _scalar(value)


def _is_sorted(value: Any) -> bool:
    items = _elements(value)
    if len(items) < 2 or isinstance(value, dict):
        return False
    if not all(_type_group(i) == "number" for i in items):
        return False
    return items == sorted(items)


def _input_reasons(values: list[Any]) -> list[str]:
    reasons: list[str] = []
    for value in values:
        if _is_empty(value):
            reasons.append(f"empty {_shape_name(value)}")
        elif isinstance(value, str):
            # Only unambiguous length facts about a string. A one-character
            # string is far more often a search key or a delimiter than the
            # problem's whole input -- `count(x, "a")` is not a "single element"
            # test case -- and repeated *letters* are not duplicate elements.
            # Emptiness and length are not ambiguous, so those stay.
            if len(value) > LONG_VALUE_MAX:
                reasons.append("very long")
        elif _is_container(value):
            items = _elements(value)
            if len(items) == 1:
                reasons.append("single element")
            elif len(items) > LONG_VALUE_MAX:
                reasons.append("very long")
            if _is_sorted(value):
                # Numbers only, and only when already in order. Detectable, and
                # the reason a naive "sort it" implementation can differ from an
                # idiomatic one.
                reasons.append("already sorted")
            # Duplicate check is for containers, not strings: a repeated letter
            # in "hello" is not an interesting test case, a repeated element in a
            # list usually is.
            if len({_hashable_key(i) for i in items}) < len(items):
                reasons.append("duplicate elements")
        for number in _numbers(value):
            if number == 0:
                reasons.append("zero")
            elif number < 0:
                reasons.append("negative")
    for value in values:
        # A unit value is a boundary only when it is the *whole* input. The 1 in
        # [1, 2, 3] is an element, not an edge case, and flagging it made this
        # the most common reason in the library by a factor of six -- a label
        # that fires on a third of all cases tells a writer nothing.
        if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) == 1:
            reasons.append("boundary value")
    return reasons


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, str):
        return []
    out: list[float] = []
    for item in _elements(value):
        out.extend(_numbers(item))
    return out


def _output_reasons(expected: Any) -> list[str]:
    reasons: list[str] = []
    if expected is None:
        reasons.append("no expected value recorded")
        return reasons
    if isinstance(expected, str) and expected.strip().lower() in _NOT_FOUND_STRINGS:
        reasons.append("not found")
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and expected == -1:
        # -1 as the *whole* answer is the library's absence sentinel: 195 test
        # cases record exactly that, and nothing else is used this way.
        #
        # Deliberately not scanned deeper. `countdown_by_step(1, 2)` is
        # `[1, -1]`, a countdown reaching below zero, and `find_word` is
        # `[False, -1]` -- flagging those as "not found" was visibly wrong in the
        # first run of this tool over the real library, and a wrong edge label
        # is worse than a missing one: a writer who trusts it writes a sentence
        # that is not true of the problem.
        reasons.append("not found")
    # One level into containers, because an empty *part* of a structured answer
    # is a real edge and says something an input-side reason cannot.
    for item in [expected, *_elements(expected)]:
        if _is_empty(item):
            reasons.append("empty result")
    return reasons


def _ordered(reasons: list[str]) -> list[str]:
    """Deduplicate and sort into :data:`REASON_PRIORITY`.

    A reason not in the list is kept rather than dropped, and sorts last. A new
    reason that someone adds without updating the vocabulary should still reach
    the report; silently filtering it would make the report look complete while
    quietly not being so.
    """

    def key(reason: str) -> int:
        return REASON_PRIORITY.index(reason) if reason in REASON_PRIORITY else len(REASON_PRIORITY)

    return sorted(set(reasons), key=key)


def edge_reasons(case: TestCase) -> list[str]:
    """Every detectable edge reason for one case, in canonical order.

    Input-derived reasons apply to hidden cases; expected-derived reasons do
    not, because the reason is a statement about the answer. See the module
    docstring.
    """
    reasons = _input_reasons(_case_values(case))
    if not case.is_hidden:
        reasons += _output_reasons(case.expected)
    return _ordered(reasons)


def _shape_signature(kind: str, reasons: list[str]) -> tuple[str, tuple[str, ...]]:
    """Identity of a *fact* rather than of a test case: same shape, same reasons.

    This library's problems are heavily generated -- 36 cases each, 12 visible
    and 24 hidden, of which many differ only by a ``_37`` suffix. Deduplicating
    by the exact call left a fact base whose six "worked examples" were the same
    fact six times. Two cases that share a shape and a reason profile are
    saying one thing, so they collapse to their lowest-index representative and
    the count of the others is reported alongside.
    """
    return (kind, tuple(reasons))


def edge_cases(
    cases: list[TestCase],
    function_name: str = "",
    *,
    limit: int | None = None,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """The cases worth calling out, each with a machine-readable reason.

    Deduplicated by :func:`_shape_signature` by default, so one problem reports
    one "empty list" fact rather than twenty copies of it -- and the
    ``same_shape`` count on each entry says how many cases were folded into it.
    """
    found: list[dict[str, Any]] = []
    counts: dict[tuple[str, tuple[str, ...]], int] = {}
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for case in cases:
        if not case.usable:
            continue
        reasons = edge_reasons(case)
        if not reasons:
            continue
        kind = input_kind(case)
        signature = _shape_signature(kind, reasons)
        counts[signature] = counts.get(signature, 0) + 1
        if dedupe:
            if signature in seen:
                # Retro-actively note the extra copies on the entry already kept.
                for entry in found:
                    if _shape_signature(entry["input_kind"], entry["reasons"]) == signature:
                        entry["same_shape"] = counts[signature]
                        break
                continue
            seen.add(signature)
        example = worked_example(case, function_name)
        found.append(
            {
                "index": case.index,
                "call": example["call"],
                "reasons": reasons,
                "primary_reason": reasons[0],
                "input_kind": kind,
                "is_hidden": case.is_hidden,
                "same_shape": counts[signature],
            }
        )
        if limit is not None and len(found) >= limit:
            break
    return found


# ── Worked examples ───────────────────────────────────────────────────


def expected_state(expected: Any) -> str:
    """``recorded`` / ``missing`` / ``prose`` / ``placeholder``.

    Delegates the recorded judgement to :func:`ducky.content.expectation_defect`
    rather than keeping a second one. That function exists precisely because
    this classification had already been written twice and the copies drifted:
    the classifier called ``"Zero"`` prose while a repair tool called only
    ``"result"`` a placeholder. A third copy would be the same bug.
    """
    if expected is None:
        return "missing"
    defect = expectation_defect(expected)
    if defect is Defect.EXPECTED_IS_PROSE:
        return "prose"
    if defect is not None:
        return "placeholder"
    return "recorded"


def worked_example(
    case: TestCase,
    function_name: str,
    *,
    width: int = DEFAULT_WIDTH,
) -> dict[str, Any]:
    """Turn one test case into a worked example a writer can use.

    The ``call`` is the invocation as the test performs it -- positional
    arguments then keyword arguments, both rendered by :func:`render_value` --
    so ``count_occurrences(["a", "b", "a"], "a")``.

    ``expected`` is :data:`EXPECTED_HIDDEN` for a hidden case. Not "omitted", not
    empty: an explicit marker survives serialisation and cannot be mistaken for
    a real answer.
    """
    width = _check_width(width)
    kind = input_kind(case)

    args_text, args_truncated = _render_parts(case.args, width)
    kwargs_text, kwargs_truncated = _render_kwargs(case.kwargs, width)
    call_parts = [p for p in (args_text, kwargs_text) if p]
    callee = function_name or "<no function name>"
    call = f"{callee}({', '.join(call_parts)})"

    state = expected_state(case.expected)
    if case.is_hidden:
        expected_text, expected_truncated = EXPECTED_HIDDEN, False
    else:
        expected_text, expected_truncated = _render(case.expected, width)

    reasons = edge_reasons(case)
    return {
        "index": case.index,
        "description": case.description,
        "call": call,
        "expected": expected_text,
        "args_text": args_text,
        "kwargs_text": kwargs_text,
        "input_kind": kind,
        "input_phrase": describe_inputs(case, kind),
        "is_hidden": case.is_hidden,
        "is_edge_case": bool(reasons),
        "edge_reasons": reasons,
        "expected_state": state,
        # The three together are what a writer should actually quote. A hidden
        # case, an unrecorded expectation, and the prose label "Zero" are all
        # facts about the library, not examples of the problem.
        "teachable": bool(case.usable) and not case.is_hidden and state == "recorded",
        "truncated": bool(args_truncated or kwargs_truncated or expected_truncated),
    }


def _render_parts(values: list[Any], width: int) -> tuple[str, bool]:
    rendered = [_render(v, width) for v in values]
    return ", ".join(text for text, _ in rendered), any(cut for _, cut in rendered)


def _render_kwargs(kwargs: dict[str, Any], width: int) -> tuple[str, bool]:
    pairs = [(name, _render(value, width)) for name, value in kwargs.items()]
    text = ", ".join(f"{name}={rendered}" for name, (rendered, _) in pairs)
    return text, any(cut for _, (_, cut) in pairs)


def _value_score(worked: list[dict[str, Any]], edges: list[dict[str, Any]], kinds: int) -> int:
    """Rank problems by how much a writer can learn from their test data."""
    teachable = sum(1 for w in worked if w["teachable"])
    return (
        (8 if teachable else 0)
        + (4 if edges else 0)
        + min(teachable, 4)
        + min(len(edges), 4)
        + min(kinds, 3)
    )


def _tier(case: TestCase) -> tuple:
    """Sort key for choosing which cases earn a slot in the fact base."""
    if not case.usable:
        return (4, 0, case.index)
    if case.is_hidden:
        return (2, 0, case.index)
    if expected_state(case.expected) != "recorded":
        return (1, 0, case.index)
    return (0, 0 if edge_reasons(case) else 1, case.index)


def derive(
    problem: ProblemSpec,
    *,
    width: int = DEFAULT_WIDTH,
    max_worked: int = MAX_WORKED,
    max_edges: int = MAX_EDGES,
) -> dict[str, Any]:
    """Build the fact base for one problem.

    This is the whole deliverable: what a rewriting agent or a teacher reads
    before writing a sentence about the problem. Every field is either measured
    from the test data or copied from the spec; none of it is a judgement about
    what the problem is "about".
    """
    width = _check_width(width)
    usable = [tc for tc in problem.test_cases if tc.usable]

    # Ordering is the fact base's main editorial decision. Within a tier -- a
    # teachable visible example beats a visible placeholder beats a hidden one --
    # a case with a detected edge comes first, because that is the material a
    # generic explanation never mentions. Within that, duplicates of a shape
    # already taken are skipped so the slots go to six different facts.
    ranked = sorted(usable, key=_tier)
    worked: list[dict[str, Any]] = []
    taken: set[tuple[str, tuple[str, ...]]] = set()
    for case in ranked:
        example = worked_example(case, problem.function_name, width=width)
        signature = _shape_signature(example["input_kind"], example["edge_reasons"])
        if signature in taken:
            continue
        taken.add(signature)
        worked.append(example)
    worked.sort(key=lambda w: (not w["teachable"], not w["is_edge_case"], w["index"]))

    edges = edge_cases(usable, problem.function_name, limit=max_edges)
    kinds = sorted({w["input_kind"] for w in worked} | {e["input_kind"] for e in edges},
                   key=VALUE_KINDS.index)
    teachable = [w for w in worked if w["teachable"]]
    visible = [w for w in worked if not w["is_hidden"]]

    return {
        "slug": problem.slug,
        "function_name": problem.function_name,
        "parameters": list(problem.parameters),
        "signature": problem.function_signature,
        "worked": worked[:max_worked],
        "edges": edges,
        "value_kinds": kinds,
        "difficulty": problem.difficulty,
        "tags": list(problem.tags),
        # --- context the fields above cannot supply on their own ---
        "title": problem.title,
        "unit": problem.unit,
        "lesson": problem.lesson,
        "topic_tier": problem.topic_tier,
        "status": problem.status.value,
        "callable": bool(problem.function_name),
        "cases_total": len(problem.test_cases),
        "cases_usable": len(usable),
        "cases_hidden": sum(1 for tc in usable if tc.is_hidden),
        "cases_teachable": len(teachable),
        "worked_kept": min(len(worked), max_worked),
        "edge_count": sum(1 for tc in usable if edge_reasons(tc)),
        "edges_kept": len(edges),
        "edge_reasons": dict(Counter(r for e in edges for r in e["reasons"]).most_common()),
        "truncated": any(w["truncated"] for w in worked),
        # The brief's definition, kept as a boolean so a consumer never has to
        # re-derive it: edges exist AND there is something non-hidden to show.
        "valuable": bool(edges) and bool(visible),
        "value_score": _value_score(worked, edges, len(kinds)),
    }


# ── Report ────────────────────────────────────────────────────────────


def build_report(
    db_path: str | Path,
    *,
    top: int,
    slug: str = "",
    limit: int = 0,
    width: int = DEFAULT_WIDTH,
) -> dict[str, Any]:
    """Derive every problem in ``db_path`` and select the ``top`` most valuable.

    Returns an empty-but-valid report when nothing matched rather than raising,
    so a caller can decide what an empty library means for it. ``main`` turns
    that into exit code 2.
    """
    started = time.monotonic()
    facts: list[dict[str, Any]] = []
    kind_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    phrase_counts: Counter[str] = Counter()

    for spec in iter_problems(db_path):
        if slug and spec.slug != slug:
            continue
        if limit and len(facts) >= limit:
            break
        fact = derive(spec, width=width)
        for worked in fact["worked"]:
            if worked["teachable"]:
                kind_counts[worked["input_kind"]] += 1
                phrase_counts[worked["input_phrase"]] += 1
        for edge in fact["edges"]:
            reason_counts.update(edge["reasons"])
        facts.append(fact)

    facts.sort(key=lambda f: (not f["valuable"], -f["value_score"], f["slug"]))
    selected = facts[:top]

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_db": str(db_path),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "width": width,
        "matched": bool(facts),
        "totals": {
            "problems": len(facts),
            "problems_with_usable_example": sum(1 for f in facts if f["cases_usable"]),
            "problems_with_teachable_example": sum(1 for f in facts if f["cases_teachable"]),
            "problems_with_edges": sum(1 for f in facts if f["edge_count"]),
            "problems_valuable": sum(1 for f in facts if f["valuable"]),
            "problems_without_edges": sum(1 for f in facts if not f["edge_count"]),
            "selected": len(selected),
        },
        "value_kinds": dict(kind_counts.most_common()),
        "input_phrases": dict(phrase_counts.most_common(20)),
        "edge_reasons": dict(reason_counts.most_common()),
        "selected_slugs": [f["slug"] for f in selected],
        "problems": selected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to the upstream coding_platform.db")
    parser.add_argument("--out", default="reports/examples.json")
    parser.add_argument("--slug", default="", help="derive only this problem")
    parser.add_argument("--limit", type=int, default=0, help="derive at most N problems")
    parser.add_argument(
        "--top",
        type=int,
        required=True,
        help="emit the K highest-value problems (value = has edges AND a non-hidden worked example)",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"error: no such database: {db}", file=sys.stderr)
        return 2

    report = build_report(db, top=args.top, slug=args.slug, limit=args.limit, width=args.width)
    if not report["matched"]:
        print(f"error: no problems matched (db={db}, slug={args.slug!r})", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.quiet:
        totals = report["totals"]
        print(f"{'=' * 66}")
        print(f"problems scanned      : {totals['problems']}")
        print(f"  with usable example : {totals['problems_with_usable_example']}")
        print(f"  with teachable      : {totals['problems_with_teachable_example']}")
        print(f"  with edge cases     : {totals['problems_with_edges']}")
        print(f"  valuable            : {totals['problems_valuable']}")
        print(f"{'=' * 66}")
        print("value kinds (teachable worked examples):")
        for kind, count in sorted(report["value_kinds"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {kind}")
        print("edge reasons:")
        for reason, count in sorted(report["edge_reasons"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {reason}")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
