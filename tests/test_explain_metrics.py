"""Tests for the explanation-genericity metrics in ``tools/explain_metrics.py``.

Every fixture here is synthetic and small. That is the point of the split in
the tool: the scoring half never opens a database, so a claim like "an empty
body scores 0" can be checked against four made-up problems instead of against
5,132 rows whose real behaviour nobody has pinned down. The numbers in the
docstrings come from running the tool against the library; they are quoted so
a change in the metric shows up as a failing expectation here rather than as a
quiet drift in a report.

The one test that touches SQLite builds a throwaway ``coding_problems`` table
in ``tmp_path`` and runs ``main()``, because the CLI's job is to decide what to
feed the pure functions, and that decision is worth testing.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str):
    """Import a module from tools/ by path, the way the script is invoked.

    ``sys.modules`` registration is required, not optional: ``dataclasses``
    resolves annotations through the module entry and raises on a module that
    was never registered.
    """
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


em = _load("explain_metrics")
Spec = em.Spec


# ── fixtures ───────────────────────────────────────────────────────────

PAD = " Read the rest of the page before you start."
TARGET = 320


def at_length(body: str, target: int = TARGET) -> str:
    """Pad a body to exactly ``target`` characters with inert prose.

    The length component is measured against the library, so a test that wants
    to fail *only* the length component -- or to pass it -- needs every other
    variable held still. Padding to an exact length is how the length changes
    without changing the words the other six components read. The tail of the
    padding can be cut mid-word, which no component reads.
    """
    if len(body) >= target:
        return body
    return body + (PAD * 40)[: target - len(body)]


GOOD_BODY = (
    "Reverse a String\n\n"
    "Write `reverse_string(text)`. The parameter `text` is a string. Return a new string that holds the "
    "same characters in the opposite order, and leave `text` itself alone.\n\n"
    "Example: `reverse_string('Python')` returns `'nohtyP'`.\n\n"
    "If `text` is an empty string, return an empty string rather than None."
)


def good_spec(slug: str = "reverse-a-string", body: str | None = None) -> Spec:
    return Spec(
        slug=slug,
        title="Reverse a String",
        function_signature="def reverse_string(text: str) -> str:",
        description=at_length(body if body is not None else GOOD_BODY),
    )


def template_spec(slug: str = "sum-a-list", body: str | None = None) -> Spec:
    """A body shaped exactly like the library's generator emits."""
    return Spec(
        slug=slug,
        title="Sum a List",
        function_signature="def total(numbers: list[int]) -> int:",
        description=at_length(
            body
            if body is not None
            else (
                "Sum a List\n\n"
                "Implement `total(numbers)`. Use the examples to determine the expected return value. "
                "Return the result from the function instead of printing it."
            )
        ),
    )


def twin_spec(slug: str, marker: str) -> Spec:
    """A second problem whose body is identical to :func:`template_spec`."""
    return Spec(
        slug=slug,
        title=f"Sum a List {marker}",
        function_signature="def total(numbers: list[int]) -> int:",
        description=template_spec("first", body=None).description,
    )


# ── normalise ──────────────────────────────────────────────────────────


def test_normalise_lowercases_and_collapses_whitespace():
    assert em.normalise("  Sum   a\n\nList.  ") == "sum a list"


def test_normalise_strips_the_title():
    assert em.normalise("Sum a List\n\nAdd the numbers.", "Sum a List") == "add the numbers"


def test_normalise_strips_the_signature():
    body = "Write the body. def total(numbers: list[int]) -> int: Then finish."
    assert "total" not in em.normalise(body, "", "def total(numbers: list[int]) -> int:")


def test_normalise_strips_the_function_name_by_default():
    body = "Call `total` on the list. Then return the sum."
    stripped = em.normalise(body, "", "def total(numbers: list[int]) -> int:")
    kept = em.normalise(body, "", "def total(numbers: list[int]) -> int:", strip_function_name=False)
    assert "total" not in stripped
    assert "total" in kept


def test_normalise_is_safe_on_empty_and_none():
    assert em.normalise("") == ""
    assert em.normalise("   \n  ") == ""
    assert em.normalise("", "a title", "def f():") == ""


def test_normalise_leaves_a_body_that_is_only_the_title_as_empty():
    assert em.normalise("Sum a List.", "Sum a List") == ""


