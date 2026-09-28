"""Tests for tools/derive_examples.py -- the tool that turns test cases into facts.

The three properties that would be expensive to get wrong, and cheap to lose:

1. **A hidden case's expected value never leaves the tool.** Not as an expected
   value, and not as an edge reason -- "not found" is a statement about the
   answer, and a reason string is as quotable as a value. ``TestHiddenRedaction``
   is the class to read first.
2. **Truncation is always marked.** A silently shortened value would let a
   rewriter assert a twelve-element list where the test passes five hundred.
   ``TestRendering`` asserts the ``...`` survives, and that the flag that
   reports it is propagated out of nested containers.
3. **Edge cases are detected, not guessed.** Every reason is a property read off
   the data. ``TestEdgeDetection`` pins the exact set, including the two reasons
   that were removed because they fired on a third of the library and told a
   writer nothing.

The value kinds and the report shape are covered too, because a fact base whose
vocabulary drifts is worse than none: a rewriter pattern-matching on "empty
list" has to be able to trust that the string means what it says.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from ducky.content import ProblemSpec, TestCase, parse_test_case

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"


def _load_tool(name: str):
    """Import a module from tools/ by path, the way the script is invoked."""
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


derive_examples = _load_tool("derive_examples")

render_value = derive_examples.render_value
worked_example = derive_examples.worked_example
edge_cases = derive_examples.edge_cases
edge_reasons = derive_examples.edge_reasons
input_kind = derive_examples.input_kind
describe_inputs = derive_examples.describe_inputs
derive = derive_examples.derive


def case(index: int, args=None, kwargs=None, expected=None, *, hidden=False, error="") -> TestCase:
    """A TestCase built directly, for cases where the encoding is not the point."""
    return TestCase(
        index=index,
        args=list(args or []),
        kwargs=dict(kwargs or {}),
        expected=expected,
        is_hidden=hidden,
        description=f"Test {index + 1}",
        parse_error=error,
    )


# ── rendering ─────────────────────────────────────────────────────────


class TestRendering:
    def test_scalars_render_as_python_literals(self):
        assert render_value(42) == "42"
        assert render_value(3.5) == "3.5"
        assert render_value(True) == "True"
        assert render_value(None) == "None"

    def test_strings_are_quoted(self):
        assert render_value("a") == "'a'"
        assert render_value("two words") == "'two words'"

    def test_a_string_containing_a_quote_stays_a_well_formed_literal(self):
        # repr() switches to double quotes here; the renderer has to follow it
        # rather than pasting a single quote inside a single-quoted string.
        assert render_value("it's") == '"it\'s"'

    def test_short_containers_stay_on_one_line(self):
        assert render_value([1, 2, 3]) == "[1, 2, 3]"
        assert render_value({"a": 1}) == "{'a': 1}"
        assert render_value([]) == "[]"
        assert render_value({}) == "{}"

    def test_a_container_that_does_not_fit_goes_multi_line(self):
        # Ten 10-character strings: 130 characters inline, 10 lines blockwise,
        # so nothing is elided and the layout choice is visible on its own.
        rendered = render_value(["x" * 10] * 10)
        assert rendered.startswith("[\n")
        assert rendered.endswith("\n]")
        assert "    'xxxxxxxxxx',\n" in rendered

    def test_multi_line_dict_closes_with_a_brace(self):
        # A dict is the one container where the closing bracket differs from a
        # list's, and getting it wrong produced `{ ... ]` in the first run.
        rendered = render_value({f"key{i}": [f"value{i}", "x" * 10] for i in range(4)})
        assert rendered.startswith("{\n")
        assert rendered.endswith("\n}")

    def test_a_long_value_is_truncated_not_dropped(self):
        rendered = render_value("a" * 300)
        assert derive_examples.ELLIPSIS in rendered
        assert len(rendered) < 300
        assert rendered.startswith("'") and rendered.endswith("'")

    def test_a_truncated_string_keeps_its_quote_and_marks_the_cut(self):
        rendered = render_value("b" * 200, width=30)
        assert rendered == "'" + "b" * 25 + derive_examples.ELLIPSIS + "'"
        assert len(rendered) <= 30

    def test_a_truncated_container_keeps_its_closing_bracket(self):
        rendered = render_value(list(range(400)))
        assert derive_examples.ELLIPSIS in rendered
        assert rendered.rstrip().endswith("]")
        assert len(rendered.splitlines()) <= derive_examples.BLOCK_MAX_LINES + 2

    def test_a_container_block_does_not_claim_to_be_truncated(self):
        # Going multi-line loses nothing, so `truncated` must stay False. Only a
        # dropped `...` counts as truncation.
        text, cut = derive_examples._render(["x" * 10] * 10, 40)
        assert "\n" in text and not cut
        text, cut = derive_examples._render({f"k{i}": ["x" * 10] for i in range(6)}, 40)
        assert "\n" in text and not cut
        assert derive_examples.ELLIPSIS not in text

    def test_truncation_is_propagated_out_of_nested_containers(self):
        # The outer brackets fitting says nothing about the inner value having
        # been cut; the flag has to be an OR over the whole subtree.
        value = {"k": [[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]]}
        text, cut = derive_examples._render(value, 40)
        assert derive_examples.ELLIPSIS in text
        assert cut

    def test_a_value_that_cannot_represent_itself_does_not_crash(self):
        class Hostile:
            def __repr__(self):
                raise RuntimeError("no repr for you")

        assert render_value(Hostile()) == "<unrepresentable>"

    def test_non_finite_floats_render_as_constructions_not_names(self):
        # `nan` is not a Python literal. A reader copying it into a test gets a
        # NameError instead of the value.
        assert render_value(float("nan")) == "float('nan')"
        assert render_value(float("inf")) == "float('inf')"

    def test_awkward_values_do_not_crash_the_renderer(self):
        awkward = [
            {"k": [[1, 2], {"x": "y"}]},
            [{"a": [1, {"b": [2, {"c": 3}]}]}],
            (),
            set(),
            [[]],          # a list containing an empty list
            [None],        # a list containing None
            {"None": 1},   # the key None
            b"bytes",      # not JSON-shaped, but a str subclass risk
            [1e400 * 10 if False else 1e308, -1e308],
            {"nested": {"deep": {"deeper": ["x" * 50]}}},
        ]
        for value in awkward:
            assert isinstance(render_value(value), str)
            assert render_value(value, width=12)

    def test_a_width_too_small_to_render_anything_is_rejected(self):
        with pytest.raises(ValueError):
            render_value([1, 2, 3], width=3)


# ── value kinds ───────────────────────────────────────────────────────


class TestValueKinds:
    """One test per kind, plus the boundaries between them."""

    def test_numbers(self):
        assert input_kind(case(0, args=[2, 3])) == "numbers"

    def test_strings(self):
        assert input_kind(case(0, kwargs={"text": "hello"})) == "strings"

    def test_list(self):
        assert input_kind(case(0, kwargs={"nums": [1, 2, 3]})) == "list"

    def test_dict(self):
        assert input_kind(case(0, kwargs={"grid": {"a": 1}})) == "dict"

    def test_nested(self):
        assert input_kind(case(0, kwargs={"grid": [[1, 2], [3, 4]]})) == "nested"

    def test_boolean(self):
        assert input_kind(case(0, kwargs={"flag": True})) == "boolean"

    def test_empty(self):
        assert input_kind(case(0, kwargs={"nums": [], "word": ""})) == "empty"

    def test_none(self):
        assert input_kind(case(0)) == "none"
        assert input_kind(case(0, args=[None])) == "none"

    def test_mixed(self):
        assert input_kind(case(0, args=[3, "x"])) == "mixed"
        assert input_kind(case(0, args=[[1], {"a": 1}])) == "mixed"

    def test_a_container_beats_a_scalar_travelling_with_it(self):
        # count_occurrences(["a", "b", "a"], "a") is a list problem. Calling it
        # "mixed" would make a writer say "some values and a string" when the
        # specific answer is "a list of words and a letter".
        assert input_kind(case(0, args=[["a", "b", "a"], "a"])) == "list"

    def test_every_kind_is_in_the_published_vocabulary(self):
        fixtures = [
            case(0, args=[2, 3]),
            case(1, kwargs={"text": "hello"}),
            case(2, kwargs={"nums": [1, 2, 3]}),
            case(3, kwargs={"grid": {"a": 1}}),
            case(4, kwargs={"grid": [[1, 2], [3, 4]]}),
            case(5, kwargs={"flag": True}),
            case(6, kwargs={"nums": []}),
            case(7),
            case(8, args=[3, "x"]),
        ]
        kinds = {input_kind(c) for c in fixtures}
        assert kinds == set(derive_examples.VALUE_KINDS)

    def test_describe_inputs_names_the_shape(self):
        assert describe_inputs(case(0, kwargs={"nums": [1, 2, 3]})) == "a list of numbers"
        assert describe_inputs(case(1, kwargs={"text": "hello"})) == "a string"
        assert describe_inputs(case(2, kwargs={"grid": [[1], [2]]})) == "a list of lists"
        assert describe_inputs(case(3, kwargs={"nums": []})) == "an empty list"
        assert describe_inputs(case(4)) == "no arguments"
        assert describe_inputs(case(5, args=[2, 3])) == "a number and a number"

    def test_a_long_string_argument_is_called_very_long(self):
        # Only unambiguous length facts survive for strings; a 1-char string is
        # usually a search key, not the problem's whole input.
        assert edge_reasons(case(0, args=["a" * 40], expected=1)) == ["very long"]
        assert edge_reasons(case(1, args=["a"], expected=1)) == []


# ── edge detection ────────────────────────────────────────────────────


class TestEdgeDetection:
    def test_empty_input(self):
        assert "empty list" in edge_reasons(case(0, kwargs={"nums": []}))
        assert "empty string" in edge_reasons(case(1, args=[""]))
        assert "empty dictionary" in edge_reasons(case(2, kwargs={"d": {}}))

    def test_single_element(self):
        assert "single element" in edge_reasons(case(0, kwargs={"nums": [7]}))

    def test_duplicate_elements(self):
        assert "duplicate elements" in edge_reasons(case(0, kwargs={"nums": [1, 1, 2]}))

    def test_duplicates_distinguish_one_from_the_string(self):
        # repr, not str: 1 and "1" are not the same element.
        assert "duplicate elements" not in edge_reasons(case(0, kwargs={"nums": [1, "1"]},
                                                         expected=1))
        assert "duplicate elements" in edge_reasons(case(1, kwargs={"nums": ["1", "1"]},
                                                        expected=1))

    def test_zero_and_negative(self):
        assert "zero" in edge_reasons(case(0, args=[0, 5]))
        assert "negative" in edge_reasons(case(1, args=[-3]))

    def test_boundary_value_only_for_a_whole_input_of_one(self):
        assert "boundary value" in edge_reasons(case(0, args=[1]))
        assert "boundary value" in edge_reasons(case(1, args=[-1]))
        # The 1 in [1, 2, 3] is an element, not an edge case. Flagging it made
        # "boundary value" the most common reason in the library by 6x.
        assert "boundary value" not in edge_reasons(case(2, args=[[1, 2, 3]]))

    def test_very_long(self):
        assert "very long" in edge_reasons(case(0, kwargs={"nums": list(range(50))}))

    def test_already_sorted(self):
        assert "already sorted" in edge_reasons(case(0, kwargs={"nums": [1, 2, 3]}))
        assert "already sorted" not in edge_reasons(case(1, kwargs={"nums": [3, 1, 2]}))
        # Words are not compared: sorted() on strings is a different question,
        # and "already sorted" there would be about alphabetical order.
        assert "already sorted" not in edge_reasons(case(2, kwargs={"words": ["a", "b"]}))

    def test_not_found_only_for_a_whole_answer_of_minus_one(self):
        assert "not found" in edge_reasons(case(0, args=[[1, 2]], expected=-1))
        assert "not found" in edge_reasons(case(1, args=[[1, 2]], expected="Not Found"))
        # A countdown that reaches below zero is not a lookup that failed.
        assert "not found" not in edge_reasons(case(2, args=[1, 2], expected=[1, -1]))

    def test_missing_expected_value_is_reported_as_such(self):
        assert edge_reasons(case(0, args=[7], expected=None)) == ["no expected value recorded"]

    def test_empty_result(self):
        assert "empty result" in edge_reasons(case(0, args=[[1, 2]], expected=[]))
        # One level into a structured answer, because an empty part of it is a
        # fact the input side cannot give.
        assert "empty result" in edge_reasons(
            case(1, args=[[1, 2]], expected={"common": [], "left": [1]})
        )

    def test_every_reason_comes_from_the_published_vocabulary(self):
        fixtures = [
            case(0, args=[[1, 1, 2, 0, -5]], expected=-1),
            case(1, kwargs={"a": "", "b": [], "c": {}}, expected=[]),
            case(2, args=[1], expected="Not Found"),
            case(3, args=[["x"] * 40], expected=None),
            case(4, args=[[1, 2, 3]]),
        ]
        found = {r for c in fixtures for r in edge_reasons(c)}
        assert found <= set(derive_examples.REASON_PRIORITY)
        assert found  # and it is not vacuously true

    def test_reasons_are_ordered_by_specificity(self):
        # Empty and sorted at once: the sharper fact takes the headline.
        reasons = edge_reasons(case(0, args=[[1, 2]], expected=[]))
        assert reasons[0] == "empty result"

    def test_edge_cases_skips_unusable_cases(self):
        broken = case(0, args=[1], error="unparseable expected_output: 'result'")
        assert edge_cases([broken], "f") == []


# ── hidden-case redaction ─────────────────────────────────────────────


class TestHiddenRedaction:
    """The answer key must not reach the fact base by any route."""

    def _spec(self) -> ProblemSpec:
        return ProblemSpec(
            slug="count-occurrences",
            title="Count occurrences",
            difficulty="Beginner",
            function_name="count_occurrences",
            parameters=["words", "target"],
            function_signature="def count_occurrences(words, target):",
            tags=["strings", "loops"],
            test_cases=[
                parse_test_case(0, '{"args": {"words": ["a", "b", "a"], "target": "a"}}', "2"),
                # A hidden case whose answer is the not-found sentinel. The
                # reason string is as quotable as the value, so it must go too.
                parse_test_case(1, '{"args": {"words": ["x"], "target": "y"}}', "-1",
                                is_hidden=True),
                parse_test_case(2, '{"args": {"words": ["x"], "target": "y"}}', "0",
                                is_hidden=True),
            ],
        )

    def test_a_hidden_case_never_emits_its_expected_value(self):
        for case_ in self._spec().test_cases:
            example = worked_example(case_, "count_occurrences")
            if not case_.is_hidden:
                assert example["expected"] != derive_examples.EXPECTED_HIDDEN
                continue
            assert example["expected"] == derive_examples.EXPECTED_HIDDEN
            # Belt and braces: the literal answer is nowhere in the rendering.
            assert "-1" not in json.dumps(example)
            assert "0" not in json.dumps(example["expected"])

    def test_a_hidden_case_withheld_its_expected_derived_edge_reason(self):
        hidden = self._spec().test_cases[1]
        assert hidden.expected == -1
        example = worked_example(hidden, "count_occurrences")
        assert example["is_hidden"] is True
        assert "not found" not in example["edge_reasons"]
        assert "not found" not in json.dumps(example)

    def test_a_hidden_case_still_reports_its_input_side_edges(self):
        # The input of a hidden case is not secret -- the content store serves it
        # and moves only the expectation aside -- so withholding these would lose
        # real information for no gain.
        hidden = self._spec().test_cases[1]
        example = worked_example(hidden, "count_occurrences")
        assert example["edge_reasons"] == ["single element"]
        assert set(example["edge_reasons"]) <= set(derive_examples.INPUT_EDGE_REASONS)

    def test_a_hidden_case_is_never_teachable(self):
        for case_ in self._spec().test_cases:
            example = worked_example(case_, "count_occurrences")
            assert example["teachable"] is (not case_.is_hidden)

    def test_derive_does_not_leak_a_hidden_expectation(self):
        fact = derive(self._spec())
        blob = json.dumps(fact)
        # "-1" appears nowhere except as an edge reason we just proved is
        # withheld, and the hidden cases' answers are 0 and -1.
        assert '"expected": "-1"' not in blob
        assert '"expected": "0"' not in blob
        hidden_entries = [w for w in fact["worked"] if w["is_hidden"]]
        assert hidden_entries
        for entry in hidden_entries:
            assert entry["expected"] == derive_examples.EXPECTED_HIDDEN

    def test_a_visible_case_still_shows_its_answer(self):
        # The other direction: redaction that eats everything is not redaction.
        visible = self._spec().test_cases[0]
        assert worked_example(visible, "count_occurrences")["expected"] == "2"

    def test_the_redaction_marker_is_the_one_the_grader_uses(self):
        # CaseResult.student_view() redacts with this exact string. Two markers
        # would mean a consumer has to know about both.
        from ducky.verify import CaseResult

        hidden = CaseResult(index=0, passed=False, expected_repr="42", is_hidden=True)
        assert hidden.student_view()["expected_repr"] == derive_examples.EXPECTED_HIDDEN


# ── worked examples ───────────────────────────────────────────────────


class TestWorkedExample:
    def test_a_positional_call_renders_the_way_the_test_calls_it(self):
        example = worked_example(case(0, args=[["a", "b", "a"], "a"], expected=2), "count_occurrences")
        assert example["call"] == "count_occurrences(['a', 'b', 'a'], 'a')"
        assert example["expected"] == "2"

    def test_keyword_arguments_are_broken_out(self):
        example = worked_example(case(0, kwargs={"words": [1, 2]}, expected=2), "total")
        assert example["args_text"] == ""
        assert example["kwargs_text"] == "words=[1, 2]"
        assert example["call"] == "total(words=[1, 2])"

    def test_positional_and_keyword_arguments_compose(self):
        example = worked_example(case(0, args=[1], kwargs={"c": 2}, expected=3), "f")
        assert example["call"] == "f(1, c=2)"

    def test_a_problem_with_no_function_name_is_visible_in_the_output(self):
        example = worked_example(case(0, args=[1], expected=1), "")
        assert "<no function name>" in example["call"]

    def test_a_prose_expected_value_is_flagged_rather_than_quoted(self):
        # "Zero" is a label upstream, not an answer. Quoting it as one is how a
        # rewriter ends up teaching a student that the answer is the word Zero.
        example = worked_example(case(0, args=[0], expected="Zero"), "f")
        assert example["expected_state"] == "prose"
        assert example["teachable"] is False

    def test_a_placeholder_expected_value_is_flagged(self):
        example = worked_example(case(0, args=[1], expected="result"), "f")
        assert example["expected_state"] == "placeholder"
        assert example["teachable"] is False

    def test_a_missing_expected_value_is_flagged(self):
        example = worked_example(case(0, args=[1], expected=None), "f")
        assert example["expected_state"] == "missing"
        assert example["teachable"] is False

    def test_truncated_is_reported_when_an_argument_was_cut(self):
        example = worked_example(case(0, args=[list(range(400))], expected=1), "f", width=40)
        assert example["truncated"] is True
        assert derive_examples.ELLIPSIS in example["args_text"]

    def test_truncated_is_false_when_nothing_was_cut(self):
        example = worked_example(case(0, args=[1, 2], expected=3), "f")
        assert example["truncated"] is False


# ── edge case selection ───────────────────────────────────────────────


class TestEdgeCaseSelection:
    def _cases(self) -> list[TestCase]:
        return [
            case(0, kwargs={"nums": []}, expected=0),
            case(1, kwargs={"nums": []}, expected=0),      # same fact, twice
            case(2, kwargs={"nums": [5]}, expected=5),
            case(3, kwargs={"nums": [1, 2, 3]}, expected=6),
            case(4, args=[7, 8], expected=15),             # not an edge
        ]

    def test_only_the_cases_with_a_reason_are_returned(self):
        found = edge_cases(self._cases(), "f")
        assert [e["index"] for e in found] == [0, 2, 3]

    def test_repeats_of_one_fact_collapse_to_their_lowest_index(self):
        # 36 generated cases per problem, most of them the same shape. Reporting
        # one fact with a count beats reporting it nine times.
        found = edge_cases(self._cases(), "f")
        empty = next(e for e in found if e["index"] == 0)
        assert empty["same_shape"] == 2

    def test_dedupe_can_be_turned_off(self):
        found = edge_cases(self._cases(), "f", dedupe=False)
        assert [e["index"] for e in found] == [0, 1, 2, 3]

    def test_a_limit_is_honoured(self):
        assert len(edge_cases(self._cases(), "f", limit=2)) == 2

    def test_every_entry_carries_a_machine_readable_reason(self):
        for entry in edge_cases(self._cases(), "f"):
            assert entry["primary_reason"] in derive_examples.REASON_PRIORITY
            assert entry["reasons"]
            assert entry["primary_reason"] == entry["reasons"][0]


# ── the fact base ─────────────────────────────────────────────────────


def _spec_with_edges() -> ProblemSpec:
    return ProblemSpec(
        slug="find-word",
        title="Find a word",
        difficulty="Intermediate",
        function_name="find_word",
        parameters=["words", "target"],
        function_signature="def find_word(words: list[str], target: str) -> list:",
        tags=["lists", "search"],
        test_cases=[
            parse_test_case(0, '{"args": {"words": ["a", "b"], "target": "b"}}', "[True, 1]"),
            parse_test_case(1, '{"args": {"words": [], "target": ""}}', "[False, -1]"),
            parse_test_case(2, '{"args": {"words": ["x"], "target": "y"}}', "[False, -1]",
                            is_hidden=True),
        ],
    )


class TestDerive:
    def test_the_fact_base_has_the_documented_shape(self):
        fact = derive(_spec_with_edges())
        for key in ("slug", "function_name", "parameters", "signature", "worked", "edges",
                    "value_kinds", "difficulty", "tags"):
            assert key in fact, key
        assert fact["slug"] == "find-word"
        assert fact["function_name"] == "find_word"
        assert fact["parameters"] == ["words", "target"]
        assert fact["difficulty"] == "Intermediate"
        assert fact["tags"] == ["lists", "search"]

    def test_value_kinds_aggregate_the_examples(self):
        fact = derive(_spec_with_edges())
        # No case here is all-strings: `target` travels with a list, and a
        # container wins, so "strings" is not a kind this problem has.
        assert fact["value_kinds"] == ["empty", "list"]
        assert all(k in derive_examples.VALUE_KINDS for k in fact["value_kinds"])

    def test_value_means_edges_and_a_visible_example(self):
        # The brief's definition, kept as a boolean.
        assert derive(_spec_with_edges())["valuable"] is True

        no_edges = _spec_with_edges()
        no_edges.test_cases = [
            parse_test_case(0, '{"args": {"words": ["a", "b"], "target": "b"}}', "[True, 1]")
        ]
        fact = derive(no_edges)
        assert fact["edge_count"] == 0
        assert fact["valuable"] is False

    def test_a_problem_with_only_hidden_cases_is_not_valuable(self):
        spec = _spec_with_edges()
        spec.test_cases = [
            parse_test_case(0, '{"args": {"words": [], "target": ""}}', "[False, -1]",
                            is_hidden=True)
        ]
        fact = derive(spec)
        assert fact["valuable"] is False
        assert fact["worked"]  # the inputs are still reported, redacted
        assert all(w["is_hidden"] for w in fact["worked"])

    def test_counts_record_what_was_dropped(self):
        spec = _spec_with_edges()
        spec.test_cases = spec.test_cases * 6  # 18 near-identical cases
        fact = derive(spec)
        assert fact["cases_total"] == 18
        assert fact["cases_usable"] == 18
        assert fact["cases_hidden"] == 6
        assert fact["edge_count"] >= fact["edges_kept"]
        assert fact["worked_kept"] == len(fact["worked"]) <= derive_examples.MAX_WORKED

    def test_worked_examples_prefer_teachable_visible_edges(self):
        fact = derive(_spec_with_edges())
        assert fact["worked"][0]["teachable"] is True
        assert fact["worked"][0]["is_hidden"] is False
        assert fact["worked"][0]["is_edge_case"] is True

    def test_a_problem_with_no_function_name_is_still_derivable(self):
        spec = _spec_with_edges()
        spec.function_name = ""
        fact = derive(spec)
        assert fact["callable"] is False
        assert fact["worked"][0]["call"].startswith("<no function name>(")

    def test_a_problem_with_no_usable_cases_does_not_crash(self):
        spec = _spec_with_edges()
        spec.test_cases = [case(0, error="unparseable expected_output: 'result'")]
        fact = derive(spec)
        assert fact["worked"] == []
        assert fact["edges"] == []
        assert fact["valuable"] is False
        assert fact["cases_usable"] == 0


# ── the tool, end to end ──────────────────────────────────────────────


UPSTREAM_SCHEMA = """
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


