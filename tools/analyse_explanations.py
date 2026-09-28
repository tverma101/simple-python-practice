#!/usr/bin/env python3
"""Measure how much of the problem library has a generic, templated description.

The brief behind this tool is "explanations must be non-generic", and nothing was
measuring that. So this does not start from a list of phrases someone guessed
might be boilerplate; it derives every threshold from the library itself and
prints the distribution it derived them from, so the numbers can be argued with.

Five measurements, in increasing order of how much they prove:

1. **Length distribution.** Percentiles of chars / words / lines over every active
   problem, plus the *floor*: the longest description that still contains no
   problem-specific sentence. A description has to be longer than that to be
   capable of saying something, which is a fact about the data rather than an
   opinion about prose.

2. **Templating.** Descriptions are normalised (lower-cased, whitespace collapsed,
   title and function name removed) and grouped. Two passes: exact grouping, and
   near-grouping with :class:`difflib.SequenceMatcher` over a shingle-blocked
   candidate set. This is the load-bearing measurement -- it *proves* reuse by
   exhibiting byte-identical text across unrelated slugs, instead of asserting it.

3. **N-gram boilerplate.** Word n-grams (n = 1..8) ranked by document frequency,
   with the real count and percentage, plus the specific phrases the brief named so
   the answer to "how often does 'your code here' actually appear" is a number.

4. **Structural placeholder-ness.** Empty, shorter than the title, title-only, or
   carrying an unrendered template marker. Reported even when the count is zero,
   because "zero" is the finding.

5. **A 0-100 score** assembled from the above, every component tagged ``measured``
   (a count read out of the data) or ``heuristic`` (a weight or a wording test
   chosen by a person). There is no "AI quality" component; nothing here models
   whether text was machine-written.

The headline verdict is *structural*, not score-thresholded:

    generic = templated OR overwritten OR placeholder-defect

``templated`` means the description shares its normalised text, or ≥
``--near-ratio`` of it, with at least one other problem. ``overwritten`` means
≥ ``BOILER_SHARE_MAX`` of its lines are sentences other problems also use. The
score ranks and buckets; it does not decide. A metric that decided would be
unfalsifiable.

A third signal, "not one content word that fewer than ``TOKEN_DF_MAX`` other
problems use", is reported but deliberately *not* part of the verdict. It is a
good measure of not-templated and a bad measure of not-a-problem-statement: a
beginner problem whose whole specification is "sum the numbers in the list" uses
only common words, and calling that generic would be wrong. It feeds the score
instead, where it can be weighed against other evidence.

Finally, the ten worst problems get real rewrites, authored by hand, merged in
from ``tools/explanation_rewrites.json``. Those are the target standard: a repair
agent or a teacher should be able to read one and know what "specific" means.

Usage:
    python tools/analyse_explanations.py --db /tmp/probe.db \\
        --out reports/explanations.json [--verified reports/verification.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# ── Thresholds, and where each one comes from ─────────────────────────
#
# These are the only numbers in this file that are not read out of the data. Each
# carries its justification inline. `--sensitivity` re-runs the classification at
# neighbouring values and prints how much the answer moves, so a wrong threshold
# is visible rather than silent.

#: A normalised line seen in at least this many problems is library boilerplate.
#:
#: Derived: the document-frequency histogram of normalised lines has 7,500 lines at
#: df=1, ~640 at df=2-3, and then a broad shelf from df=5 upward that is entirely
#: made of template text ("match behavior shown in the examples and tests" at
#: 3,268). df=4 is the last point before that shelf. The word-count floor below
#: carries most of the real weight; this number only has to exclude coincidence.
BOILER_DF = 5

#: ...and only if it is at least this many words long. Short repeats ("Output:",
#: "return a list") are more likely coincidence or a section header than a reused
#: sentence, and counting them would let a description pass on a skeleton alone.
BOILER_MIN_WORDS = 4

#: Fraction of normalised text two descriptions must share to count as the same
#: description written twice. 0.90 is tight enough that a genuinely reworded
#: problem still scores separately, and loose enough to survive markdown
#: decoration, quote-style and number-formatting drift.
NEAR_RATIO = 0.90

#: A problem is ``templated`` when it shares its description with at least this
#: many problems in total, i.e. it sits in a cluster of at least
#: ``TEMPLATE_CLUSTER + 1`` problems.
TEMPLATE_CLUSTER = 1

#: Share of a description's lines that may be library boilerplate before the
#: description counts as overwritten rather than merely repetitive. Derived from
#: the elbow of the boilerplate-share distribution: 1,944 problems sit at or above
#: 0.80 and 1,427 of them sit in the 0.80-0.85 band, but only 517 reach 0.85. The
#: population ends there, so 0.85 is where "mostly other people's sentences"
#: stops being a majority feature and becomes the whole description. Varying it is
#: in the sensitivity table.
BOILER_SHARE_MAX = 0.85

#: Distinctive tokens at which the substance component saturates. Derived: the
#: 90th percentile of distinctive-token counts is 4, so full marks at 6 means the
#: top decile of the library has to clear a bar the other 90% cannot.
SUBSTANCE_SATURATION = 6

#: A token is "distinctive" when fewer than this many problems' content lines
#: contain it. Below ~5 is where repetition stops being coincidence.
TOKEN_DF_MAX = 5

#: Shingles used to block the near-duplicate search. Without blocking, comparing
#: 5,132 descriptions is 13.2M SequenceMatcher calls; with it, ~10^5 candidates.
SHINGLE_N = 5

#: A shingle shared by more than this many problems is too common to block on --
#: it would put the whole library in one bucket.
SHINGLE_MAX_DF = 60

#: Minimum shingle Jaccard overlap before an expensive ratio() call. Cheap set
#: arithmetic; SequenceMatcher only runs on pairs that could plausibly match.
JACCARD_FLOOR = 0.55

#: Weights for the score. Chosen, not derived. Read `WEIGHT_RATIONALE`.
WEIGHTS = {
    "substance": 25,
    "specificity": 20,
    "uniqueness": 20,
    "worked_example": 15,
    "length_in_range": 10,
    "placeholder_free": 10,
}

WEIGHT_RATIONALE = (
    "substance/uniqueness/worked_example/placeholder_free/length_in_range are counts "
    "read straight from the text; the weights are editorial. substance and uniqueness "
    "dominate because they are what 'generic' actually means: text that says nothing "
    "specific, and text reused from somewhere else. Specificity is the only component "
    "that inspects wording, and it is the one most likely to be wrong -- see "
    "docs/EXPLANATION_QUALITY.md."
)

#: Section headings. Matched case-insensitively, optionally as a markdown heading,
#: optionally with a trailing colon. Deliberately generous: a heading is structure,
#: not a problem statement, so misclassifying one costs almost nothing.
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?("
    r"examples?|example\s*\d*|input|inputs|output|outputs|rules|constraints?|"
    r"requirements?|notes?|hints?|parameters?|arguments?|scenario|lesson context|"
    r"task|test focus|examples tab|output contract|explanation|"
    r"what you.?ll build|why this matters|how to approach this|what to do|"
    r"key strategy|approach|solution|requirements|return type|signature"
    r")\s*:?\s*\**\s*$",
    re.I,
)

#: An example invocation: `foo(a, b) -> 3`, `` `foo(a)` returns `3` ``, `foo() = 3`.
_EXAMPLE_INVOCATION = re.compile(
    r"^[-*]?\s*`?[\w.]+\s*\([^)]*\)`?\s*(?:->|returns|⇒|=>|=|:)\s*\S", re.I
)

#: A caption for an example, not an example.
_EXAMPLE_CAPTION = re.compile(
    r"^(test\s*(case)?\s*\d*|example\s*\d+|edge\s*cases?.*|note[sd]?.*)\s*:?\s*$", re.I
)

#: A numbered step in a "What to Do" list. Instructions, not a problem statement.
_NUMBERED_STEP = re.compile(r"^\d+\.\s+\S")

#: A row of the generated `Inputs:` block -- `- sub: string input`. Structural, not
#: statistical: whether or not it repeats, a form field filled in by a generator is
#: not a problem statement. It has to be matched structurally because
#: normalise_line strips the parameter names, which leaves these rows too short to
#: clear BOILER_MIN_WORDS and they would otherwise be counted as content.
_FORM_FIELD = re.compile(r"^[-*]?\s*[\w .]+:\s*[\w .()\[\]'-]+$")

#: A row of the generated `Output:` block -- `- Return a boolean.`, `- No parameters.`
#: Capped at six words and refused when a conditional is present, so a real
#: specification ("Return the sum when the list is empty otherwise return 0") is
#: not mistaken for a form field.
_OUTPUT_FIELD = re.compile(
    r"^[-*]?\s*(return|returns|print|prints|output|outputs|no\s+(function\s+)?parameters?)\b"
    r"[^.;:]{0,60}[.;]?$",
    re.I,
)
_CONDITIONAL = re.compile(r"\b(if|when|unless|otherwise|for each|except|in which)\b", re.I)

#: A quoted or repr'd literal used as example data: 'abc', "abc", [1, 2], 42.
_LITERAL = re.compile(r"'([^'\n]{1,40})'|\"([^\"\n]{1,40})\"|\b(\d+(?:\.\d+)?)\b")

#: The generator's `Fact:` line -- a standalone sentence introducing a piece of
#: general knowledge. Measured, not assumed: all 140 of them in this library have
#: *zero* content-word overlap with the title of the problem they sit above, and
#: all 140 are distinct, so they are randomised filler. `u1_capstone_price_formatter`
#: opens with "Fact: Polar regions receive less direct sunlight." The count and the
#: overlap are recomputed and reported as `filler_line_audit` so the rule can be
#: checked rather than believed.
_FACT_LINE = re.compile(r"^Fact\s*:", re.I)

#: Unrendered template markers -- what a generator left behind, as opposed to the
#: semantic filler this library actually has. Deliberately tight. An earlier draft
#: matched `<...>` and `[]` and reported 1,511 "defects", almost all of them
#: ordinary comparisons and empty `Inputs:` blocks: a check that cries wolf is
#: worse than no check, because the real handful get dismissed with it.
_PLACEHOLDER_MARKERS: dict[str, re.Pattern[str]] = {
    "todo": re.compile(r"\b(TODO|FIXME|TBD)\b"),
    "mustache": re.compile(r"\{\{[^}]{0,80}\}\}"),
    "python_format_field": re.compile(r"\{[a-z_][a-z0-9_]{0,40}\}"),
    "angle_placeholder": re.compile(r"<\s*(placeholder|your\w*|function\w*|code|value|name|text|"
                                    r"insert|description|title|input|output)\s*>", re.I),
    "lorem": re.compile(r"\blorem\s+ipsum\b", re.I),
    "n_a": re.compile(r"\bN/?A\b"),
    "xxx": re.compile(r"\bX{3,}\b"),
    "empty_call_placeholder": re.compile(r"\(\s*[.…]{3}\s*\)"),
    "your_code_here": re.compile(r"\byour\s+(code|function|solution|answer)\s+here\b", re.I),
    "insert_here": re.compile(r"\b(insert|add|write|paste)\b[^.\n]{0,24}?\bhere\b", re.I),
    "coming_soon": re.compile(r"\b(coming soon|to be (written|added|determined|completed))\b", re.I),
}

#: The phrases the brief asked about by name. Their real frequencies go in the
#: report whether or not they occur; an absent phrase is an answer.
_NAMED_PHRASES = (
    "your code here",
    "return the result",
    "implement this function",
    "todo",
    "write a function",
    "return the result from the function instead of printing it",
    "match behavior shown in the examples and tests",
    "use the examples to determine the expected return value",
    "implement the logic according to test cases",
    "read the scaffolding in your starter code",
    "follow the scaffolding steps in order",
    "carefully trace through the example",
    "do not hardcode",
    "no function parameters",
    "no parameters",
    "return a string",
    "return a list",
    "return the final value",
    "handle common edge cases",
    "solve the",
)


# ── Input ─────────────────────────────────────────────────────────────


@dataclass
class Problem:
    """One active problem, with only the fields this analysis reads."""

    source_id: int
    slug: str
    title: str
    description: str
    signature: str
    function_name: str
    parameters: list[str]
    tags: list[str]

    @property
    def chars(self) -> int:
        return len(self.description)

    @property
    def words(self) -> int:
        return len(self.description.split())

    @property
    def lines(self) -> int:
        return [ln for ln in self.description.splitlines() if ln.strip()]


_PROBLEM_SQL = """
SELECT id, title, slug, description, function_signature, tags
FROM coding_problems
WHERE is_active = 1
ORDER BY id
"""

_DEF_NAME = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", re.M)
_BARE_NAME = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(", re.M)


def _function_name(signature: str) -> str:
    if not signature:
        return ""
    match = _DEF_NAME.search(signature) or _BARE_NAME.search(signature)
    return match.group(1) if match else ""


def _parameters(signature: str) -> list[str]:
    """Parameter names, without importing ducky.content.

    Deliberately duplicated rather than reused: this tool must run against a bare
    database with no project import path, and a two-line regex is not worth a
    sys.path hack. Cross-checked against ducky.content.parse_parameters.
    """
    if not signature:
        return []
    inner = signature[signature.find("(") + 1 : signature.rfind(")")] if "(" in signature else ""
    names = []
    for part in inner.split(","):
        part = part.split(":")[0].split("=")[0].strip()
        if part and part.isidentifier() and part not in ("self", "cls"):
            names.append(part)
    return names


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def load_problems(db: Path) -> list[Problem]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        problems = []
        for row in conn.execute(_PROBLEM_SQL):
            signature = row["function_signature"] or ""
            problems.append(
                Problem(
                    source_id=row["id"],
                    slug=row["slug"] or f"problem-{row['id']}",
                    title=row["title"] or "",
                    description=row["description"] or "",
                    signature=signature,
                    function_name=_function_name(signature),
                    parameters=_parameters(signature),
                    tags=_json_list(row["tags"]),
                )
            )
        return problems
    finally:
        conn.close()


def load_verified(path: Path) -> tuple[set[int] | None, str]:
    """The set of source_ids the content verifier cleared, or ``None``.

    ``reports/verification.json`` is gitignored -- it is 1.2 MB of machine-generated
    detail -- so this file is legitimately absent on a fresh clone. Its absence
    degrades the report to the whole active library and says so, rather than
    guessing which problems were verified.

    The report lists only the *quarantined* problems, so the verified set is the
    active set minus those. ``totals.shippable`` is used as a cross-check, not as
    the source: if the two disagree, the report is reported as disagreeing rather
    than one of them being quietly preferred.
    """
    if not path.is_file():
        return None, "unavailable: run tools/verify_content.py first"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, f"unreadable: {exc}"
    quarantined = data.get("quarantined")
    if not isinstance(quarantined, list):
        return None, "unusable: no 'quarantined' list in the report"
    bad = {
        row["source_id"] for row in quarantined if isinstance(row, dict) and "source_id" in row
    }
    claimed = data.get("totals", {}).get("shippable")
    return bad, ("ok" if claimed is None else f"ok (quarantined {len(bad)}, shippable claim {claimed})")


# ── Normalisation ─────────────────────────────────────────────────────


def normalise_line(line: str, problem: Problem) -> str:
    """A line reduced to what it says rather than which problem it is about.

    Title and function name are removed because every description in this library
    opens by restating its own title and repeats the function name in each
    example; leaving them in would make two descriptions of different problems
    look different when the only thing distinguishing them is the problem they are
    about.
    """
    text = line.lower()
    if problem.function_name:
        text = re.sub(rf"\b{re.escape(problem.function_name.lower())}\b", " ", text)
    if problem.title:
        text = text.replace(problem.title.lower().rstrip("."), " ")
    for name in problem.parameters:
        if len(name) > 2:
            text = re.sub(rf"\b{re.escape(name.lower())}\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalise_text(problem: Problem) -> str:
    """Whole-description normalised form, for exact and near-duplicate grouping.

    Whitespace is collapsed but structure is kept as-is: markdown decoration is
    real text and a description that is 90% scaffolding *is* 90% scaffolding.
    """
    return " ".join(normalise_lines(problem))


def normalise_lines(problem: Problem) -> list[str]:
    """Per-line normalised form, with the line boundaries kept.

    Needed for n-gram work. Collapsing newlines first turns the two Rules
    sentences into one 16-word run, and every 8-word window across that run then
    has the same document frequency -- fifteen rows in the phrase table for what is
    one sentence. Newlines in this library are sentence boundaries often enough to
    be worth respecting.
    """
    return [
        normalised
        for normalised in (normalise_line(raw.strip(), problem) for raw in problem.description.splitlines())
        if normalised
    ]


# ── Measurement 1: length ─────────────────────────────────────────────


def percentiles(values: list[int]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    out: dict[str, float] = {}
    for pct in (0, 1, 5, 10, 25, 50, 75, 90, 95, 99, 100):
        pos = (len(ordered) - 1) * pct / 100
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        out[f"p{pct}"] = round(ordered[low] + (pos - low) * (ordered[high] - ordered[low]), 1)
    out["mean"] = round(sum(ordered) / len(ordered), 1)
    out["min"] = ordered[0]
    out["max"] = ordered[-1]
    return out


# ── Measurement 2: templating ─────────────────────────────────────────


class _Union:
    """Union-find, so a near-duplicate graph becomes clusters rather than pairs."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _shingles(text: str) -> frozenset[tuple[str, ...]]:
    words = text.split()
    if len(words) < SHINGLE_N:
        return frozenset({tuple(words)}) if words else frozenset()
    return frozenset(tuple(words[i : i + SHINGLE_N]) for i in range(len(words) - SHINGLE_N + 1))