# ── duplicate clusters ─────────────────────────────────────────────────


def test_two_identical_descriptions_form_a_cluster():
    rows = [template_spec("one"), template_spec("two"), good_spec("three")]
    clusters = em.duplicate_clusters(rows)
    assert len(clusters) == 1
    assert clusters[0]["size"] == 2
    assert clusters[0]["slugs"] == ["one", "two"]


def test_duplicate_clusters_ignore_a_unique_body():
    rows = [good_spec("one"), good_spec("two", body=GOOD_BODY.replace("opposite", "backwards"))]
    assert em.duplicate_clusters(rows) == []


def test_duplicate_clusters_are_ranked_largest_first():
    rows = [
        template_spec("a"),
        template_spec("b"),
        template_spec("c"),
        twin_spec("d", "one"),
        twin_spec("e", "two"),
        good_spec("f"),
    ]
    clusters = em.duplicate_clusters(rows)
    assert [cluster["size"] for cluster in clusters] == [3, 2]
    assert clusters[0]["slugs"] == ["a", "b", "c"]
    assert clusters[1]["slugs"] == ["d", "e"]


def test_duplicate_map_reports_the_cluster_size_per_slug():
    rows = [template_spec("a"), template_spec("b"), good_spec("c")]
    sizes = em.duplicate_map(rows)
    assert sizes == {"a": 2, "b": 2, "c": 1}


def test_bodies_differing_only_by_function_name_still_cluster():
    """The name is the one token guaranteed to differ, so it cannot be evidence."""
    left = Spec(slug="left", title="Count Up", function_signature="def count_up(n: int) -> None:",
                description="Call `count_up(n)` and print the numbers 1 to n, one per line. Stop after n.")
    right = Spec(slug="right", title="Count Down", function_signature="def count_down(n: int) -> None:",
                 description="Call `count_down(n)` and print the numbers 1 to n, one per line. Stop after n.")
    assert len(em.duplicate_clusters([left, right])) == 1
    assert em.duplicate_clusters([left, right], strip_function_name=False) == []


def test_min_size_excludes_singletons():
    rows = [template_spec("a"), template_spec("b")]
    assert em.duplicate_clusters(rows, min_size=3) == []


# ── boilerplate n-grams ───────────────────────────────────────────────


def test_repeated_boilerplate_phrase_is_detected_with_its_count():
    rows = [template_spec(f"p{index}") for index in range(4)] + [good_spec("odd")]
    table = em.boilerplate_phrases(rows, 0.5)
    assert table.total_documents == 5
    counts = {row["phrase"]: row["problems"] for row in table.rows}
    assert counts["the result from the function"] == 4
    assert counts["instead of printing"] == 4
    share = next(row["share"] for row in table.rows if row["phrase"] == "instead of printing")
    assert share == 0.8


def test_boilerplate_ngram_range_is_two_to_five_words_by_default():
    table = em.boilerplate_phrases([template_spec("a"), template_spec("b")], 0.5)
    assert {row["words"] for row in table.rows} <= {2, 3, 4, 5}
    assert em.boilerplate_phrases([template_spec("a")], 0.5, n_max=6).rows != []


def test_boilerplate_phrases_respect_min_share():
    rows = [template_spec(f"p{index}") for index in range(3)] + [good_spec("odd")]
    assert em.boilerplate_phrases(rows, 0.9).rows == []
    assert em.boilerplate_phrases(rows, 0.5).rows != []


def test_boilerplate_rows_are_ranked_by_count():
    body = "Return the result from the function. Twice appears in every row of this table."
    rows = [Spec(slug=f"p{index}", title="P", function_signature="def p(n: int) -> int:", description=body)
            for index in range(4)]
    rows.append(good_spec("odd"))
    counts = [row["problems"] for row in em.boilerplate_phrases(rows, 0.5).rows]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 4


def test_ngrams_do_not_span_line_boundaries():
    """A phrase the generator emits is a phrase it emits in one place."""
    rows = [
        Spec(slug="a", title="A", function_signature="def a() -> None:", description="Inputs:\nOutput:\nRules:"),
        Spec(slug="b", title="B", function_signature="def b() -> None:", description="Inputs:\nOutput:\nRules:"),
    ]
    phrases = {row["phrase"] for row in em.boilerplate_phrases(rows, 0.5, n_min=1, n_max=2).rows}
    assert "output rules" not in phrases
    assert "inputs" in phrases


