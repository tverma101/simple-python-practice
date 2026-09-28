#!/usr/bin/env python3
"""Give "non-generic" a number, then measure the library against it.

The brief behind this tool is that problem explanations must be non-generic,
customised to the problem, and in plain words. Nothing measured that, so the
first job here is the definition and the second is the measurement -- and the
definition has to be mechanical, because a metric that cannot fail is a
metric that cannot be argued with.

Every measurement in this module is derived from the library it is run
against. Thresholds are measured, printed, and written into the report, so a
reader can disagree with a number without having to re-read the code. The only
fixed constants are the two sentence-length limits in :data:`SENTENCE_WORDS_MAX`
and :data:`SENTENCE_WORDS_HARD_MAX`, which are round numbers taken from
plain-language benchmarks rather than fitted to the data; the report prints the
library's own sentence-length distribution next to them so the reader can see
whether that choice flatters or punishes it.

The load-bearing measurement is duplication. "Generic" is an accusation, and
an accusation needs evidence: two problems whose normalised descriptions are
byte-identical is proof of reuse, not an inference from vocabulary. The
n-gram table is second: it names the phrases a generator emits everywhere
("return the result", "handle common edge cases from the test suite"), which
tells you what to delete. Everything after that is a guard rail.

The scoring module is pure. Nothing in :func:`normalise` through
:func:`rewrite_manifest` opens a database, reads a file, or writes one; the
CLI at the bottom is the only part that touches SQLite or disk, and it does
nothing but hand plain rows to the pure functions. That split is what makes
the numbers testable with synthetic input.

What the score is for, and what it is not: it ranks work. It does not decide
whether a description is good. A single number hides the axis that failed,
which is the axis an author needs, so :meth:`Score.failed` and
:meth:`Score.evidence` are reported alongside every total.

Usage:
    python tools/explain_metrics.py --db content.db
    python tools/explain_metrics.py --db content.db --rewrite-manifest 200
    python tools/explain_metrics.py --db content.db --json --quiet

Output paths default to ``reports/explain_metrics.json`` and
``reports/rewrite_manifest.json``. They are deliberately *not*
``reports/explanations.json``: that file is the output of
``tools/analyse_explanations.py`` and this tool must not overwrite it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = "/tmp/probe.db"
DEFAULT_VERIFIED = "reports/verification.json"
DEFAULT_REPORT = "reports/explain_metrics.json"
DEFAULT_MANIFEST = "reports/rewrite_manifest.json"

PASS_THRESHOLD = 70
MIN_STATEMENT_WORDS = 4
SENTENCE_WORDS_MAX = 20
SENTENCE_WORDS_HARD_MAX = 30
NOMINALISATION_RUN = 3
BOILERPLATE_MIN_SHARE = 0.3
BOILERPLATE_N_MIN = 2
BOILERPLATE_N_MAX = 5

PROBE_PHRASES = (
    "return the result",
    "return the result from the function",
    "instead of printing",
    "your code here",
    "implement this function",
    "handle common edge cases from the test suite",
    "match behavior shown in the examples and tests",
    "use the examples to determine the expected return value",
    "inputs",
    "rules",
    "examples",
)

#: Whole clauses the generator emits, used by :func:`shared_sentence_audit`.
#: These are the sentences a reader would delete on sight, which makes them a
#: better "is this templated" signal than any single-body score.
SHARED_SENTENCE_PROBES = (
    "return the result from the function instead of printing it",
    "handle common edge cases from the test suite",
    "match behavior shown in the examples and tests",
    "use the examples to determine the expected return value",
    "return the result",
    "instead of printing",
    "do not modify the input list",
)

COMPONENT_WEIGHTS: dict[str, int] = {
    "not_duplicated": 25,
    "has_statement": 20,
    "no_placeholder": 20,
    "names_the_subject": 10,
    "has_a_concrete_detail": 10,
    "length_in_range": 10,
    "plain_words": 5,
}

COMPONENT_DEFINITIONS: dict[str, str] = {
    "not_duplicated": "the normalised body is non-empty and no other problem shares it",
    "has_statement": "the body is non-empty and says more than the title restated",
    "no_placeholder": "no unrendered template syntax and no unrendered marker in a non-empty body",
    "names_the_subject": "the body names the function or one of its parameters",
    "has_a_concrete_detail": "the body contains a number, a literal, a call, or a quoted example",
    "length_in_range": "the body length falls inside the library's measured interquartile band",
    "plain_words": "no sentence over the plain-language limit and no stacked nominalisations",
}

#: The weights above are editorial; the components are measured. The ordering
#: says what "generic" mostly means: text reused from somewhere else, or text
#: that is not a statement, dominates. Wording tests cannot outweigh proof of
#: reuse, so they carry 25 points between them.
WEIGHT_RATIONALE = (
    "not_duplicated and no_placeholder carry half the score because they are the two "
    "defects that make a description useless rather than merely bland. The wording "
    "components (names_the_subject, has_a_concrete_detail, plain_words) are "
    "heuristic and deliberately cannot outvote measured evidence. length_in_range is "
    "worth the least of the measured components because the library's median length is "
    "a median of templated text: it rewards being average."
)

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9_]+")
_DEF_RE = re.compile(r"\bdef\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", re.S)
_PARAM_RE = re.compile(r"([A-Za-z_]\w*)\s*(?::|=|$)")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
_CALL = re.compile(r"\b[A-Za-z_]\w*\s*\([^()\n]*\)")
_BRACKET = re.compile(r"[\[\{][^\]\}\n]*[\]\}]")
_QUOTED = re.compile(r"`[^`\n]{2,}`|\"[^\"\n]{2,}\"|'[^'\n]{2,}'")
_STATED_OUTPUT = re.compile(
    r"\b(?:returns?|prints?|outputs?|yields?|produces?|shows?)\b\s*(?:the\s+\w+\s*)?[:\-]?\s*"
    r"(?:`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\d[\w.()]*|[\[\{][^\]\}\n]*[\]\}])",
    re.I,
)
_NOMINALISATIONS = re.compile(r"\w+(?:tion|sion|ment|ance|ence|ity|ness|ism|isation|ization)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_RUN_FILLERS = frozenset(
    {"of", "the", "a", "an", "and", "or", "for", "in", "on", "to", "with", "its", "their", "that", "which"}
)

PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unrendered_template", re.compile(r"\{\{|\{%|\{#")),
    ("angle_placeholder", re.compile(r"<\s*(?:placeholder|insert\b|your[\s_-]?\w+|[A-Z][A-Z_]{2,})\s*>")),
    ("todo", re.compile(r"\b(?:todo|fixme|to-do)\b", re.I)),
    ("na", re.compile(r"(?i)(?<![\w/])n\s*/\s*a(?![\w/])")),
    ("xxx", re.compile(r"\bxxx+\b", re.I)),
    ("lorem_ipsum", re.compile(r"\blorem\s+ipsum\b", re.I)),
)


# --------------------------------------------------------------------------
# data shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One problem row, reduced to the fields the metrics read."""

    slug: str
    title: str = ""
    description: str = ""
    function_signature: str = ""
    function_name: str = ""
    parameters: tuple[str, ...] = ()
    difficulty: str = ""
    unit: Any = None
    topic_tier: Any = None
    source_id: Any = None

    @property
    def body(self) -> str:
        return self.description or ""

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> Spec:
        get = row.get

        def text(key: str) -> str:
            value = get(key)
            return "" if value is None else str(value)

        raw_params = get("parameters")
        params: tuple[str, ...] = ()
        if isinstance(raw_params, (list, tuple)):
            params = tuple(str(p) for p in raw_params)
        elif isinstance(raw_params, str) and raw_params.strip():
            try:
                decoded = json.loads(raw_params)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, list):
                params = tuple(str(p) for p in decoded)
            else:
                params = tuple(p.strip() for p in raw_params.split(",") if p.strip())
        name, sig_params = signature_names(text("function_signature"))
        return cls(
            slug=text("slug") or text("id"),
            title=text("title"),
            description=text("description"),
            function_signature=text("function_signature"),
            function_name=text("function_name") or name,
            parameters=params or sig_params,
            difficulty=text("difficulty"),
            unit=get("unit"),
            topic_tier=get("topic_tier"),
            source_id=get("source_id", get("id")),
        )