def _upstream_db(path: Path, problems: list[dict], cases: list[dict]) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(UPSTREAM_SCHEMA)
    for row in problems:
        conn.execute(
            'INSERT INTO coding_problems (id, title, slug, difficulty, function_signature,'
            ' tags, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)',
            (row["id"], row["title"], row["slug"], row.get("difficulty", "Beginner"),
             row.get("function_signature", ""), row.get("tags", "[]")),
        )
    for index, row in enumerate(cases):
        conn.execute(
            'INSERT INTO test_cases (problem_id, input_data, expected_output, is_hidden,'
            ' description, "order") VALUES (?, ?, ?, ?, ?, ?)',
            (row["problem_id"], row["input_data"], row["expected_output"],
             row.get("is_hidden", 0), row.get("description", ""), index),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def probe_db(tmp_path: Path) -> Path:
    return _upstream_db(
        tmp_path / "coding_platform.db",
        [
            {"id": 1, "title": "Count occurrences", "slug": "count-occurrences",
             "function_signature": "def count_occurrences(words, target):",
             "tags": '["strings"]'},
            {"id": 2, "title": "Add two numbers", "slug": "add-two-numbers",
             "function_signature": "def add_two_numbers(a, b):", "tags": "[]"},
        ],
        [
            # A problem with a visible example and a visible edge case.
            {"problem_id": 1, "input_data": '{"args": {"words": ["a", "b", "a"], "target": "a"}}',
             "expected_output": "2"},
            {"problem_id": 1, "input_data": '{"args": {"words": [], "target": "a"}}',
             "expected_output": "0"},
            {"problem_id": 1, "input_data": '{"args": {"words": ["x"], "target": "y"}}',
             "expected_output": "-1", "is_hidden": 1},
            # A problem with no edge case at all.
            {"problem_id": 2, "input_data": '{"args": {"a": 2, "b": 3}}', "expected_output": "5"},
            {"problem_id": 2, "input_data": '{"args": {"a": 4, "b": 5}}', "expected_output": "9"},
        ],
    )


class TestReport:
    def test_the_report_separates_the_corpus_from_the_selection(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        assert derive_examples.main(
            ["--db", str(probe_db), "--out", str(out), "--top", "1", "--quiet"]
        ) == 0
        report = json.loads(out.read_text())
        assert report["totals"]["problems"] == 2
        assert report["totals"]["problems_with_usable_example"] == 2
        assert report["totals"]["problems_with_teachable_example"] == 2
        assert report["totals"]["problems_with_edges"] == 1
        assert report["totals"]["problems_valuable"] == 1
        assert report["totals"]["selected"] == 1
        assert report["selected_slugs"] == ["count-occurrences"]
        assert len(report["problems"]) == 1

    def test_the_selection_ranks_by_value_first(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        derive_examples.main(["--db", str(probe_db), "--out", str(out), "--top", "2", "--quiet"])
        report = json.loads(out.read_text())
        # "add-two-numbers" has no edge case, so it cannot be the most valuable
        # however many examples it has.
        assert [p["slug"] for p in report["problems"]] == ["count-occurrences", "add-two-numbers"]

    def test_a_slug_filter_selects_one_problem(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        derive_examples.main(
            ["--db", str(probe_db), "--out", str(out), "--slug", "add-two-numbers",
             "--top", "5", "--quiet"]
        )
        report = json.loads(out.read_text())
        assert report["totals"]["problems"] == 1
        assert report["problems"][0]["slug"] == "add-two-numbers"

    def test_the_written_report_contains_no_hidden_answer(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        derive_examples.main(["--db", str(probe_db), "--out", str(out), "--top", "5", "--quiet"])
        report = json.loads(out.read_text())
        hidden = [w for p in report["problems"] for w in p["worked"] if w["is_hidden"]]
        assert hidden, "the fixture has a hidden case; it should appear redacted"
        for entry in hidden:
            assert entry["expected"] == derive_examples.EXPECTED_HIDDEN
        assert '"expected": "-1"' not in out.read_text()

    def test_the_report_counts_value_kinds_and_edge_reasons(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        derive_examples.main(["--db", str(probe_db), "--out", str(out), "--top", "5", "--quiet"])
        report = json.loads(out.read_text())
        assert report["value_kinds"] == {"list": 2, "numbers": 1}
        assert report["edge_reasons"]["empty list"] == 1
        assert report["input_phrases"]

    def test_a_limit_caps_the_scan(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        derive_examples.main(
            ["--db", str(probe_db), "--out", str(out), "--top", "5", "--limit", "1", "--quiet"]
        )
        assert json.loads(out.read_text())["totals"]["problems"] == 1

    def test_a_missing_database_is_an_error_not_a_traceback(self, tmp_path):
        assert derive_examples.main(
            ["--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "x.json"), "--top", "1"]
        ) == 2

    def test_a_slug_that_matches_nothing_is_an_error(self, probe_db, tmp_path):
        out = tmp_path / "examples.json"
        assert derive_examples.main(
            ["--db", str(probe_db), "--out", str(out), "--slug", "no-such-problem", "--top", "1",
             "--quiet"]
        ) == 2
        assert not out.exists()

    def test_top_is_required(self, probe_db, tmp_path):
        with pytest.raises(SystemExit):
            derive_examples.main(
                ["--db", str(probe_db), "--out", str(tmp_path / "x.json"), "--quiet"]
            )