def test_named_phrase_probe_counts_a_phrase_the_table_would_miss():
    rows = [Spec(slug="a", title="A", function_signature="def a() -> None:", description="Write your code here."),
            Spec(slug="b", title="B", function_signature="def b() -> None:", description="Write your code here.")]
    probes = {row["phrase"]: row["problems"] for row in em.named_phrase_counts(rows, ["your code here"])}
    assert probes == {"your code here": 2}


def test_maximal_phrases_drops_fragments_of_a_longer_phrase():
    rows = [
        {"phrase": "and tests", "problems": 100},
        {"phrase": "and tests handle common edge cases", "problems": 100},
        {"phrase": "and tests handle", "problems": 40},
        {"phrase": "return the result", "problems": 90},
    ]
    kept = {row["phrase"] for row in em.maximal_phrases(rows)}
    assert kept == {"and tests handle common edge cases", "return the result"}


def test_shared_phrases_by_problem_names_what_to_delete():
    rows = [template_spec("a"), template_spec("b"), good_spec("c")]
    shared = em.shared_phrases_by_problem(rows, em.ngram_counts(rows), 0.5)
    assert "the result from the function" in shared["a"]
    assert shared["c"] == []


def test_shared_sentence_audit_counts_bodies_carrying_a_repeated_clause():
    rows = [template_spec("a"), template_spec("b"), good_spec("c")]
    audit = em.shared_sentence_audit(rows)
    assert audit["problems"] == 3
    assert audit["problems_with_a_shared_sentence"] == 2
    assert audit["percent"] == pytest.approx(66.7, abs=0.1)
    top = audit["probes"][0]
    assert top["problems"] == 2
    assert "printing" in top["phrase"]


def test_shared_sentence_audit_ignores_word_order():
    shuffled = Spec(slug="a", title="A", function_signature="def a() -> None:",
                    description="Instead of printing it, return the result from the function.")
    audit = em.shared_sentence_audit([shuffled, shuffled])
    assert audit["problems_with_a_shared_sentence"] == 2


# ── placeholder markers ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("Solve the task: {{ problem }}", "unrendered_template"),
        ("Write {% if x %}something{% endif %}", "unrendered_template"),
        ("Fill in <placeholder> here", "angle_placeholder"),
        ("TODO: write this one properly", "todo"),
        ("The expected value is N/A", "na"),
        ("XXX the second parameter", "xxx"),
        ("lorem ipsum dolor sit amet", "lorem_ipsum"),
    ],
)
def test_each_placeholder_marker_class_is_caught(text, marker):
    assert marker in em.placeholder_markers(text)


def test_a_body_that_is_only_the_title_is_flagged():
    assert "title_restated" in em.placeholder_markers("Sum a List.", "Sum a List")


def test_a_body_shorter_than_its_title_is_flagged():
    assert "shorter_than_title" in em.placeholder_markers("Sum it.", "Sum a List of Numbers")


def test_a_healthy_body_carries_no_markers():
    assert em.placeholder_markers(GOOD_BODY, "Reverse a String") == []


def test_markers_are_deduplicated_and_sorted():
    found = em.placeholder_markers("TODO\nTODO\n{{ x }}", "")
    assert found == ["todo", "unrendered_template"]


def test_comparative_markers_need_a_title_to_compare_against():
    assert em.placeholder_markers("Sum it.", "") == []


# ── scoring ────────────────────────────────────────────────────────────


def test_an_empty_body_scores_zero():
    """Zero, not "no complaints": an absent description is not a good one."""
    empty = Spec(slug="empty", title="Sum a List", function_signature="def total(numbers: list[int]) -> int:", description="")
    result = em.score(empty)
    assert result.total == 0
    assert set(result.failed) == set(em.COMPONENT_WEIGHTS)
    assert result.evidence["chars"] == 0


def test_a_good_description_scores_high():
    result = em.score(good_spec(), em.Library.measured([good_spec(), template_spec()]))
    assert result.total == 100
    assert result.failed == ()