def coerce_spec(obj: Spec | Mapping[str, Any]) -> Spec:
    """Accept a :class:`Spec`, a dict, or a ``sqlite3.Row``-like mapping."""
    if isinstance(obj, Spec):
        return obj
    if hasattr(obj, "keys"):
        return Spec.from_mapping({key: obj[key] for key in obj})
    raise TypeError(f"cannot read a problem specification from {type(obj).__name__}")


def coerce_specs(items: Iterable[Spec | Mapping[str, Any]]) -> list[Spec]:
    return [coerce_spec(item) for item in items]


@dataclass(frozen=True)
class Score:
    """A 0-100 total plus the components that produced it.

    ``components`` maps a component name to 1 (passed) or 0 (failed).
    ``points`` maps the same names to the weight that passing earned, so the
    total is always ``sum(points.values())`` and always 0-100.
    """

    slug: str
    total: int
    components: dict[str, int]
    points: dict[str, int]
    failed: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "total": self.total,
            "components": dict(self.components),
            "points": dict(self.points),
            "failed": list(self.failed),
            "evidence": dict(self.evidence),
        }

    @property
    def passed(self) -> int:
        return sum(1 for value in self.components.values() if value)


@dataclass(frozen=True)
class Library:
    """Library-wide context that a single problem cannot supply on its own.

    Two components are relative by construction: "in range" needs the library's
    length band, and "not duplicated" needs to know what else is in the
    library. Scoring a problem without a :class:`Library` still works -- the
    band then comes from the problem itself and duplication is unverifiable,
    which is recorded in ``evidence["band_source"]`` rather than silently
    assumed.
    """

    specs: tuple[Spec, ...] = ()
    band_chars: tuple[float, float] | None = None
    duplicate_sizes: Mapping[str, int] = field(default_factory=dict)
    band_source: str = "library"

    @classmethod
    def measured(
        cls,
        specs: Sequence[Spec | Mapping[str, Any]],
        *,
        strip_function_name: bool = True,
    ) -> Library:
        rows = coerce_specs(specs)
        lengths = [len(spec.body) for spec in rows]
        return cls(
            specs=tuple(rows),
            band_chars=length_band(lengths),
            duplicate_sizes=duplicate_map(rows, strip_function_name=strip_function_name),
            band_source="library",
        )

    @classmethod
    def single(cls, spec: Spec) -> Library:
        """A one-problem library, for scoring a row outside a report."""
        return cls(specs=(spec,), band_chars=length_band([len(spec.body)]), duplicate_sizes={}, band_source="self")


@dataclass(frozen=True)
class Boilerplate:
    """Ranked n-grams plus the denominator they are ranked against."""

    rows: list[dict[str, Any]]
    total_documents: int
    min_share: float
    n_min: int = BOILERPLATE_N_MIN
    n_max: int = BOILERPLATE_N_MAX

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]

    def phrases(self) -> list[str]:
        return [row["phrase"] for row in self.rows]


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------


def signature_names(signature: str) -> tuple[str, tuple[str, ...]]:
    """Pull the function name and parameter names out of a signature string."""
    text = signature or ""
    match = _DEF_RE.search(text)
    if match is None:
        return "", ()
    name = match.group(1)
    raw = match.group(2).strip()
    if not raw:
        return name, ()
    params: list[str] = []
    for part in raw.split(","):
        candidate = _PARAM_RE.match(part.strip())
        if candidate is None:
            continue
        token = candidate.group(1)
        if token in {"self", "cls", "*", "/"} or token.startswith("*"):
            continue
        params.append(token)
    return name, tuple(params)


def word_tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _strip_words(phrase: str) -> str:
    return _WS.sub(" ", (phrase or "").strip().lower())


def _drop_phrase(haystack: str, phrase: str) -> str:
    """Remove ``phrase`` from ``haystack`` on word boundaries."""
    needle = _strip_words(phrase)
    if not needle:
        return haystack
    return re.sub(rf"(?<!\w){re.escape(needle)}(?!\w)", " ", haystack)


def shared_sentence_audit(
    specs: Sequence[Spec | Mapping[str, Any]],
    phrases: Sequence[str] = SHARED_SENTENCE_PROBES,
) -> dict[str, Any]:
    """How many bodies contain a sentence the whole library repeats.

    This is the number to act on, and it is deliberately not a score
    component. The score answers "how does this one body score"; duplication
    and length are relative and the rest are checks a template passes by
    construction. This answers "how much of the library is visibly assembled
    from a template", which no single-body score can.

    A body counts if it carries every word of a probe, in any order. Word
    order is not checked on purpose: a generator that shuffles a clause is
    still the same generator, and a strict match would under-count reuse for
    the sake of a tidier definition.
    """
    rows = coerce_specs(specs)
    counts = {phrase: 0 for phrase in phrases}
    with_any = 0
    for spec in rows:
        tokens = set(word_tokens(spec.body))
        hit = False
        for phrase in phrases:
            if set(phrase.split()) <= tokens:
                counts[phrase] += 1
                hit = True
        if hit:
            with_any += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "criterion": "the body contains every word of a probe sentence, in any order",
        "problems": len(rows),
        "problems_with_a_shared_sentence": with_any,
        "percent": round(100 * with_any / len(rows), 1) if rows else 0.0,
        "probes": [{"phrase": phrase, "problems": count} for phrase, count in ranked],
    }


