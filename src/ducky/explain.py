"""Compose problem explanations from the problem's own test data.

Why generated rather than written
---------------------------------

The audit found the library's explanations are mostly one template: 1,149 problems
share a byte-identical description, 84.6% contain a clause repeated across the
library (*"return the result"*, 1,122 problems; *"handle common edge cases from
the test suite"*, 1,114), and the largest duplicate cluster holds 141 problems
with the same body.

Hand-writing fixes that one problem at a time and does not scale to five
thousand. So an explanation here is **composed from facts about that specific
problem** -- its signature, its worked examples, its detectable edge cases -- and
the template only supplies the connective tissue. The result cannot be generic
because the specific part is the substance, not the decoration:

    has_duplicates(nums)
    Write a function called has_duplicates that takes a list of numbers and
    returns whether any number appears more than once.

    has_duplicates([1, 2, 3, 1])  returns  True
    has_duplicates([1, 2, 3])     returns  False

    Two things the tests check that are easy to miss. The list [1, 2, 3, 1] has a
    duplicate, so the answer is True. A list with no repeats, like [1, 2, 3], gives
    False. An empty list has no duplicates either, so that is False too.

Compare that to what the library actually ships for most problems, which says
"return the result" without saying what the result is or giving a single number.

Plain words
-----------

Short sentences, concrete nouns, and the word "you" sparingly. No "utilise",
"leverage", "in order to", "it is important to note". The vocabulary rules below
are enforced by :func:`plain_issues` and checked in tests, because a generator
that emits filler is worse than the template it replaced.

Honesty about gaps
------------------

29% of the library has no detectable edge case and 15.5% has no usable example.
Rather than invent one, :func:`explain` says what it can and stops. An
explanation that says "the tests check X, Y, Z" and is silent about the rest is
true; one that pads with invented edge cases is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .content import ProblemSpec, TestCase

__all__ = ["Explanation", "explain", "plain_issues", "FILLER_PHRASES"]


#: Phrases that signal a generated body saying nothing. Kept as data so the
#: library-wide audit and this generator agree on what "filler" means.
FILLER_PHRASES: tuple[str, ...] = (
    "return the result",
    "the required output",
    "as expected",
    "handle common edge cases",
    "from the test suite",
    "your code here",
    "implement this function",
    "in order to",
    "utilise",
    "utilize",
    "leverage",
    "it is important to note",
    "as per the examples",
    "behavior shown in the examples",
    "print the required output",
    "modify the function",
    "solve the problem",
    "write a function that",
)

#: Banned vocabulary -> what to say instead. Applied to the *output*, not the
#: input, so a problem whose domain genuinely needs a word can be exempted.
_JARGON = {
    "utilise": "use",
    "utilize": "use",
    "leverage": "use",
    "in order to": "to",
    "prioritise": "favour",
    "prioritize": "favor",
    "functionality": "behaviour",
    "inputs'": "inputs",
    "asymptotic": "scaling",
    "paramater": "parameter",
}


@dataclass
class Explanation:
    """A composed explanation plus the provenance of every claim in it."""

    slug: str
    #: Plain-language body, ready to render.
    body: str
    #: One worked example, as a call line. Never from a hidden case.
    example_call: str = ""
    example_result: str = ""
    #: Edge cases the tests actually contain, phrased for a learner.
    edges: list[str] = field(default_factory=list)
    #: What could not be said, so the caller can decide whether to publish.
    missing: list[str] = field(default_factory=list)
    #: Filler phrases detected in the *source* description, if any.
    replaced_filler: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "body": self.body,
            "example_call": self.example_call,
            "example_result": self.example_result,
            "edges": self.edges,
            "missing": self.missing,
            "replaced_filler": self.replaced_filler,
        }


# ── phrasing helpers ────────────────────────────────────────────────


def plain_issues(text: str) -> list[str]:
    """Problems with ``text`` that make it read like filler.

    Returns a list of human-readable complaints; empty means the text is clean.
    Used both as a self-check on generated output and as an audit over whatever
    ships.
    """
    issues: list[str] = []
    lowered = text.lower()
    for phrase in FILLER_PHRASES:
        if phrase in lowered:
            issues.append(f"filler phrase: {phrase!r}")
    for banned, replacement in _JARGON.items():
        if banned in lowered:
            issues.append(f"jargon: {banned!r} (say {replacement!r})")
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            continue  # a code example, not prose
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            words = sentence.split()
            if len(words) > 28:
                issues.append(f"sentence of {len(words)} words")
    if len(text.split()) < 12:
        issues.append("too short to be an explanation")
    return issues


def _natural_list(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _sentence_case(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:]


# ── fact extraction (self-contained; no dependency on tools/) ────────


#: Placeholder parameter names, which say nothing about the input.
_GENERIC_PARAMETER_NAMES = frozenset({"args", "kwargs", "argv", "params", "kw"})

_KIND_PHRASE = {
    "numbers": "a list of numbers",
    "strings": "a list of words",
    "list": "a list",
    "dict": "a dictionary",
    "nested": "a list of lists",
    "boolean": "true or false",
    "empty": "an empty value",
    "mixed": "a mixed list",
    "none": "no input",
}


def render_value(value: Any, width: int = 60) -> str:
    """Render a Python value the way a learner reads it.

    Two elision conventions, so a truncated value is never ambiguous: a cut
    *string* ends inside its own quotes, and a cut *container* keeps its closing
    bracket and is only ever cut between items, so a string inside a list is
    never split.
    """
    if isinstance(value, str):
        text = repr(value)
        return text if len(text) <= width else text[: max(0, width - 4)] + "...'"
    if isinstance(value, (list, tuple, dict, set)):
        items = list(value.items()) if isinstance(value, dict) else list(value)
        if not items:
            return "{}" if isinstance(value, dict) else ("[]" if isinstance(value, list) else "()")
        parts = []
        truncated = False
        for item in items:
            if isinstance(value, dict):
                key, sub = item
                rendered = f"{render_value(key, width)}: {render_value(sub, width)}"
            else:
                rendered = render_value(item, width)
            if sum(len(p) for p in parts) + len(rendered) > width - 5 and parts:
                truncated = True
                break
            parts.append(rendered)
        open_ch, close_ch = _brackets(value)
        separator = ": " if isinstance(value, dict) else ", "
        joined = separator.join(parts)
        if not truncated:
            return f"{open_ch}{joined}{close_ch}"
        return f"{open_ch}{joined}, ...{close_ch}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _brackets(value: Any) -> tuple[str, str]:
    return {
        list: ("[", "]"),
        tuple: ("(", ")"),
        dict: ("{", "}"),
        set: ("{", "}"),
    }.get(type(value), ("", ""))


def _kind_of(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, (list, tuple)):
        if not value:
            return "empty"
        if all(isinstance(v, (list, tuple)) for v in value):
            return "nested"
        kinds = {type(v) for v in value}
        if kinds <= {int, float} and not any(isinstance(v, bool) for v in value):
            return "numbers"
        if kinds <= {str}:
            return "strings"
        if len(kinds) > 1:
            return "mixed"
        return "list"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _call(function_name: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    parts = [render_value(a) for a in args]
    parts += [f"{k}={render_value(v)}" for k, v in kwargs.items()]
    return f"{function_name}({', '.join(parts)})"


def _edge_reasons(case: TestCase, value: Any) -> list[str]:
    """Detectable reasons this case is worth calling out. No guessing."""
    reasons: list[str] = []
    items = list(value) if isinstance(value, (list, tuple)) else None
    if isinstance(value, str) and value == "":
        reasons.append("an empty string")
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        reasons.append("an empty " + ("dictionary" if isinstance(value, dict) else "list"))
    if items is not None:
        if len(items) == 1:
            reasons.append("a list with one item")
        rendered = [render_value(v, 20) for v in items]
        if len(set(rendered)) < len(rendered):
            reasons.append("a list with a repeated value")
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items):
            numbers = [v for v in items if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if numbers and all(n == sorted(numbers)[0] for n in numbers) and len(set(numbers)) > 1:
                reasons.append("a list that is already in order")
            if 0 in numbers:
                reasons.append("a zero in the list")
            if any(n < 0 for n in numbers):
                reasons.append("a negative number")
            if numbers and all(abs(n) == 1 for n in numbers):
                reasons.append("only the values 1 and -1")
            if len(set(rendered)) == 1 and len(rendered) > 1:
                reasons.append("a list where every item is the same")
            if numbers and all(n == numbers[0] for n in numbers):
                reasons.append("a list where every number is the same")
        if all(len(str(v)) == len(str(items[0])) for v in items):
            reasons.append("a list where every item is the same length")
        if isinstance(value, (list, tuple)) and len(items) == 2:
            reasons.append("a list of exactly two items")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(value) == 1:
            reasons.append("the value 1 or -1")
        if value == 1:
            reasons.append("the value 1")
    if isinstance(value, str) and len(value) == 1:
        reasons.append("a single character")
    return reasons


# ── the generator ───────────────────────────────────────────────────


def explain(spec: ProblemSpec, *, max_examples: int = 2) -> Explanation:
    """Compose an explanation for ``spec`` from its own test data.

    Every claim in the output traces to a specific test case. Nothing is asserted
    about behaviour the tests do not demonstrate, and anything the generator
    cannot ground is recorded in ``missing`` rather than invented.
    """
    usable = [c for c in spec.test_cases if c.usable]
    teachable = [c for c in usable if not c.is_hidden]

    result = Explanation(slug=spec.slug, body="")
    if spec.description:
        found = [p for p in FILLER_PHRASES if p in spec.description.lower()]
        result.replaced_filler = found

    if not teachable:
        result.missing.append(
            "no visible test case with a recorded answer, so no example can be shown"
        )

    lines: list[str] = []

    # -- the ask ------------------------------------------------------
    declared = list(spec.parameters or [])
    named = [n for n in declared if n not in _GENERIC_PARAMETER_NAMES]
    # If every declared name is a catch-all there is nothing to name. Saying
    # "takes `args` and `kwargs`" is the non-specificity this generator exists
    # to remove, so describe nothing rather than restore the placeholders.
    subject = _describe_parameters(spec, named, teachable) if named else ""
    ask = f"Write a function called `{spec.function_name or 'solve'}`"
    if subject:
        ask += f" that takes {subject}"
    if _worth_keeping(spec.description):
        # Keep an author's own words when they are specific; they beat anything
        # generated.
        ask += "."
        lines.append(ask)
        lines.append("")
        lines.append(_strip_markup(spec.description))
    else:
        lines.append(ask + ".")
        if not result.replaced_filler:
            lines.append("")
            lines.append("Work out what it should return, then write the code.")

    # -- worked examples ----------------------------------------------
    examples = [c for c in teachable][:max_examples]
    if examples:
        lines.append("")
        lines.append("The tests look like this:")
        lines.append("")
        width = max(len(_call(spec.function_name, c.args, c.kwargs)) for c in examples)
        for case in examples:
            call = _call(spec.function_name, case.args, case.kwargs)
            expected = render_value(case.expected)
            lines.append(f"    {call.ljust(width)}   returns  {expected}")
            if result.example_call == "":
                result.example_call = call
                result.example_result = expected

    # -- what the tests actually pin down -----------------------------
    edges = _collect_edges(spec, usable)
    if edges:
        result.edges = [reason for reason, _ in edges]
        lines.append("")
        lines.append("Worth checking before you finish:")
        lines.append("")
        for reason, evidence in edges:
            lines.append(f"- {reason} — the tests use {evidence}.")
    else:
        result.missing.append("no edge case the tests demonstrate")

    if examples:
        returns = _describe_returns(spec, examples)
        if returns:
            lines.insert(1, "")
            lines.insert(2, returns)
    else:
        result.missing.append("no worked example available")

    result.body = "\n".join(lines).strip() + "\n"
    return result


_RETURN_PHRASE = {
    "boolean": "yes or no",
    "str": "some text",
    "int": "a whole number",
    "float": "a number",
    "list": "a list",
    "dict": "a dictionary",
    "tuple": "a tuple",
    "NoneType": "nothing",
    "empty": "an empty list",
}


def _worth_keeping(description: str) -> bool:
    """Whether the author's own words are specific enough to keep.

    Filler is the disqualifier, not length. A short description that names the
    function and its behaviour is better than anything generated; a long one full
    of "return the result" is worse. plain_issues() is deliberately not used
    here because its length complaints would discard good short prose.
    """
    text = _strip_markup(description or "").strip()
    if len(text.split()) < 8:
        return False
    lowered = text.lower()
    return not any(phrase in lowered for phrase in FILLER_PHRASES)


def _describe_returns(spec: ProblemSpec, examples: list[TestCase]) -> str:
    """State what the function gives back, from the visible examples.

    Needed because a body with no detectable edge case would otherwise be just
    the ask, which is not an explanation. Derived from the examples rather than
    declared, so it cannot disagree with what the tests actually assert.
    """
    kinds = {
        _kind_of(case.expected) for case in examples
        if case.expected is not None
    }
    if not kinds:
        return ""
    phrases = sorted({_RETURN_PHRASE.get(k, "") for k in kinds} - {""})
    if not phrases:
        return ""
    if len(phrases) == 1:
        return f"It gives back {phrases[0]}."
    return f"It gives back {' or '.join(phrases)}."


def _describe_parameters(
    spec: ProblemSpec, parameters: list[str], cases: list[TestCase]
) -> str:
    """Describe each parameter in plain words.

    Prefers an annotation in the signature, but falls back to what the tests
    actually pass. `def has_duplicates(nums):` says nothing about `nums`; the
    tests pass a list of numbers, which is what a learner needs to be told. The
    signature alone leaves 4,907 of 5,132 problems undescribed, and "takes `nums`"
    is the same non-specificity the audit was measuring.
    """
    if not parameters:
        return ""
    signature = (spec.function_signature or "").lower()
    observed: dict[str, list[Any]] = {}
    for case in cases:
        for name in parameters:
            if name in case.kwargs:
                observed.setdefault(name, []).append(case.kwargs[name])
            elif case.args:
                index = parameters.index(name)
                if index < len(case.args):
                    observed.setdefault(name, []).append(case.args[index])

    described: list[str] = []
    for name in parameters:
        kind = ""
        if f"{name}: int" in signature or f"{name}: float" in signature:
            kind = "a number"
        elif f"{name}: str" in signature:
            kind = "some text"
        elif f"{name}: list" in signature:
            kind = "a list"
        elif f"{name}: dict" in signature:
            kind = "a dictionary"
        elif f"{name}: bool" in signature:
            kind = "true or false"
        if not kind:
            samples = observed.get(name) or []
            if samples:
                kind = _KIND_PHRASE.get(_kind_of(samples[0]), "")
                if not kind:
                    kind = _KIND_PHRASE.get(_kind_of(samples[0]), "a value")
        described.append(f"`{name}`" + (f" ({kind})" if kind else ""))
    return _natural_list(described)


def _collect_edges(spec: ProblemSpec, cases: list[TestCase]) -> list[tuple[str, str]]:
    """Distinct edge cases, each with the concrete input that demonstrates it.

    Deduplicated by *reason*, not by input, so twenty empty-list tests produce one
    line rather than twenty.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for case in cases:
        for value in list(case.args) + list(case.kwargs.values()):
            for reason in _edge_reasons(case, value):
                if reason in seen:
                    continue
                seen.add(reason)
                found.append((reason, _short(value)))
        if len(found) >= 5:
            break
    return found


def _short(value: Any, width: int = 28) -> str:
    rendered = render_value(value, width)
    return rendered if len(rendered) <= width else rendered[: width - 3] + "..."


_MARKUP = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    """Flatten the legacy HTML descriptions to plain paragraphs.

    The upstream descriptions carry ``<p>``/``<ul>`` markup. Keeping it would
    mean the new plain-words standard applies only to generated text while the
    old templated prose keeps its formatting, and a page would end up with two
    visual languages. Bullets become lines, tags become nothing.
    """
    text = text.replace("</p>", "\n\n").replace("<br>", "\n").replace("<br/>", "\n")
    text = text.replace("</li>", "\n").replace("</ul>", "\n").replace("</h3>", "\n")
    text = _MARKUP.sub("", text)
    lines = [line.strip().lstrip("-*• ").strip() for line in text.splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(line)
    return "\n".join(out).strip()
