"""Tests for the explanation generator.

The bar is not "produces a string". It is that every sentence is *grounded* in
that problem's own test data, and that the output is plain. A generator which
invents an edge case is worse than the template it replaced, because the
invented one reads as authoritative.
"""

from __future__ import annotations

import pytest

from ducky.content import ProblemSpec, parse_test_case
from ducky.explain import (
    FILLER_PHRASES,
    explain,
    plain_issues,
    render_value,
)


def _spec(cases, **kwargs):
    base = dict(
        slug="s", title="T", function_name="f", parameters=["xs"],
        function_signature="def f(xs):", description="Return the result",
        test_cases=cases,
    )
    base.update(kwargs)
    return ProblemSpec(**base)


# ── value rendering ─────────────────────────────────────────────────


class TestRenderValue:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, "1"),
            (1.0, "1"),
            ("hi", "'hi'"),
            (True, "True"),
            (None, "None"),
            ([], "[]"),
            ({}, "{}"),
            ([1, 2, 3], "[1, 2, 3]"),
            ({"a": 1}, "{'a': 1}"),
        ],
    )
    def test_readable(self, value, expected):
        assert render_value(value) == expected

    def test_truncation_keeps_the_closing_bracket(self):
        text = render_value(list(range(100)), width=30)
        assert text.endswith("]")
        assert "..." in text

    def test_a_cut_string_ends_inside_its_quotes(self):
        """Two conventions, so a truncated value is never ambiguous."""
        text = render_value("x" * 200, width=30)
        assert text.endswith("...'")
        assert text.startswith("'")

    def test_a_container_is_never_cut_mid_item(self):
        text = render_value(["a" * 50, "b" * 50, "c" * 50], width=40)
        assert text.count("'") % 2 == 0, f"a string was split: {text}"

    def test_never_raises_on_awkward_values(self):
        for value in [set(), (), {1: {2: 3}}, [[[1]]], -0.0, 1e308]:
            render_value(value)


# ── the generator ───────────────────────────────────────────────────