def normalise(
    text: str,
    title: str = "",
    function_signature: str = "",
    *,
    strip_function_name: bool = True,
) -> str:
    """Reduce a description to the part that should differ between problems.

    Lower-cased, whitespace collapsed, with the title and the function
    signature removed. The function name goes too by default: it is the one
    token that is *guaranteed* to differ between two otherwise identical
    problems, by construction rather than by content, so leaving it in would
    hide exactly the reuse this is looking for. Pass
    ``strip_function_name=False`` to keep it and see the smaller, stricter set
    of duplicates.
    """
    body = _strip_words(text)
    if not body:
        return ""
    body = _drop_phrase(body, title)
    body = _drop_phrase(body, function_signature)
    if strip_function_name:
        name, _ = signature_names(function_signature)
        if name:
            body = re.sub(rf"(?<!\w){re.escape(name.lower())}(?!\w)", " ", body)
    body = _WS.sub(" ", body)
    return body.strip(" -:;,.#*_`\"'")


def length_band(lengths: Sequence[int], *, lower: float = 0.25, upper: float = 0.75) -> tuple[float, float]:
    """The measured interquartile band of a set of body lengths."""
    if not lengths:
        return (0.0, 0.0)
    return (percentile(lengths, lower), percentile(lengths, upper))


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile, matching the usual ``numpy`` default."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * min(max(q, 0.0), 1.0)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low]) * (1.0 - weight) + float(ordered[high]) * weight


# --------------------------------------------------------------------------
# measurement 1: exact duplication
# --------------------------------------------------------------------------


def duplicate_clusters(
    specs: Sequence[Spec | Mapping[str, Any]],
    *,
    min_size: int = 2,
    strip_function_name: bool = True,
    sample_chars: int = 240,
) -> list[dict[str, Any]]:
    """Group problems whose normalised descriptions are identical.

    Returned largest-first, so ``clusters[0]`` is the biggest pile of the same
    text in the library. This is the strongest evidence the tool produces: a
    cluster is a set of slugs sharing one string, which is reuse rather than an
    opinion about tone.
    """
    groups: dict[str, list[Spec]] = {}
    samples: dict[str, str] = {}
    for raw in specs:
        spec = coerce_spec(raw)
        key = normalise(
            spec.body,
            spec.title,
            spec.function_signature,
            strip_function_name=strip_function_name,
        )
        groups.setdefault(key, []).append(spec)
        samples.setdefault(key, _WS.sub(" ", spec.body.strip())[:sample_chars])
    clusters = [
        {
            "size": len(members),
            "slugs": sorted(member.slug for member in members),
            "titles": sorted({member.title for member in members if member.title}),
            "normalised_sha": _digest(key),
            "sample": samples[key],
        }
        for key, members in groups.items()
        if len(members) > min_size - 1
    ]
    clusters.sort(key=lambda cluster: (-cluster["size"], cluster["slugs"][0]))
    return clusters


def duplicate_map(
    specs: Sequence[Spec | Mapping[str, Any]],
    *,
    strip_function_name: bool = True,
) -> dict[str, int]:
    """Map every slug to the size of its duplicate cluster (1 when unique)."""
    groups: dict[str, int] = {}
    keys: dict[str, str] = {}
    for raw in specs:
        spec = coerce_spec(raw)
        key = normalise(
            spec.body,
            spec.title,
            spec.function_signature,
            strip_function_name=strip_function_name,
        )
        keys[spec.slug] = key
        groups[key] = groups.get(key, 0) + 1
    return {slug: groups[key] for slug, key in keys.items()}


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# measurement 2: boilerplate n-grams
# --------------------------------------------------------------------------


def ngram_counts(
    specs: Sequence[Spec | Mapping[str, Any]],
    *,
    n_min: int = BOILERPLATE_N_MIN,
    n_max: int = BOILERPLATE_N_MAX,
) -> Counter[str]:
    """Document frequency of every ``n_min``-to-``n_max`` word n-gram.

    Document frequency, not raw count: a phrase repeated ten times inside one
    description is one problem's habit, while a phrase in 1,100 descriptions is
    the generator's signature.

    N-grams are formed inside a line, never across one. Flattening the whole
    body first invents phrases that no template contains -- "input output" is
    two headings and a bullet between them, and "and tests handle common edge
    cases" is the end of one sentence joined to the start of the next. A phrase
    a generator emits is a phrase it emits in one place.
    """
    counts: Counter[str] = Counter()
    for raw in specs:
        spec = coerce_spec(raw)
        seen: set[str] = set()
        for line in (spec.body or "").splitlines():
            tokens = word_tokens(line)
            for size in range(n_min, n_max + 1):
                for start in range(len(tokens) - size + 1):
                    seen.add(" ".join(tokens[start : start + size]))
        counts.update(seen)
    return counts


def boilerplate_phrases(
    specs: Sequence[Spec | Mapping[str, Any]],
    min_share: float = BOILERPLATE_MIN_SHARE,
    *,
    n_min: int = BOILERPLATE_N_MIN,
    n_max: int = BOILERPLATE_N_MAX,
    top: int | None = None,
) -> Boilerplate:
    """N-grams present in at least ``min_share`` of descriptions, ranked.

    ``min_share`` is a fraction of *documents*, not of n-grams, so 0.5 means
    "half the library says this". Rows carry the real document count and the
    real share; the total document count is on the returned object because a
    count without its denominator is the thing this tool exists to avoid.
    """
    rows_in = coerce_specs(specs)
    total = len(rows_in)
    counts = ngram_counts(rows_in, n_min=n_min, n_max=n_max)
    floor = min_share * total
    kept = [(phrase, count) for phrase, count in counts.items() if count >= floor]
    kept.sort(key=lambda item: (-item[1], item[0]))
    if top is not None:
        kept = kept[:top]
    rows = [
        {
            "phrase": phrase,
            "words": phrase.count(" ") + 1,
            "problems": count,
            "share": round(count / total, 4) if total else 0.0,
        }
        for phrase, count in kept
    ]
    return Boilerplate(rows=rows, total_documents=total, min_share=min_share, n_min=n_min, n_max=n_max)