def test_total_is_the_sum_of_its_points_and_stays_in_range():
    library = em.Library.measured([good_spec(), template_spec()])
    for spec in (good_spec(), template_spec(), Spec(slug="x", title="X", function_signature="", description="")):
        result = em.score(spec, library)
        assert result.total == sum(result.points.values())
        assert 0 <= result.total <= 100
        assert set(result.components) == set(em.COMPONENT_WEIGHTS)


def test_not_duplicated_fails_on_its_own():
    rows = [good_spec("a"), good_spec("b"), template_spec("c")]
    library = em.Library.measured(rows)
    result = em.score(rows[0], library)
    assert result.failed == ("not_duplicated",)
    assert result.total == 75
    assert result.evidence["duplicate_size"] == 2


def test_no_placeholder_fails_on_its_own():
    marked = good_spec("a", body=GOOD_BODY.replace(
        "Return a new string that holds the same characters in the opposite order, and leave `text` itself alone.",
        "Return {{ the_case }}.",
    ))
    library = em.Library.measured([marked, template_spec("b")])
    result = em.score(marked, library)
    assert result.failed == ("no_placeholder",)
    assert "unrendered_template" in result.evidence["markers"]


def test_length_in_range_fails_on_its_own_and_uses_the_measured_band():
    tiny = Spec(slug="tiny", title="Reverse a String", function_signature="def reverse_string(text: str) -> str:",
                description="Use `reverse_string(text)` on `text`.")
    library = em.Library.measured([good_spec("a"), good_spec("b"), tiny])
    assert library.band_chars[0] > 100, "the band must come from the long bodies, not the short one"
    result = em.score(tiny, library)
    assert result.failed == ("length_in_range",)
    assert result.evidence["band_chars"] == [round(value, 1) for value in library.band_chars]


def test_the_length_band_is_the_measured_interquartile_range():
    library = em.Library.measured([Spec(slug=str(n), title="t", function_signature="", description="x" * n) for n in range(1, 101)])
    low, high = library.band_chars
    assert low == pytest.approx(25.5, abs=1)
    assert high == pytest.approx(75.5, abs=1)


def test_names_the_subject_fails_when_only_the_signature_names_it():
    body = "Add up the values that arrive and hand the answer back. It returns 0 for an empty list."
    spec = Spec(slug="a", title="Sum a List", function_signature="def total(numbers: list[int]) -> int:",
                description=at_length(body))
    library = em.Library.measured([spec, good_spec("b")])
    result = em.score(spec, library)
    assert result.failed == ("names_the_subject",)
    assert result.evidence["subject_hits"] == ["signature_only:total"]


def test_names_the_subject_accepts_a_parameter_name():
    body = "Add up the values in `numbers` and return the sum as an integer."
    spec = Spec(slug="a", title="Sum a List", function_signature="def total(numbers: list[int]) -> int:",
                description=at_length(body))
    library = em.Library.measured([spec, good_spec("b")])
    result = em.score(spec, library)
    assert result.failed == ()
    assert "parameter:numbers" in result.evidence["subject_hits"]


def test_has_a_concrete_detail_fails_on_a_stated_output_with_no_value():
    """The library's most common sentence names an output and gives nothing."""
    body = "Add up the values that numbers holds, then return the answer to the caller."
    spec = Spec(slug="a", title="Sum a List", function_signature="def total(numbers: list[int]) -> int:",
                description=at_length(body))
    library = em.Library.measured([spec, good_spec("b")])
    result = em.score(spec, library)
    assert result.failed == ("has_a_concrete_detail",)
    assert result.evidence["concrete_detail_kinds"] == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Add up the values and return 15 for the list [1, 2, 3].", "number"),
        ("Call `total(numbers)` on the list that arrives.", "call"),
        ("Return the value of `numbers[0]` for the list.", "literal"),
        ("The word 'hello' is the only thing that matters.", "quoted"),
        ("The function returns 'nohtyP' for the input.", "stated_output"),
    ],
)
def test_concrete_detail_kinds_are_each_detected(text, kind):
    assert kind in em.concrete_details(text)


def test_plain_words_fails_on_a_wall_of_nominalisations():
    wall = "Consider the implementation of the configuration of the initialisation before the evaluation."
    report = em.plain_words_report(wall)
    assert report["nominalisation_run"] == 3
    assert report["passes"] is False


