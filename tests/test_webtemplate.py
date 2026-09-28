"""Tests for the template renderer.

Two properties matter more than feature coverage:

1. **Escaping is default and cannot be routed around by data.** A value in the
   library must never be able to emit markup.
2. **Template syntax never reaches a student.** The previous renderer passed
   unrecognised tags straight through, so `{{ visible_cases|length }}` and
   `{% if total != 1 %}` were rendered as literal text into the page. A student
   should never see the template language, and silent passthrough is how that
   happens.
"""

from __future__ import annotations

import pytest

from ducky.webtemplate import (
    FILTERS,
    TemplateError,
    render,
    render_fragment,
    validate_source,
)


class TestEscaping:
    def test_values_are_escaped(self):
        out = render_fragment("<p>{{ x }}</p>", {"x": "<script>alert(1)</script>"})
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_quotes_are_escaped_for_attribute_contexts(self):
        """data-starter="..." must not be breakable out of."""
        out = render_fragment('<b data-starter="{{ x }}">', {"x": '" onload="evil()'})
        assert '" onload="evil()' not in out
        assert "&quot;" in out

    def test_single_quotes_escaped(self):
        out = render_fragment("<p>{{ x }}</p>", {"x": "it's"})
        assert "&#x27;" in out or "&#39;" in out

    def test_lists_and_dicts_render_as_json_then_escaped(self):
        out = render_fragment("{{ x }}", {"x": {"a": "<b>"}})
        assert "<b>" not in out
        assert "\\u003cb\\u003e" in out or "&lt;b&gt;" in out

    def test_bools_render_as_json_literals(self):
        assert render_fragment("{{ x }}", {"x": True}) == "true"
        assert render_fragment("{{ x }}", {"x": False}) == "false"

    def test_none_renders_empty(self):
        assert render_fragment("[{{ x }}]", {"x": None}) == "[]"

    def test_missing_name_renders_empty_not_an_error(self):
        assert render_fragment("[{{ nope }}]", {}) == "[]"

    def test_dotted_lookup(self):
        assert render_fragment("{{ a.b.c }}", {"a": {"b": {"c": "deep"}}}) == "deep"

    def test_dotted_lookup_through_a_missing_link(self):
        assert render_fragment("[{{ a.b.c }}]", {"a": None}) == "[]"


class TestNoTemplateSourceReachesStudents:
    def test_unknown_filter_is_rejected(self):
        with pytest.raises(TemplateError, match="unknown filter"):
            render_fragment("{{ x|nope }}", {"x": "v"})

    def test_unknown_tag_is_rejected(self):
        with pytest.raises(TemplateError, match="unknown template tag"):
            validate_source("{% now x %}y")

    def test_an_unclosed_block_is_rejected(self):
        """The literal-passthrough bug: this used to reach the student's page.

        `if` is a known tag, so a name check passes it; the block regex then
        fails to match and the text survives verbatim into the page.
        """
        with pytest.raises(TemplateError, match="endif"):
            validate_source("{% if x %}y")

    def test_an_unopened_closer_is_rejected(self):
        with pytest.raises(TemplateError, match="endif"):
            validate_source("{% endif %}")

    def test_an_unclosed_for_is_rejected(self):
        with pytest.raises(TemplateError, match="endfor"):
            validate_source("{% for x in xs %}y")

    def test_a_balanced_template_validates(self):
        validate_source("{% if a %}{{ b|length }}{% endif %}{% for c in d %}{{ c }}{% endfor %}")

    def test_nested_blocks_validate(self):
        validate_source("{% for r in rs %}{% if r %}x{% endif %}{% endfor %}")

    def test_data_containing_braces_is_not_mistaken_for_syntax(self):
        """A test case expecting the literal string "{{" is legitimate content."""
        out = render_fragment("{{ x }}", {"x": "{{ not a tag"})
        assert out == "{{ not a tag"

    def test_length_filter_renders(self):
        assert render_fragment("{{ xs|length }}", {"xs": [1, 2, 3]}) == "3"

    def test_length_filter_on_a_string(self):
        assert render_fragment("{{ s|length }}", {"s": "abcd"}) == "4"

    def test_lower_and_upper(self):
        assert render_fragment("{{ s|lower }}", {"s": "MiXeD"}) == "mixed"
        assert render_fragment("{{ s|upper }}", {"s": "MiXeD"}) == "MIXED"

    def test_default_filter_substitutes_for_empty(self):
        assert render_fragment("{{ x|default('none') }}", {"x": ""}) == "none"
        assert render_fragment("{{ x|default('none') }}", {"x": "v"}) == "v"

    def test_unknown_filter_names_the_filter(self):
        with pytest.raises(TemplateError, match="unknown filter"):
            render_fragment("{{ x|notafilter }}", {"x": "v"})

    def test_filters_chain(self):
        assert render_fragment("{{ s|upper|length }}", {"s": "abc"}) == "3"

    def test_every_advertised_filter_exists(self):
        for name in FILTERS:
            assert callable(FILTERS[name])