def maximal_phrases(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop n-grams that are fragments of a longer n-gram with at least as many problems.

    A raw n-gram ranking is a ladder: "and tests", "and tests handle", "and
    tests handle common" all score 1,114, and only the last rung says anything.
    A fragment of a phrase that appears in at least as many problems carries no
    information the longer phrase does not, so it goes. Nothing is invented
    here and no count changes -- this is a projection of the same rows, and the
    full ladder stays in the report under ``phrases_raw``.
    """
    kept: list[dict[str, Any]] = []
    for row in rows:
        words = str(row["phrase"]).split()
        fragment = False
        for other in rows:
            if other is row:
                continue
            longer = str(other["phrase"]).split()
            if len(longer) <= len(words) or other["problems"] < row["problems"]:
                continue
            if _is_sublist(longer, words):
                fragment = True
                break
        if not fragment:
            kept.append(dict(row))
    kept.sort(key=lambda row: (-row["problems"], row["phrase"]))
    return kept


def _is_sublist(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    span = len(needle)
    return any(list(haystack[start : start + span]) == list(needle) for start in range(len(haystack) - span + 1))


def named_phrase_counts(
    specs: Sequence[Spec | Mapping[str, Any]],
    phrases: Sequence[str] = PROBE_PHRASES,
) -> list[dict[str, Any]]:
    """Count the phrases the brief named, whether or not they clear ``min_share``."""
    rows_in = coerce_specs(specs)
    total = len(rows_in)
    documents = [set(word_tokens(spec.body)) for spec in rows_in]
    out: list[dict[str, Any]] = []
    for phrase in phrases:
        tokens = phrase.split()
        problems = sum(1 for tokenset in documents if set(tokens) <= tokenset)
        out.append(
            {
                "phrase": phrase,
                "problems": problems,
                "share": round(problems / total, 4) if total else 0.0,
            }
        )
    out.sort(key=lambda row: (-row["problems"], row["phrase"]))
    return out


# --------------------------------------------------------------------------
# measurement 3: placeholders
# --------------------------------------------------------------------------


def placeholder_markers(text: str, title: str = "") -> list[str]:
    """Every sign that a template was never filled in.

    Three of the classes are lexical (unrendered syntax, a TODO, a body that
    is the title again) and one is comparative (a body shorter than the title
    it is supposed to expand). Sorted and deduplicated, so the caller gets a
    set of failed checks rather than a count that depends on how many times a
    generator happened to emit ``TODO``.
    """
    body = (text or "").strip()
    found: set[str] = set()
    for name, pattern in PLACEHOLDER_PATTERNS:
        if pattern.search(body):
            found.add(name)
    if title.strip():
        if len(body) < len(title.strip()):
            found.add("shorter_than_title")
        if not word_tokens(statement_remainder(body, title)):
            found.add("title_restated")
    return sorted(found)


# --------------------------------------------------------------------------
# measurement 4: components
# --------------------------------------------------------------------------


def statement_remainder(body: str, title: str) -> str:
    """What the body says once the title has been taken back out."""
    return _WS.sub(" ", _drop_phrase(_strip_words(body), title)).strip()


def sentence_stats(text: str) -> tuple[int, float, int]:
    """``(sentence count, mean words per sentence, longest sentence)``."""
    chunks = [chunk for chunk in _SENTENCE_SPLIT.split(text or "") if chunk and chunk.strip()]
    if not chunks:
        return (0, 0.0, 0)
    counts = [len(word_tokens(chunk)) for chunk in chunks]
    return (len(counts), sum(counts) / len(counts), max(counts))


def longest_nominalisation_run(tokens: Sequence[str]) -> int:
    """Longest run of nominalisation-shaped words, ignoring filler between them.

    "the implementation of the configuration of the initialisation" is three
    stacked nouns and reads as a wall; a single "implementation" is a normal
    word. Counting runs rather than hits is the difference between measuring
    density and measuring a wall, and skipping "of the" is what lets the test
    see the stack -- English puts its filler between the nouns, not after them.
    """
    longest = 0
    run = 0
    for token in tokens:
        if _NOMINALISATIONS.fullmatch(token):
            run += 1
            longest = max(longest, run)
        elif token not in _RUN_FILLERS:
            run = 0
    return longest


def concrete_details(text: str) -> list[str]:
    """Which kinds of concrete detail the body actually contains.

    Kinds, not values: the report wants to know "this body has no literal in
    it", and a count of every number in a description would drown that. A
    stated output counts only when a value follows it -- "return the result
    from the function" states an output and gives nothing, which is exactly
    the sentence the library emits in 1,221 problems.
    """
    body = text or ""
    found: list[str] = []
    if _NUMBER.search(body):
        found.append("number")
    if _CALL.search(body):
        found.append("call")
    if _BRACKET.search(body):
        found.append("literal")
    if _QUOTED.search(body):
        found.append("quoted")
    if _STATED_OUTPUT.search(body):
        found.append("stated_output")
    return found


def names_subject(spec: Spec, *, include_signature_only: bool = False) -> list[str]:
    """Which of the function's own names the body repeats, and near misses.

    A statement that never says what the function is called, or what its
    argument is called, is not describing this problem. A body that only
    inherits the name from the signature is reported as ``signature_only`` and
    is not a pass: every row in the library has a signature, so accepting it
    would make the component measure nothing. :func:`report` counts both, so
    the effect of the stricter rule is visible rather than hidden.
    """
    body_tokens = set(word_tokens(spec.body))
    name = spec.function_name or signature_names(spec.function_signature)[0]
    parameters = spec.parameters or signature_names(spec.function_signature)[1]
    hits: list[str] = []
    if name and name.lower() in body_tokens:
        hits.append("function_name")
    for param in parameters:
        if param and param.lower() in body_tokens:
            hits.append(f"parameter:{param}")
    if hits or not include_signature_only:
        return hits
    if name:
        return [f"signature_only:{name}"]
    return []


def plain_words_report(text: str) -> dict[str, Any]:
    """The wording measurements behind the ``plain_words`` component."""
    sentences, mean_words, longest = sentence_stats(text)
    run = longest_nominalisation_run(word_tokens(text))
    long_sentence = longest > SENTENCE_WORDS_HARD_MAX
    dense_sentence = sentences > 0 and mean_words > SENTENCE_WORDS_MAX
    return {
        "sentences": sentences,
        "mean_words_per_sentence": round(mean_words, 1),
        "longest_sentence_words": longest,
        "nominalisation_run": run,
        "passes": not (long_sentence or dense_sentence or run >= NOMINALISATION_RUN),
    }


def score(problem: Spec | Mapping[str, Any], library: Library | None = None) -> Score:
    """Score one problem out of 100 against the measured library.

    Every component is gated on the body being non-empty. That is not
    bookkeeping: an empty description has no placeholders, no jargon, and
    nothing duplicated, and an ungated metric would hand it 40 points for
    being absent. Gated, an empty body scores 0 -- which is the only honest
    reading of "how good is this explanation".

    ``library`` supplies the length band and the duplicate counts. Without it
    the band is measured from this one problem and duplication is taken on
    faith; ``evidence["band_source"]`` records which happened.
    """
    spec = coerce_spec(problem)
    if library is None:
        library = Library.single(spec)

    body = spec.body.strip()
    has_body = bool(body)
    chars = len(body)
    words = len(word_tokens(body))

    remainder = statement_remainder(body, spec.title) if body else ""
    remainder_words = len(word_tokens(remainder))
    has_statement = has_body and remainder_words >= MIN_STATEMENT_WORDS

    band = library.band_chars
    if band is None:
        length_ok = 0
        band_used: list[float] | None = None
    else:
        band_used = [round(band[0], 1), round(band[1], 1)]
        length_ok = int(has_body and band[0] <= chars <= band[1])

    duplicate_size = int(library.duplicate_sizes.get(spec.slug, 1))
    not_duplicated = int(has_body and duplicate_size == 1)

    markers = placeholder_markers(spec.body, spec.title) if has_body else []
    no_placeholder = int(has_body and not markers)

    subject_hits = names_subject(spec, include_signature_only=True) if has_body else []
    names_the_subject = int(any(not hit.startswith("signature_only:") for hit in subject_hits))

    details = concrete_details(spec.body) if has_body else []
    has_a_concrete_detail = int(bool(details))

    wording = plain_words_report(spec.body) if has_body else {
        "sentences": 0,
        "mean_words_per_sentence": 0.0,
        "longest_sentence_words": 0,
        "nominalisation_run": 0,
        "passes": False,
    }
    plain_words = int(wording["passes"])

    components = {
        "has_statement": int(has_statement),
        "length_in_range": length_ok,
        "not_duplicated": not_duplicated,
        "no_placeholder": no_placeholder,
        "names_the_subject": names_the_subject,
        "has_a_concrete_detail": has_a_concrete_detail,
        "plain_words": plain_words,
    }
    points = {name: COMPONENT_WEIGHTS[name] * value for name, value in components.items()}
    failed = tuple(name for name, value in components.items() if not value)
    evidence = {
        "chars": chars,
        "words": words,
        "duplicate_size": duplicate_size,
        "markers": markers,
        "subject_hits": subject_hits,
        "concrete_detail_kinds": details,
        "statement_remainder_words": remainder_words,
        "band_chars": band_used,
        "band_source": library.band_source,
        "wording": wording,
    }
    return Score(
        slug=spec.slug,
        total=int(sum(points.values())),
        components=components,
        points=points,
        failed=failed,
        evidence=evidence,
    )


def score_all(specs: Sequence[Spec | Mapping[str, Any]], library: Library | None = None) -> list[Score]:
    if library is None:
        library = Library.measured(specs)
    return [score(spec, library) for spec in coerce_specs(specs)]


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def distribution(values: Sequence[float], *, bins: int = 10) -> dict[str, Any]:
    """Summary statistics plus a histogram, so a mean cannot hide a shape."""
    if not values:
        return {"count": 0}
    counts = Counter(int(min(max(value, 0), 100) // (100 / bins)) for value in values)
    histogram = [
        {
            "from": index * (100 // bins),
            "to": (index + 1) * (100 // bins) - 1 if index < bins - 1 else 100,
            "count": counts.get(index, 0),
        }
        for index in range(bins)
    ]
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p10": round(percentile(ordered, 0.10), 1),
        "p25": round(percentile(ordered, 0.25), 1),
        "median": round(percentile(ordered, 0.50), 1),
        "mean": round(sum(ordered) / len(ordered), 1),
        "p75": round(percentile(ordered, 0.75), 1),
        "p90": round(percentile(ordered, 0.90), 1),
        "max": ordered[-1],
        "histogram": histogram,
    }


def score_distribution(scores: Sequence[Score], threshold: int = PASS_THRESHOLD) -> dict[str, Any]:
    """Totals, the pass count at ``threshold``, and per-component failure counts.

    The pass count is also split by whether the description was duplicated.
    Without that split the number flatters itself: a description shared with
    forty other problems can still clear the threshold on the strength of the
    components it satisfies, and only the split makes that visible.
    """
    totals = [item.total for item in scores]
    summary = distribution(totals)
    failing = {name: 0 for name in COMPONENT_WEIGHTS}
    for item in scores:
        for name in item.failed:
            failing[name] += 1
    passed = [item for item in scores if item.total >= threshold]
    unique = [item for item in scores if item.evidence["duplicate_size"] == 1]
    shared = [item for item in scores if item.evidence["duplicate_size"] > 1]
    badly_shared = [item for item in shared if item.evidence["duplicate_size"] >= 10]
    summary["threshold"] = threshold
    summary["passing"] = len(passed)
    summary["failing"] = len(scores) - len(passed)
    summary["passing_percent"] = round(100 * len(passed) / len(scores), 1) if scores else 0.0
    summary["passing_by_duplication"] = {
        "unique_description": {
            "problems": len(unique),
            "passing": sum(1 for item in unique if item.total >= threshold),
        },
        "shared_description": {
            "problems": len(shared),
            "passing": sum(1 for item in shared if item.total >= threshold),
        },
        "shared_with_10_or_more": {
            "problems": len(badly_shared),
            "passing": sum(1 for item in badly_shared if item.total >= threshold),
        },
        "note": (
            "a shared description can reach 75 at most, because not_duplicated is the heaviest "
            "component, so any threshold at or below 75 admits some reused text. Read the shared rows "
            "as a failure of the threshold, not as a pass"
        ),
    }
    summary["component_failures"] = failing
    summary["component_failure_percent"] = {
        name: (round(100 * count / len(scores), 1) if scores else 0.0) for name, count in failing.items()
    }
    summary["mean_points_lost_per_component"] = {
        name: round(sum(COMPONENT_WEIGHTS[name] * item.components[name] for item in scores) / len(scores), 1)
        if scores
        else 0.0
        for name in COMPONENT_WEIGHTS
    }
    summary["thresholds_sweep"] = {
        str(mark): sum(1 for item in scores if item.total >= mark) for mark in (0, 50, 60, 70, 80, 90, 100)
    }
    return summary


def report(
    specs: Sequence[Spec | Mapping[str, Any]],
    *,
    source: Mapping[str, Any] | None = None,
    verification: Mapping[str, Any] | None = None,
    threshold: int = PASS_THRESHOLD,
    min_share: float = BOILERPLATE_MIN_SHARE,
    top: int = 25,
    strip_function_name: bool = True,
    largest_clusters: int = 10,
    n_min: int = BOILERPLATE_N_MIN,
    n_max: int = BOILERPLATE_N_MAX,
) -> dict[str, Any]:
    """The whole measurement as a JSON-ready dict.

    Pure: it takes rows and returns a dictionary. Writing that dictionary to
    disk is the CLI's job, so this function can be called from a test with four
    synthetic problems and no filesystem.
    """
    rows = coerce_specs(specs)
    library = Library.measured(rows, strip_function_name=strip_function_name)
    scores = [score(row, library) for row in rows]
    by_slug = {row.slug: row for row in rows}

    clusters = duplicate_clusters(rows, strip_function_name=strip_function_name)
    strict_clusters = duplicate_clusters(rows, strip_function_name=False)
    table = boilerplate_phrases(rows, min_share, n_min=n_min, n_max=n_max)
    floor = table.min_share * table.total_documents
    all_ngrams = ngram_counts(rows, n_min=n_min, n_max=n_max)
    kept = list(table.rows)
    table = Boilerplate(
        rows=kept[:top],
        total_documents=table.total_documents,
        min_share=table.min_share,
        n_min=table.n_min,
        n_max=table.n_max,
    )

    lengths = [len(row.body) for row in rows]
    marker_totals: Counter[str] = Counter()
    marker_problems = 0
    sentence_means: list[float] = []
    subject_only_in_signature = 0
    for row in rows:
        markers = placeholder_markers(row.body, row.title)
        marker_totals.update(markers)
        if markers:
            marker_problems += 1
        sentence_means.append(plain_words_report(row.body)["mean_words_per_sentence"])
        if any(hit.startswith("signature_only:") for hit in names_subject(row, include_signature_only=True)):
            subject_only_in_signature += 1

    problems = []
    for item in scores:
        spec = by_slug[item.slug]
        record = item.as_dict()
        record["title"] = spec.title
        record["chars"] = item.evidence["chars"]
        record["words"] = item.evidence["words"]
        record["duplicate_size"] = item.evidence["duplicate_size"]
        record["markers"] = item.evidence["markers"]
        problems.append(record)
    problems.sort(key=lambda record: (record["total"], record["slug"]))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "tools/explain_metrics.py",
        "source": dict(source or {}),
        "verification_subset": dict(verification or {}),
        "thresholds": {
            "pass_threshold": threshold,
            "boilerplate_min_share": min_share,
            "boilerplate_ngram_range": [BOILERPLATE_N_MIN, BOILERPLATE_N_MAX],
            "min_statement_words": MIN_STATEMENT_WORDS,
            "sentence_words_max": SENTENCE_WORDS_MAX,
            "sentence_words_hard_max": SENTENCE_WORDS_HARD_MAX,
            "nominalisation_run": NOMINALISATION_RUN,
            "length_band_source": "measured interquartile range of active descriptions",
        },
        "scoring": {
            "weights": dict(COMPONENT_WEIGHTS),
            "weight_rationale": WEIGHT_RATIONALE,
            "components": [
                {"name": name, "weight": COMPONENT_WEIGHTS[name], "definition": COMPONENT_DEFINITIONS[name]}
                for name in COMPONENT_WEIGHTS
            ],
            "gating": (
                "every component requires a non-empty body, so an empty description scores 0 "
                "rather than collecting the points for having no placeholders and no jargon"
            ),
        },
        "library": {
            "problems": len(rows),
            "empty_bodies": sum(1 for row in rows if not row.body.strip()),
            "length_chars": {
                "min": min(lengths) if lengths else 0,
                "p25": round(percentile(lengths, 0.25), 1),
                "median": round(percentile(lengths, 0.50), 1),
                "mean": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
                "p75": round(percentile(lengths, 0.75), 1),
                "p90": round(percentile(lengths, 0.90), 1),
                "max": max(lengths) if lengths else 0,
            },
            "length_band_chars": [round(value, 1) for value in (library.band_chars or (0.0, 0.0))],
            "sentence_words": {
                "mean_of_means": round(sum(sentence_means) / len(sentence_means), 1) if sentence_means else 0.0,
                "p75_of_means": round(percentile(sentence_means, 0.75), 1) if sentence_means else 0.0,
            },
        },
        "duplicates": {
            "pass": "normalised description identical to another problem's (title, signature and function name removed)",
            "strip_function_name": strip_function_name,
            "clusters": len(clusters),
            "problems_in_clusters": sum(cluster["size"] for cluster in clusters),
            "unique_descriptions": len({normalise(row.body, row.title, row.function_signature) for row in rows}),
            "largest": clusters[:largest_clusters],
            "sensitivity_keep_function_name": {
                "clusters": len(strict_clusters),
                "problems_in_clusters": sum(cluster["size"] for cluster in strict_clusters),
                "note": (
                    "keeping the function name in the text hides reuse between problems that differ only "
                    "in what their function is called, so this is the count of duplication that survives "
                    "even after the one guaranteed-different token is put back"
                ),
            },
        },
        "boilerplate": {
            "total_documents": table.total_documents,
            "min_share": table.min_share,
            "ngram_range": [table.n_min, table.n_max],
            "phrases_above_min_share": sum(1 for count in all_ngrams.values() if count >= floor),
            "top": table.rows,
            "top_maximal": maximal_phrases(kept)[:top],
            "maximal_note": (
                "top_maximal drops n-grams that are word-for-word fragments of a longer n-gram with at "
                "least as many problems; top keeps the full ladder. Same counts, less repetition"
            ),
            "named_probes": named_phrase_counts(rows),
        },
        "placeholders": {
            "criterion": "marker classes found in the description, plus the two comparative checks",
            "totals": dict(marker_totals.most_common()),
            "problems_with_any_marker": marker_problems,
        },
        "shared_sentence_audit": shared_sentence_audit(rows),
        "names_the_subject": {
            "criterion": "the body repeats the function name or one of its parameter names",
            "body_names_it": sum(1 for item in scores if item.components["names_the_subject"]),
            "signature_only": subject_only_in_signature,
            "note": (
                "the brief allows the signature to satisfy this component; every row has a signature, "
                "so accepting it would pass the whole library and measure nothing. The signature-only "
                "count is how many rows would pass under the looser rule"
            ),
        },
        "scores": score_distribution(scores, threshold),
        "rewrite_candidates": {
            "below_threshold": sum(1 for item in scores if item.total < threshold),
            "perfect_scores": sum(1 for item in scores if item.total == 100),
        },
        "problems": problems,
    }


def rewrite_manifest(
    specs: Sequence[Spec | Mapping[str, Any]],
    scores: Sequence[Score],
    count: int,
    *,
    shared_phrases: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """The ``count`` lowest-scoring problems, shaped for a rewriting tool.

    Sorted worst-first, then by slug so two runs over the same library emit the
    same list. ``failed_components`` is the reason field: a rewriter that
    receives only a total cannot tell "too short" from "used by 400 other
    problems", and those need different work.

    Two keys beyond the ones the brief names are added, both derived and
    optional: ``duplicate_size`` (how many problems share this text, which is
    the number that makes the row urgent) and ``shared_phrases`` (the library
    n-grams this body contains, so the rewriter is handed the sentences to
    delete rather than being told to be less generic). Consumers that expect
    the six named keys still get them.
    """
    if count <= 0:
        return []
    by_slug = {coerce_spec(spec).slug: coerce_spec(spec) for spec in specs}
    ordered = sorted(scores, key=lambda item: (item.total, item.slug))
    manifest: list[dict[str, Any]] = []
    for item in ordered[:count]:
        spec = by_slug.get(item.slug)
        if spec is None:
            continue
        entry = {
            "slug": item.slug,
            "title": spec.title,
            "function_signature": spec.function_signature,
            "current_description": spec.body,
            "score": item.total,
            "failed_components": list(item.failed),
            "duplicate_size": item.evidence.get("duplicate_size", 1),
        }
        if shared_phrases is not None:
            entry["shared_phrases"] = list(shared_phrases.get(item.slug, ()))
        manifest.append(entry)
    return manifest


def shared_phrases_by_problem(
    specs: Sequence[Spec | Mapping[str, Any]],
    counts: Mapping[str, int],
    min_share: float = BOILERPLATE_MIN_SHARE,
) -> dict[str, list[str]]:
    """Map each slug to the library-wide n-grams its own body contains.

    The join between the boilerplate table and an individual problem. Without
    it, "this phrase is everywhere" and "this problem contains that phrase"
    are two facts a human has to hold at once.
    """
    rows = coerce_specs(specs)
    floor = min_share * len(rows)
    shared = {phrase for phrase, count in counts.items() if count >= floor}
    out: dict[str, list[str]] = {}
    for spec in rows:
        found: set[str] = set()
        for line in (spec.body or "").splitlines():
            tokens = word_tokens(line)
            for size in range(BOILERPLATE_N_MIN, BOILERPLATE_N_MAX + 1):
                for start in range(len(tokens) - size + 1):
                    phrase = " ".join(tokens[start : start + size])
                    if phrase in shared:
                        found.add(phrase)
        out[spec.slug] = sorted(found, key=lambda phrase: (-len(phrase), phrase))
    return out


# --------------------------------------------------------------------------
# CLI: the only part that touches the filesystem
# --------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _pick_table(conn: sqlite3.Connection, preferred: Sequence[str]) -> tuple[str, list[str]]:
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in preferred:
        if table in existing:
            columns = _table_columns(conn, table)
            if "description" in columns:
                return table, columns
    raise SystemExit(f"error: no table with a description column among {list(preferred)}")


def load_verified_subset(path: Path) -> dict[str, Any]:
    """Read the verified subset from a verification report, if one exists.

    Absence is a fact to report, not an error: the tool then measures every
    active problem and says so in the report, because "we did not know which
    rows were verified" is different from "every row was verified".
    """
    if not path.is_file():
        return {
            "source": str(path),
            "status": "absent - measured every active problem instead",
            "verified_problems": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"source": str(path), "status": f"unreadable ({exc}) - measured every active problem", "verified_problems": None}
    quarantined = {entry.get("slug") for entry in data.get("quarantined", []) if isinstance(entry, Mapping)}
    totals = data.get("totals") or {}
    return {
        "source": str(path),
        "status": "ok",
        "quarantined": len(quarantined),
        "shippable_claim": totals.get("shippable"),
        "verified_slugs": sorted(quarantined),
    }


def verified_slugs(verification: Mapping[str, Any]) -> set[str] | None:
    slugs = verification.get("verified_slugs")
    return set(slugs) if slugs is not None else None


def fetch_rows(conn: sqlite3.Connection, table: str, columns: Sequence[str], *, only_active: bool) -> list[dict[str, Any]]:
    wanted = [name for name in columns if name != "id"] or ["slug"]
    if "slug" not in wanted and "id" in columns:
        wanted.append("slug")
    sql = f'SELECT {", ".join(chr(34) + name + chr(34) for name in wanted)} FROM "{table}"'
    if only_active:
        sql += " WHERE is_active = 1"
    cursor = conn.execute(sql)
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def _table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str], *, limit: int | None = None) -> str:
    shown = rows if limit is None else list(rows)[:limit]
    widths = [max(len(str(column)), *(len(_cell(row.get(column))) for row in shown)) if shown else len(str(column)) for column in columns]
    lines = ["  ".join(str(column).ljust(width) for column, width in zip(columns, widths, strict=True))]
    for row in shown:
        lines.append("  ".join(_cell(row.get(column)).ljust(width) for column, width in zip(columns, widths, strict=True)))
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def print_summary(result: Mapping[str, Any], *, top: int = 25, clusters: int = 10) -> None:
    source = result.get("source", {})
    verification = result.get("verification_subset", {})
    library = result["library"]
    print("=" * 78)
    print("EXPLANATION GENERICITY -- measured, not estimated")
    print("=" * 78)
    print(f"source            {source.get('db')} (table {source.get('table')}, is_active filter: {source.get('active_filter')})")
    subset = (
        f"{verification.get('verified_problems')} of {library['problems']} verified"
        if verification.get("verified_problems") is not None
        else f"verification subset {verification.get('status')} -- all {library['problems']} active problems measured"
    )
    print(f"subset            {subset}")
    print(f"empty bodies      {library['empty_bodies']}")
    print(f"length band       {library['length_band_chars'][0]:g}-{library['length_band_chars'][1]:g} chars (measured IQR)")
    print(f"median length     {library['length_chars']['median']:g} chars")

    duplicates = result["duplicates"]
    print()
    print(f"-- duplication: {duplicates['clusters']} clusters, {duplicates['problems_in_clusters']} problems share a description")
    print(f"   with the function name kept in the text: {duplicates['sensitivity_keep_function_name']['clusters']} clusters, "
          f"{duplicates['sensitivity_keep_function_name']['problems_in_clusters']} problems")
    for cluster in duplicates["largest"][:clusters]:
        print(f"   size {cluster['size']:>4}  {', '.join(cluster['slugs'][:4])}" + (" ..." if cluster["size"] > 4 else ""))
        print(f"           \"{cluster['sample'][:110]}\"")

    boilerplate = result["boilerplate"]
    print()
    print(f"-- boilerplate: {boilerplate['phrases_above_min_share']} n-grams in >={boilerplate['min_share']:.0%} "
          f"of {boilerplate['total_documents']} descriptions")
    print(_table(
        [{"phrase": row["phrase"], "problems": row["problems"], "share": f"{row['share']:.1%}"} for row in boilerplate["top_maximal"][:top]],
        ["phrase", "problems", "share"],
    ))
    print("   named probes (counted whether or not they clear the share):")
    print(_table(
        [{"phrase": row["phrase"], "problems": row["problems"], "share": f"{row['share']:.1%}"} for row in boilerplate["named_probes"]],
        ["phrase", "problems", "share"],
    ))

    placeholders = result["placeholders"]
    print()
    print(f"-- placeholders: {placeholders['problems_with_any_marker']} problems carry at least one marker")
    print(_table(
        [{"marker": name, "problems": count} for name, count in placeholders["totals"].items()],
        ["marker", "problems"],
    ))

    audit = result["shared_sentence_audit"]
    print()
    print(f"-- shared sentences: {audit['problems_with_a_shared_sentence']} of {audit['problems']} bodies "
          f"({audit['percent']:.1f}%) contain a clause the library repeats everywhere")
    print("   this is the number to act on: it does not depend on the score")
    print(_table(
        [{"clause": row["phrase"], "problems": row["problems"]} for row in audit["probes"] if row["problems"]],
        ["clause", "problems"],
    ))

    scores = result["scores"]
    print()
    print(f"-- score: median {scores['median']:g}, mean {scores['mean']:g}, max {scores['max']:g}")
    print(_table(
        [
            {
                "band": f"{bucket['from']}-{bucket['to']}",
                "problems": bucket["count"],
                "bar": "#" * max(1, round(40 * bucket["count"] / max(scores["count"], 1))),
            }
            for bucket in scores["histogram"]
            if bucket["count"]
        ],
        ["band", "problems", "bar"],
    ))
    print(f"   pass >= {scores['threshold']}: {scores['passing']} of {scores['count']} ({scores['passing_percent']:.1f}%)")
    split = scores["passing_by_duplication"]
    print("   the same count, split by whether the text is shared:")
    print(_table(
        [
            {
                "group": "unique description",
                "problems": split["unique_description"]["problems"],
                "passing": split["unique_description"]["passing"],
            },
            {
                "group": "shared description",
                "problems": split["shared_description"]["problems"],
                "passing": split["shared_description"]["passing"],
            },
            {
                "group": "shared with 10+",
                "problems": split["shared_with_10_or_more"]["problems"],
                "passing": split["shared_with_10_or_more"]["passing"],
            },
        ],
        ["group", "problems", "passing"],
    ))
    sweep = scores["thresholds_sweep"]
    print("   threshold sweep: " + "  ".join(f"{mark}->{count}" for mark, count in sweep.items()))
    print("   component failures (the axis, not the total):")
    print(_table(
        [
            {
                "component": name,
                "weight": result["scoring"]["weights"][name],
                "failed": count,
                "of": scores["count"],
                "percent": f"{scores['component_failure_percent'][name]:.1f}%",
            }
            for name, count in scores["component_failures"].items()
        ],
        ["component", "weight", "failed", "of", "percent"],
    ))
    print()
    print("   worst five:")
    for record in result["problems"][:5]:
        print(f"     {record['total']:>3}  {record['slug']:<40} failed: {', '.join(record['failed']) or '-'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="path to the read-only content database")
    parser.add_argument("--verified", default=DEFAULT_VERIFIED, help="verification report naming the quarantined slugs")
    parser.add_argument("--out", default=DEFAULT_REPORT, help="where to write the JSON report")
    parser.add_argument("--rewrite-manifest", type=int, metavar="N", default=0,
                        help="also write the N lowest-scoring problems as a JSON work-list")
    parser.add_argument("--rewrite-out", default=DEFAULT_MANIFEST, help="where to write the work-list")
    parser.add_argument("--threshold", type=int, default=PASS_THRESHOLD, help="pass mark out of 100")
    parser.add_argument("--min-share", type=float, default=BOILERPLATE_MIN_SHARE,
                        help="fraction of descriptions an n-gram must appear in")
    parser.add_argument("--ngram-max", type=int, default=BOILERPLATE_N_MAX,
                        help="longest n-gram to count; raise it to 12 to see whole shared sentences")
    parser.add_argument("--top", type=int, default=25, help="how many boilerplate n-grams to print")
    parser.add_argument("--clusters", type=int, default=10, help="how many duplicate clusters to print")
    parser.add_argument("--keep-function-name", action="store_true",
                        help="do not strip the function name when normalising (under-reports duplication)")
    parser.add_argument("--all-active", action="store_true",
                        help="ignore the verification subset and measure every active problem")
    parser.add_argument("--json", action="store_true", help="print the report to stdout instead of the tables")
    parser.add_argument("--quiet", action="store_true", help="suppress the tables")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: no such database: {db_path}", file=sys.stderr)
        return 2

    started = time.monotonic()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"error: cannot open {db_path}: {exc}", file=sys.stderr)
        return 2
    with conn:
        table, columns = _pick_table(conn, ("coding_problems", "problems"))
        active_filter = "is_active" in columns
        rows = fetch_rows(conn, table, columns, only_active=active_filter)

    specs = [Spec.from_mapping(row) for row in rows]
    if not specs:
        print(f"error: {db_path}:{table} has no rows to measure", file=sys.stderr)
        return 2

    verification = load_verified_subset(Path(args.verified))
    if args.all_active or verification.get("status") != "ok":
        verification["measured"] = len(specs)
        verification["verified_problems"] = None
        measured = specs
    else:
        excluded = verified_slugs(verification) or set()
        measured = [spec for spec in specs if spec.slug not in excluded]
        verification["measured"] = len(measured)
        verification["verified_problems"] = len(measured)
        verification["quarantined"] = len(excluded)
    if not measured:
        print("error: the verified subset is empty; pass --all-active", file=sys.stderr)
        return 2

    result = report(
        measured,
        source={
            "db": str(db_path),
            "table": table,
            "active_filter": active_filter,
            "rows_in_table": len(specs),
            "measured": len(measured),
            "columns": list(columns),
            "elapsed_load_seconds": round(time.monotonic() - started, 2),
        },
        verification=verification,
        threshold=args.threshold,
        min_share=args.min_share,
        top=args.top,
        strip_function_name=not args.keep_function_name,
        largest_clusters=max(args.clusters, 1),
        n_max=args.ngram_max,
    )
    result["source"]["elapsed_seconds"] = round(time.monotonic() - started, 2)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")

    if args.json:
        json.dump(result, sys.stdout, indent=1)
        sys.stdout.write("\n")
    elif not args.quiet:
        print_summary(result, top=args.top, clusters=args.clusters)
        print()
        print(f"report written to {out_path}")

    if args.rewrite_manifest:
        library = Library.measured(measured, strip_function_name=not args.keep_function_name)
        scores = [score(spec, library) for spec in measured]
        shared = shared_phrases_by_problem(measured, ngram_counts(measured, n_max=args.ngram_max), args.min_share)
        manifest = rewrite_manifest(measured, scores, args.rewrite_manifest, shared_phrases=shared)
        manifest_path = Path(args.rewrite_out)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        print(f"rewrite manifest: {len(manifest)} problems -> {manifest_path}")
        if manifest:
            counts: Counter[str] = Counter()
            for item in manifest:
                counts.update(item["failed_components"])
            print("  failed components across the work-list:")
            print(_table(
                [{"component": name, "problems": count} for name, count in counts.most_common()],
                ["component", "problems"],
            ))
            print(f"  lowest score in work-list: {manifest[0]['score']} ({manifest[0]['slug']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