def test_plain_words_accepts_one_nominalisation_in_an_ordinary_sentence():
    assert em.plain_words_report("Use a loop to build the implementation of the list you need.")["passes"] is True


def test_plain_words_fails_on_a_sentence_that_runs_on():
    long_sentence = " ".join(["word"] * 40) + "."
    assert em.plain_words_report(long_sentence)["passes"] is False


def test_plain_words_measures_mean_and_longest_sentence():
    stats = em.sentence_stats("One two three. Four five.")
    assert stats[0] == 2
    assert stats[1] == pytest.approx(2.5)
    assert stats[2] == 3


def test_has_statement_fails_when_the_body_only_restates_the_title():
    spec = Spec(slug="a", title="Sum a List", function_signature="def total(numbers: list[int]) -> int:",
                description="Sum a List. Just numbers.")
    library = em.Library.measured([spec, good_spec("b")])
    result = em.score(spec, library)
    assert "has_statement" in result.failed
    assert result.evidence["statement_remainder_words"] < em.MIN_STATEMENT_WORDS


def test_scoring_without_a_library_records_that_the_band_is_self_measured():
    result = em.score(good_spec())
    assert result.evidence["band_source"] == "self"
    assert result.total == 100


def test_scores_can_be_taken_for_a_whole_list_at_once():
    rows = [good_spec("a"), template_spec("b"), template_spec("c"),
            Spec(slug="d", title="D", function_signature="", description="")]
    scores = em.score_all(rows)
    assert [item.slug for item in scores] == ["a", "b", "c", "d"]
    assert scores[3].total == 0
    assert scores[1].total < scores[0].total


# ── report ─────────────────────────────────────────────────────────────


def test_report_returns_aggregates_and_a_row_per_problem():
    rows = [good_spec("a"), template_spec("b"), template_spec("c")]
    result = em.report(rows, source={"db": "memory"}, verification={"status": "absent"})
    assert result["source"]["db"] == "memory"
    assert result["library"]["problems"] == 3
    assert result["duplicates"]["clusters"] == 1
    assert result["duplicates"]["problems_in_clusters"] == 2
    assert result["duplicates"]["largest"][0]["slugs"] == ["b", "c"]
    assert result["boilerplate"]["total_documents"] == 3
    assert result["scores"]["count"] == 3
    assert {row["slug"] for row in result["problems"]} == {"a", "b", "c"}
    assert result["problems"] == sorted(result["problems"], key=lambda row: (row["total"], row["slug"]))


def test_report_records_the_components_and_their_weights():
    result = em.report([good_spec("a")])
    names = [component["name"] for component in result["scoring"]["components"]]
    assert names == list(em.COMPONENT_WEIGHTS)
    assert sum(component["weight"] for component in result["scoring"]["components"]) == 100
    assert set(result["scores"]["component_failures"]) == set(em.COMPONENT_WEIGHTS)


def test_report_splits_the_pass_count_by_duplication():
    """The split is the honest reading of the headline number.

    A shared description still clears 70 when everything else passes, because
    the heaviest component is 25 points and the remaining six sum to 75. A
    reader who only saw "75% pass" would call reused text good.
    """
    rows = [
        good_spec("a"),
        good_spec("b", body=GOOD_BODY.replace("Python", "Ruby")),
        template_spec("c"),
        template_spec("d"),
    ]
    result = em.report(rows)
    split = result["scores"]["passing_by_duplication"]
    assert split["unique_description"] == {"problems": 2, "passing": 2}
    assert split["shared_description"]["problems"] == 2
    assert split["shared_description"]["passing"] == 2
    totals = {row["slug"]: row["total"] for row in result["problems"]}
    assert totals["c"] == totals["a"] - em.COMPONENT_WEIGHTS["not_duplicated"]


def test_report_survives_an_empty_library():
    result = em.report([])
    assert result["library"]["problems"] == 0
    assert result["duplicates"]["clusters"] == 0
    assert result["scores"]["count"] == 0
    assert result["problems"] == []


def test_report_is_json_serialisable():
    json.dumps(em.report([good_spec("a"), template_spec("b")]))


# ── rewrite manifest ───────────────────────────────────────────────────