class TestExplain:
    def test_states_the_ask(self):
        body = explain(_spec([parse_test_case(0, '{"args": {"xs": [1]}}', "1")])).body
        assert "Write a function called `f`" in body

    def test_parameter_kind_comes_from_the_tests_when_unannotated(self):
        """`def f(xs):` says nothing; the tests pass a list of numbers."""
        result = explain(_spec(
            [parse_test_case(0, '{"args": {"nums": [1, 2]}}', "2")],
            parameters=["nums"],
            function_signature="def f(nums):",
            description="Add up every number in the list and give back the total.",
        ))
        assert "a list of numbers" in result.body

    def test_signature_annotation_wins_over_inference(self):
        result = explain(_spec(
            [parse_test_case(0, '{"args": {"xs": [1]}}', "1")],
            function_signature="def f(xs: int) -> int:",
        ))
        assert "a number" in result.body

    def test_generic_parameter_names_are_dropped(self):
        """A signature of `def f(args, kwargs)` should not produce that text."""
        result = explain(_spec(
            [parse_test_case(0, "[1, 2]", "2")],
            parameters=["args", "kwargs"],
            function_signature="def f(args, kwargs):",
        ))
        assert "`kwargs`" not in result.body

    def test_shows_a_worked_example(self):
        result = explain(_spec([parse_test_case(0, '{"args": {"xs": [1, 2, 3, 1]}}', "True")]))
        assert "f(xs=[1, 2, 3, 1])" in result.body
        assert "returns  True" in result.body
        assert result.example_call == "f(xs=[1, 2, 3, 1])"

    def test_states_what_it_returns(self):
        result = explain(_spec([parse_test_case(0, '{"args": {"xs": [1]}}', "True")]))
        assert "It gives back yes or no." in result.body

    def test_names_detected_edge_cases(self):
        result = explain(_spec([
            parse_test_case(0, '{"args": {"xs": [1, 2, 1]}}', "True"),
            parse_test_case(1, '{"args": {"xs": []}}', "False", is_hidden=True),
        ]))
        assert "a repeated value" in " ".join(result.edges)
        assert "empty" in " ".join(result.edges)

    def test_edge_cases_are_deduplicated_by_reason(self):
        """Twenty empty-list tests should produce one line, not twenty."""
        cases = [parse_test_case(i, '{"args": {"xs": []}}', "False") for i in range(5)]
        result = explain(_spec(cases))
        assert sum(1 for e in result.edges if "empty" in e) == 1

    def test_never_leaks_a_hidden_expectation(self):
        result = explain(_spec([
            parse_test_case(0, '{"args": {"xs": [1]}}', "1"),
            parse_test_case(1, '{"args": {"xs": [2]}}', "SECRET", is_hidden=True),
        ]))
        assert "SECRET" not in result.body
        assert "SECRET" not in result.example_result
        assert "SECRET" not in str(result.to_dict())

    def test_records_gaps_rather_than_inventing(self):
        """No example and no edge case must be reported, not fabricated."""
        result = explain(_spec([parse_test_case(0, "", "x")]))
        assert not result.complete
        assert any("no worked example" in m for m in result.missing)

    def test_reports_source_filler_it_replaced(self):
        result = explain(_spec(
            [parse_test_case(0, '{"args": {"xs": [1]}}', "1")],
            description="Return the result and handle common edge cases.",
        ))
        assert "return the result" in result.replaced_filler

    def test_keeps_a_specific_author_description(self):
        """An author's own specific words beat anything generated."""
        good = (
            "Count how many times each word appears in the text, ignoring case, "
            "and return a dictionary mapping each word to its count."
        )
        result = explain(_spec(
            [parse_test_case(0, '{"args": {"xs": [1]}}', "1")], description=good
        ))
        assert "ignoring case" in result.body

    def test_strips_legacy_html_from_kept_descriptions(self):
        result = explain(_spec(
            [parse_test_case(0, '{"args": {"xs": [1]}}', "1")],
            description=(
                "<p>Count how many times each word appears in the text.</p>"
                "<ul><li>Ignore case, so 'Cat' and 'cat' are the same word.</li>"
                "<li>Words are split on whitespace.</li></ul>"
            ),
        ))
        assert "<p>" not in result.body
        assert "<ul>" not in result.body
        assert "Count how many times each word appears" in result.body
        assert "Ignore case" in result.body

    def test_a_short_description_is_replaced_rather_than_kept(self):
        """Five words is not an explanation, however well written."""
        result = explain(_spec(
            [parse_test_case(0, '{"args": {"xs": [1]}}', "1")],
            description="<p>Count the words.</p>",
        ))
        assert "Count the words." not in result.body
        assert "The tests look like this" in result.body

    def test_a_zero_argument_function_is_still_explained(self):
        """Regression: zero-arg cases were excluded, so these had no examples."""
        result = explain(_spec(
            [parse_test_case(0, "[]", "'HELLO'")],
            parameters=[], function_signature="def f() -> str:",
        ))
        assert "f()" in result.body
        assert "'HELLO'" in result.body

    def test_output_is_plain(self):
        result = explain(_spec([
            parse_test_case(0, '{"args": {"xs": [1, 2, 1]}}', "True"),
            parse_test_case(1, '{"args": {"xs": [0]}}', "True"),
        ]))
        assert plain_issues(result.body) == []


# ── the plain-words check ───────────────────────────────────────────


class TestPlainIssues:
    def test_clean_text_passes(self):
        assert plain_issues(
            "Count each word and return how many times it appears. "
            "The tests check both an empty string and a repeated word."
        ) == []

    @pytest.mark.parametrize("phrase", FILLER_PHRASES)
    def test_every_declared_filler_phrase_is_detected(self, phrase):
        assert plain_issues(f"Write it. {phrase}. Done.") != []

    @pytest.mark.parametrize(
        ("text", "banned"),
        [
            ("utilise a set here to track it", "utilise"),
            ("We leverage the loop to do it", "leverage"),
            ("Add a counter in order to track it", "in order to"),
        ],
    )
    def test_jargon_is_detected(self, text, banned):
        issues = plain_issues(text)
        assert any(banned in issue for issue in issues), issues

    def test_a_long_sentence_is_flagged(self):
        issues = plain_issues(" ".join(["word"] * 40) + ".")
        assert any("sentence of" in issue for issue in issues)

    def test_a_code_block_is_not_read_as_prose(self):
        """Indented example lines have no sentence structure."""
        body = "The tests look like this:\n\n" + "\n".join(
            f"    f(x={i})   returns  {i}" for i in range(30)
        )
        assert not any("sentence of" in i for i in plain_issues(body))

    def test_too_short_is_flagged(self):
        assert any("too short" in i for i in plain_issues("Do it."))