def cluster_exact(problems: list[Problem], texts: list[str]) -> list[list[int]]:
    """Groups of problems whose normalised descriptions are byte-identical."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, text in enumerate(texts):
        groups[text].append(i)
    return [members for members in groups.values() if len(members) > 1]


def cluster_near(
    problems: list[Problem], texts: list[str], ratio: float, jaccard_floor: float
) -> tuple[list[list[int]], list[tuple[float, str, str]]]:
    """Problems whose normalised descriptions are near-identical.

    Blocking: shared word-5-gram shingles, ignoring shingles so common they cannot
    discriminate. Candidates are pre-filtered on shingle Jaccard (set arithmetic)
    and only then handed to SequenceMatcher, which is ~100x slower per pair. The
    Jaccard floor is a soundness/latency trade: two texts at ratio >= 0.90 with
    similar lengths always exceed 0.55 shingle overlap, and the sensitivity table
    reports what moving it does.
    """
    shingles = [_shingles(t) for t in texts]
    df: Counter[tuple[str, ...]] = Counter()
    for shingle in shingles:
        df.update(shingle)

    inverted: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i, shingle in enumerate(shingles):
        for gram in shingle:
            if df[gram] <= SHINGLE_MAX_DF:
                inverted[gram].append(i)

    union = _Union(len(problems))
    pairs: dict[tuple[int, int], float] = {}
    seen: set[tuple[int, int]] = set()
    for members in inverted.values():
        if len(members) > SHINGLE_MAX_DF:
            continue
        for a, b in itertools.combinations(members, 2):
            if (a, b) in seen:
                continue
            seen.add((a, b))
            left, right = shingles[a], shingles[b]
            if not left or not right:
                continue
            overlap = len(left & right) / len(left | right)
            if overlap < jaccard_floor:
                continue
            matcher = SequenceMatcher(None, texts[a], texts[b], autojunk=False)
            if matcher.real_quick_ratio() < ratio or matcher.quick_ratio() < ratio:
                continue
            score = matcher.ratio()
            if score >= ratio:
                pairs[(a, b)] = score
                union.union(a, b)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(problems)):
        root = union.find(i)
        if root != i:
            clusters[root].append(i)
    for members in clusters.values():
        members.sort()

    ranked = sorted(
        ((score, problems[a].slug, problems[b].slug) for (a, b), score in pairs.items()),
        reverse=True,
    )
    return [members for members in clusters.values() if len(members) > 1], ranked


# ── Measurement 3: n-gram boilerplate ─────────────────────────────────


def ngram_tables(
    line_groups: list[list[str]], max_n: int = 8, min_n: int = 3, limit: int = 40
) -> dict[str, list[dict]]:
    """Repeated phrases, ranked by how many problems contain them.

    `line_groups` is one list of normalised lines per problem (see
    :func:`normalise_lines`); n-grams never straddle a line boundary.

    Document frequency, not raw count: a phrase repeated three times inside one
    description is not library-wide boilerplate, and counting it that way would
    overstate single-problem noise.

    Only *maximal* phrases are reported, and only phrases (n >= `min_n`). Two
    filters, for two reasons. Maximal: "match behavior shown in the examples and
    tests" and its prefix "match behavior shown in the examples" have identical
    document frequency, and listing both tells the reader the same thing twice
    while pushing real phrases off the end of the table. Contiguous-substring: two
    adjacent repeated lines produce several overlapping windows at the same
    frequency where there is one fact to report. Minimum length: at n=1 the table
    is just "the", "return" and "in", which is true and useless. Word frequencies
    are reported separately.
    """
    total = len(line_groups)
    df: Counter[tuple[str, ...]] = Counter()
    word_df: Counter[str] = Counter()
    for lines in line_groups:
        grams: set[tuple[str, ...]] = set()
        words_here: set[str] = set()
        for line in lines:
            words = line.split()
            words_here.update(words)
            for n in range(1, max_n + 1):
                for i in range(len(words) - n + 1):
                    grams.add(tuple(words[i : i + n]))
        word_df.update(words_here)
        df.update(grams)

    # Index by (length, all-but-last) and (length, all-but-first) so the extension
    # test is a dict lookup per candidate word rather than a scan of every gram.
    by_prefix: dict[tuple[int, tuple[str, ...]], set[str]] = defaultdict(set)
    by_suffix: dict[tuple[int, tuple[str, ...]], set[str]] = defaultdict(set)
    for gram in df:
        if len(gram) < max_n:
            by_prefix[(len(gram), gram[:-1])].add(gram[-1])
            by_suffix[(len(gram), gram[1:])].add(gram[0])

    def has_freer_extension(gram: tuple[str, ...]) -> bool:
        count = df[gram]
        n = len(gram)
        right = any(
            df.get(gram + (word,), 0) >= count for word in by_prefix.get((n + 1, gram[:-1]), ())
        )
        if right:
            return True
        return any(
            df.get((word,) + gram, 0) >= count for word in by_suffix.get((n + 1, gram[1:]), ())
        )

    def redundant(gram: tuple[str, ...], kept_gram: tuple[str, ...]) -> bool:
        """True when `gram` says the same thing as an already-kept gram.

        Contiguous containment catches prefixes and windows. Overlap catches the
        case maximality cannot: "- return the result from the function instead of"
        and "result from the function instead of printing it" are different windows
        of one sentence, so neither contains the other, but they are one fact and
        only one of them belongs in the table. The overlap test is on word
        multisets, so word order does not save a re-shuffle of the same phrase.
        """
        width = len(gram)
        if width <= len(kept_gram) and any(
            kept_gram[i : i + width] == gram for i in range(len(kept_gram) - width + 1)
        ):
            return True
        mine, theirs = Counter(gram), Counter(kept_gram)
        overlap = sum((mine & theirs).values())
        return overlap / len(mine | theirs) >= 0.6

    candidates = [
        gram
        for gram, count in df.items()
        if count > 1 and min_n <= len(gram) <= max_n and not has_freer_extension(gram)
    ]
    # Highest frequency first, longest first within a frequency, so the set that
    # later grams are tested against is the set that matters. Bounded: a redundancy
    # check costs O(kept) and there are tens of thousands of candidates.
    candidates.sort(key=lambda g: (-df[g], -len(g)))
    kept: list[tuple[str, ...]] = []
    kept_counts: list[int] = []
    phrases: list[dict] = []
    for gram in candidates:
        if any(
            redundant(gram, other) for other, count in zip(kept, kept_counts, strict=True)
            if count == df[gram]
        ):
            continue
        kept.append(gram)
        kept_counts.append(df[gram])
        phrases.append(
            {
                "ngram": " ".join(gram),
                "n": len(gram),
                "problems": df[gram],
                "percent": round(100 * df[gram] / total, 1),
            }
        )
        if len(kept) >= limit:
            break

    words = [
        {"word": word, "problems": count, "percent": round(100 * count / total, 1)}
        for word, count in word_df.most_common(limit)
    ]
    return {"phrases": phrases, "words": words}


def named_phrase_counts(texts: list[str]) -> list[dict]:
    """Real frequencies for the phrases the brief named, present or not."""
    out = []
    for phrase in _NAMED_PHRASES:
        count = sum(1 for text in texts if phrase in text)
        out.append(
            {
                "phrase": phrase,
                "present": count > 0,
                "problems": count,
                "percent": round(100 * count / len(texts), 1),
            }
        )
    out.sort(key=lambda row: -row["problems"])
    return out


# ── Measurement 4 + scoring ───────────────────────────────────────────


@dataclass
class LineFacts:
    """Per-description line classification, computed once and reused everywhere."""

    total: int = 0
    content: list[str] = field(default_factory=list)
    boilerplate: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    literals: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)


class BoilerplateIndex:
    """Which normalised lines are library-wide template text.

    A line qualifies on two conditions at once, and both are needed: it must be
    long enough that a repeat cannot be coincidence, and it must appear in enough
    problems that the repeat is systematic. Filtering on either alone is wrong --
    document frequency alone marks "Output:" as boilerplate, which is a heading;
    length alone marks nothing.

    Also holds the document frequency of every individual content word, which is
    what the substance measure is built from.
    """

    def __init__(
        self, counts: Counter[str], token_df: Counter[str], threshold: int, min_words: int
    ) -> None:
        self.counts = counts
        self.token_df = token_df
        self.threshold = threshold
        self.min_words = min_words

    def is_boilerplate(self, normalised: str) -> bool:
        return (
            len(normalised.split()) >= self.min_words
            and self.counts.get(normalised, 0) >= self.threshold
        )

    def is_distinctive(self, token: str) -> bool:
        return self.token_df.get(token, 0) < TOKEN_DF_MAX

    def histogram(self) -> dict[int, int]:
        return dict(sorted(Counter(self.counts.values()).items()))


def build_boilerplate(problems: list[Problem], threshold: int) -> BoilerplateIndex:
    counts: Counter[str] = Counter()
    token_df: Counter[str] = Counter()
    for problem in problems:
        seen_lines: set[str] = set()
        seen_tokens: set[str] = set()
        for raw in problem.description.splitlines():
            line = raw.strip()
            if not line:
                continue
            normalised = normalise_line(line, problem)
            if len(normalised.split()) >= BOILER_MIN_WORDS:
                seen_lines.add(normalised)
            if not is_filler_line(line):
                seen_tokens.update(_TOKEN.findall(line.lower()))
        counts.update(seen_lines)
        token_df.update(seen_tokens)
    return BoilerplateIndex(
        counts=counts, token_df=token_df, threshold=threshold, min_words=BOILER_MIN_WORDS
    )


_TOKEN = re.compile(r"[a-z][a-z']{2,}")


def is_filler_line(line: str) -> bool:
    """Lines that carry no information about the problem they sit in."""
    return bool(_FACT_LINE.match(line))


def audit_filler_lines(problems: list[Problem]) -> dict:
    """Re-derive the evidence for the `Fact:` rule instead of asserting it.

    A classification rule that is quietly wrong is worse than no rule, so the
    number of `Fact:` lines, how many are distinct, and how many share a single
    content word with their own problem's title all go into the report.
    """
    lines: list[str] = []
    zero_overlap = 0
    for problem in problems:
        title_words = {w for w in _TOKEN.findall(problem.title.lower())}
        for raw in problem.description.splitlines():
            line = raw.strip()
            if not is_filler_line(line):
                continue
            lines.append(line)
            if not (set(_TOKEN.findall(line.lower())) & title_words):
                zero_overlap += 1
    return {
        "filler_line_audit": {
            "pattern": "^Fact:",
            "lines": len(lines),
            "distinct_lines": len(set(lines)),
            "sharing_a_content_word_with_their_own_title": len(lines) - zero_overlap,
            "conclusion": "each of these is a distinct sentence about a subject "
            "unrelated to the problem it introduces, so it is randomised filler",
            "example": lines[0] if lines else "",
        }
    }


def classify_lines(problem: Problem, boiler: BoilerplateIndex) -> LineFacts:
    facts = LineFacts()
    title = problem.title.strip().lower().rstrip(".").strip()
    in_fence = False
    for raw in problem.description.splitlines():
        line = raw.strip()
        if not line:
            continue
        facts.total += 1
        if line.startswith("```"):
            in_fence = not in_fence
            facts.examples.append(line)
            continue
        if in_fence or line.startswith(("Input:", "Output:")):
            facts.examples.append(line)
            continue
        facts.literals.extend(
            next(group for group in match.groups() if group is not None)
            for match in _LITERAL.finditer(line)
        )
        if boiler.is_boilerplate(normalise_line(line, problem)) or is_filler_line(line):
            facts.boilerplate.append(line)
            continue
        if title and line.lower().rstrip(".").strip() == title:
            facts.boilerplate.append(line)
            continue
        if _HEADING.match(line) or _EXAMPLE_CAPTION.match(line) or _NUMBERED_STEP.match(line):
            facts.boilerplate.append(line)
            continue
        if _is_form_field(line):
            facts.boilerplate.append(line)
            continue
        if _EXAMPLE_INVOCATION.match(line):
            facts.examples.append(line)
            continue
        facts.content.append(line)
        facts.tokens.update(_TOKEN.findall(line.lower()))
    return facts


def _is_form_field(line: str) -> bool:
    """True for a generated Inputs/Outputs row rather than a written specification."""
    if _FORM_FIELD.match(line):
        return True
    if _OUTPUT_FIELD.match(line) and not _CONDITIONAL.search(line):
        return len(line.split()) <= 6
    return False


def placeholder_defects(problem: Problem) -> list[str]:
    """Structural placeholder markers. Reported even when the count is zero."""
    defects: list[str] = []
    text = problem.description
    stripped = text.strip()
    if not stripped:
        defects.append("empty")
    for name, pattern in _PLACEHOLDER_MARKERS.items():
        if pattern.search(text):
            defects.append(f"marker:{name}")
    if stripped and len(stripped) < len(problem.title.strip()):
        defects.append("shorter_than_title")
    if stripped:
        residue = re.sub(re.escape(problem.title), " ", stripped, flags=re.I)
        residue = re.sub(r"[^a-z0-9 ]+", " ", residue.lower())
        if len(residue.split()) <= 2:
            defects.append("title_only")
    return defects


#: Wording signals for the one component that inspects phrasing. Each is a
#: measured, checkable predicate. The *weights* below are the heuristic.
_EDGE_CASE_WORDS = re.compile(
    r"\b(empty|blank|negative|zero|duplicate|duplicates|sort|sorted|ascending|descending|"
    r"tie|tiebreaker|case[- ]insensitive|whitespace|unicode|none|null|overflow|integer division|"
    r"otherwise|if not|edge case|raises?|invalid|out of range|single|all elements|"
    r"longest|shortest|largest|smallest|first|last|only|must|should not|do not|"
    r"assume|at least|at most|up to|between|leaves?|preserve|maintain|keep)\b",
    re.I,
)


@dataclass
class Score:
    total: int
    components: dict[str, dict]

    def as_json(self) -> dict:
        return {
            "score": self.total,
            "components": [
                {
                    "name": name,
                    "points": detail["points"],
                    "max": detail["max"],
                    "kind": detail["kind"],
                    "detail": detail["detail"],
                }
                for name, detail in self.components.items()
            ],
        }


def score_problem(
    problem: Problem,
    facts: LineFacts,
    defects: list[str],
    cluster_size: int,
    shared_literal_ratio: float,
    length_floor: int,
    length_band: tuple[int, int],
    distinctive: int,
) -> Score:
    """Assemble the 0-100 score from the six declared components.

    `substance`, `uniqueness`, `worked_example`, `placeholder_free` and
    `length_in_range` are counts read out of the description. `specificity` is
    measured line by line but weighted by hand, and is the component most likely to
    misfire -- a description can name every parameter and still say nothing.
    """
    components: dict[str, dict] = {}

    # -- substance (measured): content words that almost no other problem uses.
    #
    # Not a line count. An earlier version counted problem-specific *lines*, which
    # reported 1,674 descriptions as saying nothing when 4,532 of them contain no
    # content word of their own: templates differ from one another by a single
    # randomised word, so one surviving line reads as substance when it is a dice
    # roll. Counting the words removes that escape.
    substance = min(distinctive, SUBSTANCE_SATURATION) / SUBSTANCE_SATURATION
    components["substance"] = {
        "points": round(substance * WEIGHTS["substance"], 1),
        "max": WEIGHTS["substance"],
        "kind": "measured",
        "detail": f"{distinctive} word(s) used by fewer than {TOKEN_DF_MAX} other problems",
    }

    # -- specificity (heuristic): five checkable signals, hand-weighted.
    text = problem.description
    signals: dict[str, bool] = {
        "names_function": bool(problem.function_name)
        and problem.function_name.lower() in text.lower(),
        "names_parameters": bool(problem.parameters)
        and all(p.lower() in text.lower() for p in problem.parameters),
        "concrete_literals": len(set(facts.literals)) >= 3,
        "states_edge_case": bool(_EDGE_CASE_WORDS.search(text)),
        "states_a_rule": bool(
            re.search(r"\b(must|should|return|returns|ignore|skip|keep|preserve|assume|"
                      r"do not|never|always|only|each|every)\b", text, re.I)
        ),
    }
    spec_weights = {
        "names_function": 0.15,
        "names_parameters": 0.25,
        "concrete_literals": 0.2,
        "states_edge_case": 0.25,
        "states_a_rule": 0.15,
    }
    spec_raw = sum(w for k, w in spec_weights.items() if signals[k])
    components["specificity"] = {
        "points": round(spec_raw * WEIGHTS["specificity"], 1),
        "max": WEIGHTS["specificity"],
        "kind": "heuristic",
        "detail": ", ".join(k for k, v in signals.items() if v) or "no specificity signal",
    }

    # -- uniqueness (measured): 1 - share of lines shared with the rest of the library.
    if facts.total:
        share = len(facts.boilerplate) / facts.total
    else:
        share = 1.0
    unique = max(0.0, 1.0 - share)
    if cluster_size > TEMPLATE_CLUSTER:
        # Reuse is not a matter of degree for scoring purposes: text shared with
        # another problem is not this problem's explanation, whatever the ratio.
        unique = 0.0
    components["uniqueness"] = {
        "points": round(unique * WEIGHTS["uniqueness"], 1),
        "max": WEIGHTS["uniqueness"],
        "kind": "measured",
        "detail": (
            f"{len(facts.boilerplate)}/{facts.total} lines are library boilerplate"
            + (f"; description is shared with {cluster_size - 1} other problem(s)" if cluster_size > TEMPLATE_CLUSTER else "")
        ),
    }

    # -- worked_example (measured): real invocations, discounted when the literals
    #    come from a pool shared across the library.
    n_examples = len(facts.examples)
    example_points = 0.0
    if n_examples:
        example_points = min(n_examples / 3, 1.0)
    if shared_literal_ratio >= 0.99 and n_examples:
        # Every literal in this description also appears in other problems' examples.
        # That is what a generated example block looks like, and it is worse than
        # no example at all: it looks worked and is not.
        example_points = 0.0
    components["worked_example"] = {
        "points": round(example_points * WEIGHTS["worked_example"], 1),
        "max": WEIGHTS["worked_example"],
        "kind": "measured",
        "detail": (
            f"{n_examples} example line(s)"
            + (f"; {round(shared_literal_ratio * 100)}% of literals shared with other problems"
               if shared_literal_ratio >= 0.99 and n_examples else "")
        ),
    }

    # -- length_in_range (measured): below the floor scores nothing; inside the band
    #    full marks; above it tapers, because 1,900 characters of template is not
    #    better than 300 characters of template.
    n_chars = problem.chars
    if n_chars < length_floor:
        length_points = 0.0
    else:
        lo, hi = length_band
        if n_chars <= hi:
            length_points = 1.0
        else:
            length_points = max(0.0, 1.0 - (n_chars - hi) / (hi * 2))
    components["length_in_range"] = {
        "points": round(length_points * WEIGHTS["length_in_range"], 1),
        "max": WEIGHTS["length_in_range"],
        "kind": "measured",
        "detail": f"{n_chars} chars (floor {length_floor}, band <= {length_band[1]})",
    }

    # -- placeholder_free (measured): full marks minus a flat penalty per defect.
    penalty = 5.0 * len(defects)
    components["placeholder_free"] = {
        "points": round(max(0.0, WEIGHTS["placeholder_free"] - penalty), 1),
        "max": WEIGHTS["placeholder_free"],
        "kind": "measured",
        "detail": ", ".join(defects) if defects else "no placeholder marker",
    }

    total = round(sum(c["points"] for c in components.values()), 1)
    return Score(total=int(round(total)), components=components)


# ── Report assembly ───────────────────────────────────────────────────


def build_rows(
    problems: list[Problem],
    facts: list[LineFacts],
    cluster_of: dict[int, list[int]],
    boiler: BoilerplateIndex,
) -> dict:
    """Per-problem measurements and score, from the parts already computed.

    Split out of :func:`analyse` because it depends only on the cheap inputs. The
    sensitivity sweep re-varies the boilerplate threshold and the similarity ratio,
    and would otherwise pay the 90-second near-duplicate search eight times over.
    """
    # Shared-literal pool: literals appearing in the examples of many problems are
    # drawn from a generator's fixed vocabulary, not from the problem being solved.
    literal_df: Counter[str] = Counter()
    for problem_facts in facts:
        literal_df.update(set(problem_facts.literals))

    def shared_ratio(problem_facts: LineFacts) -> float:
        unique = set(problem_facts.literals)
        if not unique:
            return 0.0
        return sum(1 for lit in unique if literal_df[lit] >= 5) / len(unique)

    # Derived length floor: the longest description that still says nothing
    # specific. Not a chosen number -- it is the top of the set with no content
    # word of its own, so below it a description cannot be specific.
    silent_lengths = [
        p.chars
        for p, f in zip(problems, facts, strict=True)
        if not (set(f.tokens) & {t for t in f.tokens if boiler.is_distinctive(t)})
    ]
    content_bearing = [
        p.chars
        for p, f in zip(problems, facts, strict=True)
        if any(boiler.is_distinctive(t) for t in f.tokens)
    ]
    length_floor = max(silent_lengths) if silent_lengths else 0
    length_band = (
        int(percentiles(content_bearing).get("p25", 0)),
        int(percentiles(content_bearing).get("p90", 0)),
    )

    rows = []
    for i, problem in enumerate(problems):
        members = cluster_of.get(i, [i])
        defects = placeholder_defects(problem)
        ratio = shared_ratio(facts[i])
        distinctive = sum(1 for token in facts[i].tokens if boiler.is_distinctive(token))
        share = len(facts[i].boilerplate) / facts[i].total if facts[i].total else 1.0
        row_score = score_problem(
            problem,
            facts[i],
            defects,
            len(members),
            ratio,
            length_floor,
            length_band,
            distinctive,
        )
        templated = len(members) > TEMPLATE_CLUSTER
        overwritten = share >= BOILER_SHARE_MAX
        rows.append(
            {
                "source_id": problem.source_id,
                "slug": problem.slug,
                "title": problem.title,
                "function_signature": problem.signature,
                "chars": problem.chars,
                "words": problem.words,
                "lines": facts[i].total,
                "content_lines": len(facts[i].content),
                "boilerplate_lines": len(facts[i].boilerplate),
                "boilerplate_share": round(share, 3),
                "distinctive_words": distinctive,
                "no_distinctive_words": distinctive == 0,
                "shared_literal_ratio": round(ratio, 3),
                "duplicate_cluster": sorted(problems[m].slug for m in members) if templated else [],
                "duplicate_cluster_size": len(members),
                "placeholder_defects": defects,
                "templated": templated,
                "overwritten": overwritten,
                "generic": templated or overwritten or bool(defects),
                "score": row_score.total,
                "score_components": row_score.as_json()["components"],
                "content_sample": facts[i].content[:3],
            }
        )

    return {
        "rows": rows,
        "length_floor": length_floor,
        "length_band": length_band,
        "silent_lengths": silent_lengths,
        "content_bearing_lengths": content_bearing,
        "literal_df": literal_df,
    }


def scaffold_texts(problems: list[Problem], facts: list[LineFacts]) -> list[str]:
    """The boilerplate lines only, normalised, joined -- i.e. the scaffolding.

    Whole-description similarity misses the most common form of reuse: a long
    shared preamble wrapped around examples that genuinely differ.
    `bfs-5-pacific-atlantic-water-flow-leetcode-417` carries a 300-character
    `Scenario:` paragraph that every other hard graph problem also carries, word
    for word, and then three real worked examples. The two descriptions are only
    about 60% similar, so it sits in no cluster -- while the scaffolding is
    identical. Comparing scaffolding to scaffolding separates "same description
    twice" from "same description plus different examples", and the second is the
    more common of the two in this library.
    """
    return [
        " ".join(normalise_line(line, problem) for line in problem_facts.boilerplate)
        for problem, problem_facts in zip(problems, facts, strict=True)
    ]


def analyse(
    problems: list[Problem],
    *,
    boiler_threshold: int,
    near_ratio: float,
    jaccard_floor: float,
) -> dict:
    boiler = build_boilerplate(problems, boiler_threshold)
    texts = [normalise_text(p) for p in problems]
    facts = [classify_lines(p, boiler) for p in problems]

    exact_clusters = cluster_exact(problems, texts)
    near_clusters, near_pairs = cluster_near(problems, texts, near_ratio, jaccard_floor)
    scaffolds = scaffold_texts(problems, facts)
    scaffold_clusters, _ = cluster_near(problems, scaffolds, near_ratio, jaccard_floor)

    # A problem counts as templated if *any* of the three passes groups it with
    # another problem. Whole-text grouping is the strongest claim; scaffolding
    # grouping is the weakest but catches the most.
    cluster_of: dict[int, list[int]] = {}
    for members in (*exact_clusters, *near_clusters, *scaffold_clusters):
        cluster_of.update({i: members for i in members})

    result = {
        "problems": problems,
        "texts": texts,
        "scaffolds": scaffolds,
        "line_groups": [normalise_lines(p) for p in problems],
        "facts": facts,
        "boiler_index": boiler,
        "exact_clusters": exact_clusters,
        "near_clusters": near_clusters,
        "scaffold_clusters": scaffold_clusters,
        "near_pairs": near_pairs,
        "cluster_of": cluster_of,
    }
    result.update(build_rows(problems, facts, cluster_of, boiler))
    return result


def summarise(result: dict, boiler_threshold: int, near_ratio: float) -> dict:
    problems: list[Problem] = result["problems"]
    n = len(problems)

    largest_exact = sorted(result["exact_clusters"], key=len, reverse=True)
    largest_near = sorted(result["near_clusters"], key=len, reverse=True)
    largest_scaffold = sorted(result["scaffold_clusters"], key=len, reverse=True)

    def cluster_report(members: list[int]) -> dict:
        head = problems[members[0]]
        return {
            "size": len(members),
            "example_slugs": [problems[m].slug for m in members[:5]],
            "example_titles": [problems[m].title for m in members[:3]],
            "normalised_text": normalise_text(head)[:400],
            "members": [problems[m].slug for m in members],
        }

    exact_dupes = sum(len(m) for m in largest_exact)
    near_dupes = sum(len(m) for m in largest_near)

    # Read the boilerplate line texts back off the problems. Counter keys are the
    # normalised form, which is unreadable in a report; the original line is what a
    # human needs to see. One line per normalised form, so a repeated sentence is
    # listed once with its true problem count.
    boiler_lines: Counter[str] = Counter()
    representative: dict[str, str] = {}
    for problem, problem_facts in zip(problems, result["facts"], strict=True):
        for line in problem_facts.boilerplate:
            key = normalise_line(line, problem)
            if key:
                boiler_lines[key] += 1
                representative.setdefault(key, line)
    top_boiler = [
        {"line": representative[key], "normalised": key, "problems": count}
        for key, count in sorted(boiler_lines.items(), key=lambda pair: -pair[1])[:25]
    ]

    rows_all_shared = sum(1 for r in result["rows"] if r["shared_literal_ratio"] >= 0.99)

    return {
        "thresholds": {
            "boilerplate_df": boiler_threshold,
            "boilerplate_min_words": BOILER_MIN_WORDS,
            "near_ratio": near_ratio,
            "template_cluster": TEMPLATE_CLUSTER,
            "boilerplate_share_max": BOILER_SHARE_MAX,
            "token_df_max": TOKEN_DF_MAX,
            "note": "boilerplate_df and near_ratio are chosen, not derived; see "
            "sensitivity below and WEIGHT_RATIONALE for the rest",
        },
        "library": {"active_problems": n},
        "length": {
            "chars": percentiles([p.chars for p in problems]),
            "words": percentiles([p.words for p in problems]),
            "lines": percentiles([p.lines.__len__() for p in problems]),
            "derived_floor_chars": result["length_floor"],
            "floor_meaning": "longest description in the library that contains no "
            "problem-specific line; below it a description cannot be specific",
            "content_bearing_chars": percentiles(result["content_bearing_lengths"]),
            "no_distinctive_word_chars": percentiles(result["silent_lengths"]),
            "no_distinctive_word_count": len(result["silent_lengths"]),
        },
        "templating": {
            "distinct_normalised": len(set(result["texts"])),
            "exact_duplicate_problems": exact_dupes,
            "exact_duplicate_clusters": len(largest_exact),
            "near_duplicate_problems": near_dupes,
            "near_duplicate_clusters": len(largest_near),
            "scaffold_duplicate_problems": sum(len(m) for m in largest_scaffold),
            "scaffold_duplicate_clusters": len(largest_scaffold),
            "templated_total": len(result["cluster_of"]),
            "unique_descriptions": n - len(result["cluster_of"]),
            "passes": {
                "exact": "whole normalised description byte-identical to another problem's",
                "near": f"whole normalised description >= {near_ratio} similar to another "
                "problem's",
                "scaffold": "the boilerplate lines alone are >= "
                f"{near_ratio} similar to another problem's -- catches the shared "
                "preamble wrapped around genuinely different examples, which the "
                "whole-text pass cannot see",
                "union": "a problem counts as templated if any pass groups it with "
                "another",
            },
            "largest_exact_clusters": [cluster_report(m) for m in largest_exact[:8]],
            "largest_near_clusters": [cluster_report(m) for m in largest_near[:8]],
            "largest_scaffold_clusters": [
                {**cluster_report(m), "scaffold": result["scaffolds"][m[0]][:300]}
                for m in largest_scaffold[:8]
            ],
            "top_near_pairs": [
                {"ratio": round(r, 4), "a": a, "b": b} for r, a, b in result["near_pairs"][:15]
            ],
        },
        "boilerplate_lines": {
            "criterion": f"a normalised line of >= {BOILER_MIN_WORDS} words occurring in "
            f">= {boiler_threshold} problems",
            "distinct_normalised_lines": len(result["boiler_index"].counts),
            "document_frequency_histogram": {
                str(df): n for df, n in result["boiler_index"].histogram().items()
            },
            "top_lines": top_boiler,
        },
        **audit_filler_lines(problems),
        "ngrams": ngram_tables(result["line_groups"]),
        "named_phrases": named_phrase_counts(result["texts"]),
        "shared_example_literals": {
            "criterion": "a literal appearing in >= 5 problems' example lines, "
            "excluding single digits (too common to be evidence)",
            "top": [
                {"literal": lit, "problems": count}
                for lit, count in sorted(result["literal_df"].items(), key=lambda p: -p[1])
                if count >= 5 and len(lit) > 1
            ][:25],
            "problems_whose_every_example_literal_is_shared": rows_all_shared,
            "why_it_matters": "these are drawn from a generator's fixed example "
            "vocabulary, so a description built from them looks worked and is not",
        },
    }


# ── Output ────────────────────────────────────────────────────────────


def _table(rows: list[dict], width_title: int = 46) -> str:
    out = [
        f"{'score':>5}  {'verdict':<8} {'chars':>5} {'own':>4} {'boil':>4} {'clus':>5}  "
        f"{'slug':<{width_title}}  title",
        "-" * (width_title + 50),
    ]
    for row in rows:
        verdict = "GENERIC" if row["generic"] else "specific"
        out.append(
            f"{row['score']:>5}  {verdict:<8} {row['chars']:>5} {row['distinctive_words']:>4} "
            f"{row['boilerplate_lines']:>4} {row['duplicate_cluster_size']:>5}  "
            f"{row['slug'][:width_title]:<{width_title}}  {row['title'][:38]}"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/tmp/probe.db", help="path to the content database (read-only)")
    parser.add_argument("--verified", default="reports/verification.json")
    parser.add_argument("--out", default="reports/explanations.json")
    parser.add_argument(
        "--rewrites",
        default=str(Path(__file__).resolve().parent / "explanation_rewrites.json"),
        help="hand-authored rewrites merged into the report",
    )
    parser.add_argument("--boilerplate-df", type=int, default=BOILER_DF)
    parser.add_argument("--near-ratio", type=float, default=NEAR_RATIO)
    parser.add_argument("--jaccard-floor", type=float, default=JACCARD_FLOOR)
    parser.add_argument("--sensitivity", action="store_true", help="re-classify at nearby thresholds")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"error: no such database: {db}", file=sys.stderr)
        return 2

    started = time.monotonic()
    problems = load_problems(db)
    if not problems:
        print(f"error: {db} has no active problems", file=sys.stderr)
        return 2

    if not args.quiet:
        print(f"loaded {len(problems)} active problems in {time.monotonic() - started:.1f}s")

    result = analyse(
        problems,
        boiler_threshold=args.boilerplate_df,
        near_ratio=args.near_ratio,
        jaccard_floor=args.jaccard_floor,
    )
    rows: list[dict] = result["rows"]
    summary = summarise(result, args.boilerplate_df, args.near_ratio)
    args_boiler_df = args.boilerplate_df

    # -- verified subset -------------------------------------------------
    quarantined, verified_status = load_verified(Path(args.verified))
    verified_ids: set[int] | None = None
    if quarantined is not None:
        verified_ids = {p.source_id for p in problems} - quarantined
        for row in rows:
            row["verified"] = row["source_id"] in verified_ids
    else:
        for row in rows:
            row["verified"] = None
    summary["verification_subset"] = {
        "source": str(args.verified),
        "status": verified_status,
        "verified_problems": len(verified_ids) if verified_ids is not None else None,
        "active_problems": len(problems),
    }

    generic = [r for r in rows if r["generic"]]
    templated = [r for r in rows if r["templated"]]
    wordless = [r for r in rows if r["no_distinctive_words"]]
    overwritten = [r for r in rows if r["overwritten"]]
    defective = [r for r in rows if r["placeholder_defects"]]
    specific = [r for r in rows if not r["generic"]]
    _generic_reasons = Counter(
        reason
        for row in rows
        for reason, hit in (
            ("templated", row["templated"]),
            ("no_distinctive_words", row["no_distinctive_words"]),
            ("overwritten", row["overwritten"]),
            ("placeholder", bool(row["placeholder_defects"])),
        )
        if hit
    )

    summary["verdict"] = {
        "definition": (
            "generic = any of: templated (the normalised description is shared with "
            ">=1 other problem, or shares >= near_ratio of it) OR overwritten "
            f"(>= {BOILER_SHARE_MAX:.0%} of its lines are library boilerplate) OR a "
            "structural placeholder defect. no_distinctive_words is reported but is "
            "not a reason: it measures not-templated, not not-a-problem-statement"
        ),
        "generic": len(generic),
        "specific": len(specific),
        "generic_percent": round(100 * len(generic) / len(rows), 1),
        "reason_counts_not_mutually_exclusive": dict(_generic_reasons.most_common()),
        "templated": len(templated),
        "overwritten": len(overwritten),
        "no_distinctive_words": len(wordless),
        "templated_only": sum(
            1 for r in rows if r["templated"] and not r["overwritten"]
        ),
        "overwritten_only": sum(
            1 for r in rows if r["overwritten"] and not r["templated"]
        ),
        "placeholder_defects": len(defective),
        "boilerplate_share_histogram": _share_histogram(rows),
        "score_histogram": _score_histogram(rows),
        "score_of_generic_median": _median([r["score"] for r in generic]),
        "score_of_specific_median": _median([r["score"] for r in specific]),
    }
    if verified_ids is not None:
        vrows = [r for r in rows if r["verified"]]
        vgen = [r for r in vrows if r["generic"]]
        summary["verdict"]["verified_only"] = {
            "total": len(vrows),
            "generic": len(vgen),
            "specific": len(vrows) - len(vgen),
            "generic_percent": round(100 * len(vgen) / len(vrows), 1),
            "templated": sum(1 for r in vrows if r["templated"]),
            "overwritten": sum(1 for r in vrows if r["overwritten"]),
            "no_distinctive_words": sum(1 for r in vrows if r["no_distinctive_words"]),
            "score_histogram": _score_histogram(vrows),
            "score_of_specific_median": _median([r["score"] for r in vrows if not r["generic"]]),
            "the_specific_ones": sorted(
                ({"slug": r["slug"], "score": r["score"]} for r in vrows if not r["generic"]),
                key=lambda r: -r["score"],
            ),
        }

    # -- rewrites --------------------------------------------------------
    rewrites = _load_rewrites(Path(args.rewrites), rows, result["problems"])
    summary["examples_of_rewrites"] = rewrites

    if args.sensitivity:
        summary["sensitivity"] = _sensitivity(problems, result, args.near_ratio)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_db": str(db),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "scoring": {
            "weights": WEIGHTS,
            "weight_rationale": WEIGHT_RATIONALE,
            "components": [
                {"name": "substance", "kind": "measured", "max": WEIGHTS["substance"],
                 "what": f"count of content words used by fewer than {TOKEN_DF_MAX} other "
                         f"problems, saturating at {SUBSTANCE_SATURATION}"},
                {"name": "specificity", "kind": "heuristic", "max": WEIGHTS["specificity"],
                 "what": "five checkable wording signals (names the function, names every "
                         "declared parameter, >=3 concrete literals, states an edge case, "
                         "states a rule), hand-weighted"},
                {"name": "uniqueness", "kind": "measured", "max": WEIGHTS["uniqueness"],
                 "what": f"1 - share of lines whose normalised form occurs in >= "
                         f"{args_boiler_df} other problems; zeroed outright if the "
                         "description is shared with another problem"},
                {"name": "worked_example", "kind": "measured", "max": WEIGHTS["worked_example"],
                 "what": "example lines, saturating at 3; zeroed if every literal in the "
                         "description also appears in other problems' examples"},
                {"name": "length_in_range", "kind": "measured", "max": WEIGHTS["length_in_range"],
                 "what": "0 below the derived floor, 1 inside [p25, p90] of the "
                         "content-bearing population, tapering to 0 at 3x p90"},
                {"name": "placeholder_free", "kind": "measured", "max": WEIGHTS["placeholder_free"],
                 "what": "10 minus 5 per structural placeholder defect"},
            ],
            "explicitly_not_measured": "nothing here estimates whether text was "
            "machine-generated or 'AI quality'; there is no such component",
        },
        "summary": summary,
        "problems": sorted(rows, key=lambda r: (r["score"], r["slug"])),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.quiet:
        _print_report(report, result)
    print(f"\nwrote {out}")
    return 0


def _score_histogram(rows: list[dict]) -> dict[str, int]:
    bands = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101))
    return {f"{lo}-{hi - 1}": sum(1 for r in rows if lo <= r["score"] < hi) for lo, hi in bands}


def _share_histogram(rows: list[dict], step: float = 0.05) -> dict[str, int]:
    hist: dict[str, int] = {}
    for row in rows:
        bucket = min(int(row["boilerplate_share"] / step), int(1 / step) - 1)
        key = f"{bucket * step:.2f}-{bucket * step + step:.2f}"
        hist[key] = hist.get(key, 0) + 1
    return hist


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def _load_rewrites(path: Path, rows: list[dict], problems: list[Problem]) -> list[dict]:
    """Merge hand-authored rewrites with the originals read back from the database.

    The original is taken from the database, not from the sidecar, so the report
    cannot claim a rewrite improved a description that has since changed. A slug
    in the sidecar that is not in the library is reported as stale rather than
    silently dropped.
    """
    by_slug = {row["slug"]: row for row in rows}
    by_id = {p.source_id: p for p in problems}
    if not path.is_file():
        return [{"note": f"no rewrite sidecar at {path}"}]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [{"note": f"unreadable rewrite sidecar {path}: {exc}"}]

    entries = data.get("rewrites") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return [{"note": f"rewrite sidecar {path} has no 'rewrites' list"}]

    out = []
    for entry in entries:
        slug = entry.get("slug")
        row = by_slug.get(slug)
        if row is None:
            out.append({"slug": slug, "stale": "slug not found in the active library"})
            continue
        problem = by_id[row["source_id"]]
        out.append(
            {
                "source_id": row["source_id"],
                "slug": slug,
                "title": problem.title,
                "function_signature": problem.signature,
                "score": row["score"],
                "templated": row["templated"],
                "overwritten": row["overwritten"],
                "why_it_was_flagged": (
                    f"{row['content_lines']} problem-specific line(s), "
                    f"{row['boilerplate_lines']}/{row['lines']} boilerplate, "
                    f"cluster of {row['duplicate_cluster_size']}"
                ),
                "original": problem.description,
                "rewrite": entry.get("rewrite", ""),
                "rewrite_notes": entry.get("notes", ""),
            }
        )
    return out


def _sensitivity(problems: list[Problem], result: dict, near_ratio: float) -> dict:
    """How much the verdict moves when the chosen thresholds move.

    Both thresholds are editorial. Rather than defend them, this shows the answer
    at neighbouring values so a reader can see whether the conclusion depends on
    them. The boilerplate-threshold sweep reuses the near-duplicate clusters from
    the main pass, because that measurement does not depend on this threshold --
    recomputing it four more times would cost six minutes and change nothing.
    """
    out: dict[str, dict] = {"boilerplate_df": {}, "near_ratio": {}}
    for threshold in (3, 5, 10, 20):
        boiler = build_boilerplate(problems, threshold)
        facts = [classify_lines(p, boiler) for p in problems]
        rows = build_rows(problems, facts, result["cluster_of"], boiler)["rows"]
        out["boilerplate_df"][str(threshold)] = {
            "no_distinctive_words": sum(1 for r in rows if r["no_distinctive_words"]),
            "generic": sum(1 for r in rows if r["generic"]),
            "mean_score": round(sum(r["score"] for r in rows) / len(rows), 1),
        }
    for ratio in (0.80, 0.85, 0.90, 0.95):
        # All three passes are re-run, so the row at the default ratio reproduces the
        # headline number. Re-running only the two similarity passes under-counts by
        # ~300, because the exact-duplicate pass is not implied by either of them.
        exact = cluster_exact(problems, result["texts"])
        whole, _ = cluster_near(problems, result["texts"], ratio, max(0.2, ratio - 0.4))
        scaffold, _ = cluster_near(
            problems, result["scaffolds"], ratio, max(0.2, ratio - 0.4)
        )
        cluster_of = {i: members for members in (*exact, *whole, *scaffold) for i in members}
        rows = build_rows(problems, result["facts"], cluster_of, result["boiler_index"])["rows"]
        out["near_ratio"][str(ratio)] = {
            "templated": sum(1 for r in rows if r["templated"]),
            "generic": sum(1 for r in rows if r["generic"]),
        }
    return out


def _print_report(report: dict, result: dict) -> None:
    summary = report["summary"]
    length = summary["length"]
    templating = summary["templating"]
    verdict = summary["verdict"]
    n = summary["library"]["active_problems"]

    print(f"\n{'=' * 78}\nLENGTH DISTRIBUTION ({n} active problems)\n{'=' * 78}")
    for key in ("chars", "words", "lines"):
        row = length[key]
        cells = "  ".join(f"{k}={row[k]:g}" for k in ("p0", "p5", "p25", "p50", "p75", "p95", "p100"))
        print(f"  {key:<6} {cells}   mean={row['mean']:g}")
    print(
        f"\n  derived floor: {length['derived_floor_chars']} chars "
        f"({length['floor_meaning']})"
    )
    print(
        f"  no content word of their own: {length['no_distinctive_word_count']}   "
        f"at least one: {n - length['no_distinctive_word_count']}"
    )

    print(f"\n{'=' * 78}\nTEMPLATING\n{'=' * 78}")
    print(f"  distinct normalised descriptions : {templating['distinct_normalised']} of {n}")
    print(f"  pass 1 exact duplicate            : {templating['exact_duplicate_problems']:>5}"
          f"  in {templating['exact_duplicate_clusters']} groups")
    print(f"  pass 2 whole-text >= {summary['thresholds']['near_ratio']} similar   : "
          f"{templating['near_duplicate_problems']:>5}  in {templating['near_duplicate_clusters']} groups")
    print(f"  pass 3 scaffolding only           : "
          f"{templating['scaffold_duplicate_problems']:>5}  in {templating['scaffold_duplicate_clusters']} groups")
    print(f"  union: templated {templating['templated_total']}, "
          f"unique to one problem {templating['unique_descriptions']}")
    print(f"  {templating['passes']['scaffold']}")
    print("\n  largest near-duplicate clusters (whole description):")
    for cluster in templating["largest_near_clusters"][:4]:
        print(f"    {cluster['size']:>5} problems, e.g. {', '.join(cluster['example_slugs'][:3])}")
        print(f"          normalised: {cluster['normalised_text'][:104]!r}")
    print("\n  largest scaffolding clusters (same scaffold, different examples):")
    for cluster in templating["largest_scaffold_clusters"][:4]:
        print(f"    {cluster['size']:>5} problems, e.g. {', '.join(cluster['example_slugs'][:3])}")
        print(f"          scaffold: {cluster.get('scaffold', '')[:104]!r}")

    print(f"\n{'=' * 78}\nBOILERPLATE PHRASES (maximal n-grams, document frequency)\n{'=' * 78}")
    print(f"  {'problems':>8} {'pct':>6}  n  phrase")
    for row in summary["ngrams"]["phrases"][:25]:
        print(f"  {row['problems']:>8} {row['percent']:>5}%  {row['n']}  {row['ngram'][:66]}")

    print("\n  most common single words: "
          + ", ".join(
              f"{w['word']} {w['percent']:g}%" for w in summary["ngrams"]["words"][:10]
          ))

    shared = summary["shared_example_literals"]
    print(
        f"\n  fixed example vocabulary: "
        f"{shared['problems_whose_every_example_literal_is_shared']} problems use only "
        f"literals that also appear in other problems' examples"
    )
    for row in shared["top"][:8]:
        print(f"    {row['problems']:>5} problems  {row['literal']!r}")

    print("\n  phrases named in the brief, with their real frequency:")
    for row in summary["named_phrases"]:
        mark = " " if row["present"] else "?"
        count = f"{row['problems']:>6} {row['percent']:>5}%" if row["present"] else "  absent"
        print(f"   {mark} {count}  {row['phrase']}")

    print(f"\n{'=' * 78}\nVERDICT\n{'=' * 78}")
    print(f"  generic    : {verdict['generic']:>6}  ({verdict['generic_percent']}%)")
    print(f"  specific   : {verdict['specific']:>6}")
    print("  reasons (a problem can have more than one):")
    for reason, count in verdict["reason_counts_not_mutually_exclusive"].items():
        print(f"    {reason:<12} {count:>6}")
    print(f"  templated but not overwritten                : {verdict['templated_only']:>6}")
    print(f"  overwritten but not templated                : {verdict['overwritten_only']:>6}")
    print(f"  score median: generic {verdict['score_of_generic_median']}, "
          f"specific {verdict['score_of_specific_median']}")
    print(f"  score histogram: {verdict['score_histogram']}")
    if "verified_only" in verdict:
        v = verdict["verified_only"]
        print(
            f"\n  of the {v['total']} verified problems: {v['generic']} generic "
            f"({v['generic_percent']}%), {v['specific']} specific"
        )
        print(f"    templated {v['templated']}, overwritten {v['overwritten']}, "
              f"no distinctive words {v['no_distinctive_words']}")
    print("\n  boilerplate-share distribution (the BOILER_SHARE_MAX elbow):")
    for key, count in verdict["boilerplate_share_histogram"].items():
        marker = "  <-- gate" if abs(float(key.split("-")[1]) - BOILER_SHARE_MAX) < 1e-9 else ""
        print(f"    {key}  {count:>5}{marker}")

    rows = report["problems"]
    print(f"\n{'=' * 78}\n20 WORST\n{'=' * 78}")
    print(_table(rows[:20]))
    print(f"\n{'=' * 78}\n10 BEST\n{'=' * 78}")
    print(_table(rows[-10:][::-1]))

    if "sensitivity" in summary:
        sens = summary["sensitivity"]
        print(f"\n{'=' * 78}\nSENSITIVITY (both thresholds are chosen, not derived)\n{'=' * 78}")
        print(f"  {'boilerplate_df':>14}  {'no distinct. words':>18}  {'generic':>8}  {'mean score':>11}")
        for key, row in sens["boilerplate_df"].items():
            mark = "  <- used" if int(key) == summary["thresholds"]["boilerplate_df"] else ""
            print(f"  {key:>14}  {row['no_distinctive_words']:>18}  {row['generic']:>8}  "
                  f"{row['mean_score']:>11}{mark}")
        print(f"\n  {'near_ratio':>14}  {'templated':>18}  {'generic':>8}")
        for key, row in sens["near_ratio"].items():
            mark = "  <- used" if float(key) == summary["thresholds"]["near_ratio"] else ""
            print(f"  {key:>14}  {row['templated']:>18}  {row['generic']:>8}{mark}")

    rewrites = summary.get("examples_of_rewrites") or []
    real = [r for r in rewrites if r.get("rewrite")]
    if real:
        print(f"\n{'=' * 78}\nHAND-AUTHORED REWRITES ({len(real)})\n{'=' * 78}")
        for entry in real:
            print(f"  {entry['score']:>4}  {entry['slug']}  ->  {len(entry['rewrite'])} chars")


if __name__ == "__main__":
    raise SystemExit(main())