def test_rewrite_manifest_returns_the_worst_first_with_the_named_keys():
    rows = [good_spec("a"), template_spec("b"), template_spec("c"),
            Spec(slug="d", title="D", function_signature="def d() -> None:", description="")]
    manifest = em.rewrite_manifest(rows, em.score_all(rows), 2)
    assert [entry["slug"] for entry in manifest] == ["d", "b"]
    assert set(manifest[0]) >= {
        "slug",
        "title",
        "function_signature",
        "current_description",
        "score",
        "failed_components",
    }
    assert manifest[0]["current_description"] == ""
    assert "has_statement" in manifest[0]["failed_components"]


def test_rewrite_manifest_orders_ties_by_slug():
    rows = [Spec(slug=slug, title=slug, function_signature="", description="") for slug in ("c", "a", "b")]
    manifest = em.rewrite_manifest(rows, em.score_all(rows), 3)
    assert [entry["slug"] for entry in manifest] == ["a", "b", "c"]


def test_rewrite_manifest_can_carry_the_shared_phrases():
    rows = [template_spec("a"), template_spec("b"), good_spec("c")]
    shared = em.shared_phrases_by_problem(rows, em.ngram_counts(rows), 0.5)
    manifest = em.rewrite_manifest(rows, em.score_all(rows), 1, shared_phrases=shared)
    assert "the result from the function" in manifest[0]["shared_phrases"]


def test_rewrite_manifest_of_zero_is_empty():
    rows = [good_spec("a")]
    assert em.rewrite_manifest(rows, em.score_all(rows), 0) == []


# ── the CLI ────────────────────────────────────────────────────────────


def _probe_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE coding_problems (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            title TEXT,
            description TEXT,
            function_signature TEXT,
            is_active INTEGER
        );
        """
    )
    rows = [
        (1, "keep-a", "Reverse a String", GOOD_BODY, "def reverse_string(text: str) -> str:", 1),
        (2, "keep-b", "Count Up", at_length("Count from 1 to n using a while loop. Print each number."),
         "def count_up(n: int) -> None:", 1),
        (3, "drop-a", "Sum a List", "Sum a List\n\nReturn the result from the function instead of printing it.",
         "def total(numbers: list[int]) -> int:", 1),
        (4, "inactive", "Old One", "Old body that should never be measured at all.", "def old() -> None:", 0),
    ]
    conn.executemany("INSERT INTO coding_problems VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_cli_measures_the_verified_subset_and_writes_both_files(tmp_path, capsys):
    db = tmp_path / "probe.db"
    _probe_db(db)
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({"totals": {"shippable": 2}, "quarantined": [{"slug": "drop-a"}]}))
    out = tmp_path / "report.json"
    manifest_path = tmp_path / "manifest.json"

    code = em.main([
        "--db", str(db),
        "--verified", str(verification),
        "--out", str(out),
        "--rewrite-manifest", "1",
        "--rewrite-out", str(manifest_path),
        "--quiet",
    ])
    assert code == 0
    result = json.loads(out.read_text())
    assert result["source"]["table"] == "coding_problems"
    assert result["source"]["active_filter"] is True
    assert {row["slug"] for row in result["problems"]} == {"keep-a", "keep-b"}
    assert result["verification_subset"]["verified_problems"] == 2
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest) == 1
    assert manifest[0]["slug"] in {"keep-a", "keep-b"}
    assert "rewrite manifest" in capsys.readouterr().out


def test_cli_falls_back_to_every_active_row_when_verification_is_absent(tmp_path):
    db = tmp_path / "probe.db"
    _probe_db(db)
    out = tmp_path / "report.json"
    code = em.main([
        "--db", str(db),
        "--verified", str(tmp_path / "nope.json"),
        "--out", str(out),
        "--quiet",
    ])
    assert code == 0
    result = json.loads(out.read_text())
    assert result["library"]["problems"] == 3
    assert result["verification_subset"]["verified_problems"] is None
    assert "absent" in result["verification_subset"]["status"]


def test_cli_reports_a_missing_database_instead_of_raising(tmp_path, capsys):
    code = em.main(["--db", str(tmp_path / "absent.db")])
    assert code == 2
    assert "no such database" in capsys.readouterr().err