class TestConditionals:
    def test_bare_truthiness(self):
        assert render_fragment("{% if x %}yes{% endif %}", {"x": [1]}) == "yes"
        assert render_fragment("{% if x %}yes{% endif %}", {"x": []}) == ""
        assert render_fragment("{% if x %}yes{% endif %}", {"x": 0}) == ""

    def test_inequality(self):
        assert render_fragment("{% if n != 1 %}s{% endif %}", {"n": 2}) == "s"
        assert render_fragment("{% if n != 1 %}s{% endif %}", {"n": 1}) == ""

    def test_equality(self):
        assert render_fragment("{% if n == 1 %}one{% endif %}", {"n": 1}) == "one"

    def test_ordered_comparisons(self):
        assert render_fragment("{% if n > 3 %}big{% endif %}", {"n": 4}) == "big"
        assert render_fragment("{% if n < 3 %}small{% endif %}", {"n": 2}) == "small"
        assert render_fragment("{% if n >= 3 %}ge{% endif %}", {"n": 3}) == "ge"
        assert render_fragment("{% if n <= 3 %}le{% endif %}", {"n": 3}) == "le"

    def test_string_comparison(self):
        assert render_fragment("{% if s == 'ok' %}y{% endif %}", {"s": "ok"}) == "y"

    def test_incomparable_types_do_not_raise(self):
        assert render_fragment("{% if n > 'x' %}y{% endif %}", {"n": 1}) == ""


class TestLoops:
    def test_iterates(self):
        out = render_fragment("{% for x in xs %}[{{ x }}]{% endfor %}", {"xs": [1, 2]})
        assert out == "[1][2]"

    def test_empty_sequence(self):
        assert render_fragment("{% for x in xs %}x{% endfor %}", {"xs": []}) == ""

    def test_missing_sequence(self):
        assert render_fragment("{% for x in nope %}x{% endfor %}", {}) == ""

    def test_dotted_sequence(self):
        out = render_fragment("{% for t in p.tags %}{{ t }},{% endfor %}",
                              {"p": {"tags": ["a", "b"]}})
        assert out == "a,b,"

    def test_dict_sequence_yields_pairs(self):
        out = render_fragment("{% for kv in d %}{{ kv }}{% endfor %}", {"d": {"a": 1}})
        assert "a" in out


class TestDoubleSubstitutionIsImpossible:
    """The escape that was verified exploitable.

    The loop body was rendered and escaped by a nested call, then the outer
    variable pass rescanned the inlined result. A value containing `{{{ x }}}`
    was matched by the triple-brace branch, which does not escape -- so a tag in
    the content database was enough to inject raw markup.
    """

    def test_triple_braces_in_loop_data_cannot_inject(self):
        out = render_fragment(
            "{% for tag in p.tags %}<i>{{ tag }}</i>{% endfor %}",
            {"p": {"title": "<script>alert(1)</script>",
                   "tags": ["{{{ p.title }}}"]}},
        )
        assert "<script>" not in out

    def test_triple_braces_outside_a_loop_stay_literal(self):
        out = render_fragment("{{ x }}", {"x": "{{{ p.title }}}"})
        assert "{{{ p.title }}}" in out
        # Braces are not HTML-special, so the value survives as written.

    def test_whole_dict_injection_is_neutralised(self):
        out = render_fragment(
            "{% for tag in p.tags %}{{ tag }}{% endfor %}",
            {"p": {"title": "<b>x</b>", "tags": ["{{{ p }}}"]}},
        )
        assert "<b>x</b>" not in out

    def test_nested_loops_are_safe(self):
        out = render_fragment(
            "{% for row in rows %}{% for c in row %}{{ c }}{% endfor %}|{% endfor %}",
            {"rows": [["a", "{{{ x }}}"], ["b"]]},
        )
        assert "<script" not in out


class TestInclude:
    def test_traversal_is_refused(self):
        with pytest.raises(TemplateError, match="escapes the template root"):
            render("../../../etc/passwd")

    def test_absolute_path_is_refused(self):
        with pytest.raises(TemplateError):
            render("/etc/passwd")

    def test_missing_template_is_a_filenotfound(self):
        with pytest.raises(FileNotFoundError):
            render("no_such_template.html")


class TestLayout:
    def test_named_template_renders(self, tmp_path, monkeypatch):
        import ducky.webtemplate as wt

        root = tmp_path / "templates"
        root.mkdir()
        (root / "page.html").write_text("<p>{{ x }}</p>", encoding="utf-8")
        (root / "shell.html").write_text("<main>{{{ body }}}</main>", encoding="utf-8")
        monkeypatch.setattr(wt, "TEMPLATE_ROOT", root)

        out = wt.render("page.html", {"x": "hi", "layout": "shell.html"})
        assert out == "<main><p>hi</p></main>"

    def test_layout_none_renders_standalone(self, tmp_path, monkeypatch):
        import ducky.webtemplate as wt

        root = tmp_path / "templates"
        root.mkdir()
        (root / "solo.html").write_text("<!DOCTYPE html><body>{{ x }}</body>",
                                        encoding="utf-8")
        monkeypatch.setattr(wt, "TEMPLATE_ROOT", root)

        out = wt.render("solo.html", {"x": "hi", "layout": None})
        assert out.startswith("<!DOCTYPE html>")
        assert "shell" not in out

    def test_rejects_unknown_tags_in_a_real_template(self, tmp_path, monkeypatch):
        import ducky.webtemplate as wt

        root = tmp_path / "templates"
        root.mkdir()
        (root / "bad.html").write_text("{{ xs|nosuchfilter }}", encoding="utf-8")
        monkeypatch.setattr(wt, "TEMPLATE_ROOT", root)

        with pytest.raises(TemplateError):
            wt.render("bad.html", {"xs": [1]})


class TestShippedTemplates:
    """The real templates must validate, and must render without leaking syntax.

    `{% else %}` was not implemented, so every if/else in the shipped templates
    rendered its else-tag as literal text. The source validator is what caught
    it, so it runs against the real files rather than only against fixtures.
    """

    @pytest.mark.parametrize(
        "name", ["base.html", "index.html", "problems.html", "problem.html"]
    )
    def test_validates(self, name):
        import ducky.webtemplate as wt

        validate_source((wt.TEMPLATE_ROOT / name).read_text(encoding="utf-8"), name)

    def test_every_template_in_the_directory_validates(self):
        import ducky.webtemplate as wt

        for path in sorted(wt.TEMPLATE_ROOT.glob("*.html")):
            validate_source(path.read_text(encoding="utf-8"), path.name)

    def test_problem_page_has_no_leftover_syntax(self):
        from ducky.content import parse_test_case

        out = render("problem.html", {
            "layout": None,
            "problem": {
                "slug": "hello", "title": "Say hello",
                "description": "Return a greeting.", "difficulty": "Beginner",
                "difficulty_percent": 10, "topic_tier": 1,
                "tags": ["strings", "greeting"],
                "learning_outcomes": ["Return a formatted string"],
                "time_complexity": "O(n)", "space_complexity": "O(1)",
                "optimal_approach": "Use an f-string.",
                "function_name": "greet", "function_signature": "def greet(name):",
                "parameters": ["name"],
                "starter_code": "def greet(name):\n    pass\n",
            },
            "visible_cases": [parse_test_case(0, '{"args": {"name": "Ada"}}', '"Hello, Ada!"')],
            "hidden_count": 1,
        })
        for leaked in ("{{", "{%", "}}", "%}"):
            assert leaked not in out, f"template syntax {leaked!r} reached the page"
        assert "Say hello" in out
        assert "Hello, Ada!" in out

    def test_problem_list_page_has_no_leftover_syntax(self):
        out = render("problems.html", {
            "title": "Problems", "page": "practice",
            "total": 2, "problems": [
                {"slug": "a", "title": "Alpha", "difficulty": "Beginner", "tags": []},
                {"slug": "b", "title": "Beta", "difficulty": "Advanced", "tags": []},
            ],
            "truncated": False,
            "filters": {"topic": "", "difficulty": "", "q": ""},
            "topics": ["strings"], "difficulties": ["Beginner", "Advanced"],
        })
        assert "{{" not in out and "{%" not in out
        assert "2 problems" in out
        assert "Alpha" in out

    def test_singular_problem_count(self):
        out = render("problems.html", {
            "title": "Problems", "page": "practice", "total": 1,
            "problems": [{"slug": "a", "title": "Alpha", "difficulty": "Beginner", "tags": []}],
            "truncated": False,
            "filters": {"topic": "", "difficulty": "", "q": ""},
            "topics": [], "difficulties": [],
        })
        assert "1 problem<" in out or "1 problem\n" in out or "1 problem " in out
        assert "{{" not in out

    def test_empty_problem_list_shows_the_empty_state(self):
        out = render("problems.html", {
            "title": "Problems", "page": "practice", "total": 0, "problems": [],
            "truncated": False,
            "filters": {"topic": "", "difficulty": "", "q": "zzz"},
            "topics": [], "difficulties": [],
        })
        assert "No problems match" in out

    def test_index_page_renders(self):
        out = render("index.html", {"title": "Home", "page": "home"})
        assert "Simple Python Practice" in out
        assert "{{" not in out and "{%" not in out
