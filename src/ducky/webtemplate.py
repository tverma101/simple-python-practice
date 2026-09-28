"""A very small template renderer.

Not a template engine. The pages need variable substitution, conditionals, one
loop, and one include -- and that is all they need. Pulling in Jinja would add a
runtime dependency to a project whose zero-dependency property is load-bearing:
it is what lets the student build be a single ``pip install`` of one wheel.

Supports ``{{ name }}`` (HTML-escaped), ``{{{ name }}}`` (pre-rendered),
``{{ name|filter }}``, ``{% if %}``/``{% else %}``/``{% endif %}``,
``{% for x in y %}``/``{% endfor %}``, and ``{% include "name" %}``.

Why this is a recursive-descent pass and not a pile of regexes
-------------------------------------------------------------

It was a pile of regexes, and every property that mattered was broken by it:

* An ``{% if p.on %}`` *inside* ``{% for p in ... %}`` was evaluated before the
  loop bound ``p``, so it silently took the else-branch and every item rendered
  identically.
* An ``{% if %}`` whose body was a ``{% for %}`` could never match, because a
  block's body was only allowed to contain resolved text. Processing ifs before
  fors deadlocked; processing fors before ifs deadlocked the other way.
* Loop output was inlined and then rescanned by the variable pass, so a value
  containing ``{{{ x }}}`` was re-matched by the triple-brace branch, which does
  not escape. A tag in the content database was enough to inject raw markup.
* Unrecognised tags passed through verbatim, so ``{{ xs|length }}`` and
  ``{% else %}`` were rendered as literal text *into a student's browser*.

Parsing the template into a tree once and rendering it recursively fixes all four
by construction: a block's body is parsed as part of that block, so nesting is
structural rather than a regex's problem, and a substituted value is never
re-scanned because nothing scans the output.

The escaping default is the load-bearing part. A template that requires opting
*out* of escaping is a template that eventually forgets.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"

__all__ = ["render", "render_fragment", "validate_source", "TEMPLATE_ROOT", "TemplateError", "FILTERS"]


class TemplateError(Exception):
    pass


# ── Filters ─────────────────────────────────────────────────────────
#
# Deliberately tiny. A filter that does not exist must be a loud error, not
# silently-dropped syntax.

FILTERS: dict[str, Any] = {
    "length": lambda v: len(v) if hasattr(v, "__len__") else 0,
    "count": lambda v: len(v) if hasattr(v, "__len__") else 0,
    "lower": lambda v: str(v).lower(),
    "upper": lambda v: str(v).upper(),
    "title": lambda v: str(v).title(),
    "trim": lambda v: str(v).strip(),
    "first": lambda v: (list(v)[0] if len(list(v)) else ""),
    "last": lambda v: (list(v)[-1] if len(list(v)) else ""),
    "join": lambda v, sep=" ": sep.join(str(x) for x in v),
    "default": lambda v, other="": other if v in (None, "", [], {}) else v,
    "int": lambda v: int(v) if str(v).lstrip("-").isdigit() else 0,
    "abs": lambda v: abs(v) if isinstance(v, (int, float)) else v,
}

_COMPARATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


def _coerce(text: str) -> Any:
    """Turn a literal in a template expression into a Python value."""
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
        return stripped[1:-1]
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped


def _lookup(context: dict[str, Any], dotted: str) -> Any:
    value: Any = context
    for part in dotted.split("."):
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = getattr(value, part, None)
    return value


def _truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (list, tuple, dict, set, str)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _apply_filters(value: Any, spec: str) -> Any:
    for part in re.finditer(r"\|\s*([a-zA-Z_]\w*)(?:\(([^()]*)\))?", spec):
        name, raw = part.group(1), part.group(2)
        func = FILTERS.get(name)
        if func is None:
            raise TemplateError(f"unknown filter {name!r}; add it to FILTERS or stop using it")
        value = func(value) if raw is None else func(value, *[_coerce(a) for a in raw.split(",")])
    return value


# ── Parsing ─────────────────────────────────────────────────────────


class _Text:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Var:
    __slots__ = ("name", "filters", "raw")

    def __init__(self, name: str, filters: str, raw: bool) -> None:
        self.name, self.filters, self.raw = name, filters, raw


class _If:
    __slots__ = ("expr", "then", "otherwise")

    def __init__(self, expr: _Expr, then: list, otherwise: list) -> None:
        self.expr, self.then, self.otherwise = expr, then, otherwise


class _For:
    __slots__ = ("name", "iterable", "body")

    def __init__(self, name: str, iterable: str, body: list) -> None:
        self.name, self.iterable, self.body = name, iterable, body


class _Include:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _Expr:
    """A condition: ``name``, or ``name <op> literal``."""

    __slots__ = ("name", "op", "literal")

    def __init__(self, name: str, op: str = "", literal: Any = None) -> None:
        self.name, self.op, self.literal = name, op, literal

    def evaluate(self, context: dict[str, Any]) -> bool:
        left = _lookup(context, self.name)
        if not self.op:
            return _truthy(left)
        try:
            return bool(_COMPARATORS[self.op](left, self.literal))
        except TypeError:
            return False


_TOKEN = re.compile(
    r"\{\{\{\s*(?P<raw_name>[a-zA-Z_][\w.]*)\s*\}\}\}"
    r"|\{\{\s*(?P<name>[a-zA-Z_][\w.]*)\s*(?P<filters>(?:\|[a-zA-Z_]\w*(?:\([^()]*\))?)*)\s*\}\}"
    r"|\{%\s*(?P<tag>\w+)(?P<body>[^%]*?)%\}",
    re.S,
)
_NAME = r"[a-zA-Z_][\w.]*"
_IF_BODY = re.compile(rf"\s*({_NAME})\s*(?:(==|!=|>=|<=|>|<)\s*(.+?))?\s*$", re.S)
_FOR_BODY = re.compile(rf"\s*(\w+)\s+in\s+({_NAME})\s*$")
_INCLUDE_BODY = re.compile(r'\s*"([^"]+)"\s*$')


def _parse(source: str, pos: int, stop: tuple[str, ...], name: str) -> tuple[list, int]:
    """Parse nodes until one of ``stop`` tags (or end of input) is reached.

    Returns the node list and the position of the terminating tag, or -1. The
    terminator is *not* consumed, so the caller can check which one it was.
    """
    nodes: list = []
    # Start from the caller's position. Initialising to 0 instead makes every
    # recursive call re-find the same opening tag, which is an infinite loop
    # rather than an error.
    cursor = pos
    while True:
        match = _TOKEN.search(source, cursor)
        if match is None:
            tail = source[cursor:]
            if tail:
                nodes.append(_Text(tail))
            return nodes, -1
        if match.start() > cursor:
            nodes.append(_Text(source[cursor:match.start()]))
        if match.group("tag") is None:
            if match.group("raw_name") is not None:
                nodes.append(_Var(match.group("raw_name"), "", True))
            else:
                nodes.append(_Var(match.group("name"), match.group("filters") or "", False))
            cursor = match.end()
            continue

        tag = match.group("tag")
        if tag in stop:
            return nodes, match.start()
        if tag == "if":
            expr_match = _IF_BODY.match(match.group("body") or "")
            if expr_match is None:
                raise TemplateError(f"malformed if condition: {{% if {match.group('body')} %}}")
            expr = _Expr(
                expr_match.group(1),
                expr_match.group(2) or "",
                _coerce(expr_match.group(3)) if expr_match.group(2) else None,
            )
            then_nodes, end = _parse(source, match.end(), ("else", "endif"), name)
            if end < 0:
                raise TemplateError("{% if %} with no {% endif %}")
            otherwise: list = []
            closer = _TOKEN.match(source, end)
            if closer and closer.group("tag") == "else":
                otherwise, end2 = _parse(source, closer.end(), ("endif",), name)
                if end2 < 0:
                    raise TemplateError("{% if %} with no {% endif %}")
                nodes.append(_If(expr, then_nodes, otherwise))
                cursor = _TOKEN.match(source, end2).end()
            else:
                nodes.append(_If(expr, then_nodes, otherwise))
                cursor = _TOKEN.match(source, end).end()
            continue
        if tag == "for":
            for_match = _FOR_BODY.match(match.group("body") or "")
            if for_match is None:
                raise TemplateError(f"malformed for: {{% for {match.group('body')} %}}")
            body_nodes, end = _parse(source, match.end(), ("endfor",), name)
            if end < 0:
                raise TemplateError("{% for %} with no {% endfor %}")
            nodes.append(_For(for_match.group(1), for_match.group(2), body_nodes))
            cursor = _TOKEN.match(source, end).end()
            continue
        if tag == "include":
            include_match = _INCLUDE_BODY.match(match.group("body") or "")
            if include_match is None:
                raise TemplateError('malformed include: use {% include "name.html" %}')
            nodes.append(_Include(include_match.group(1)))
            cursor = match.end()
            continue
        if tag in ("comment",):
            body_nodes, end = _parse(source, match.end(), ("endcomment",), name)
            cursor = match.end() if end < 0 else _TOKEN.match(source, end).end()
            continue
        raise TemplateError(
            f"unknown template tag {{% {tag} %}} in {name or 'template'}"
        )


# ── Rendering ───────────────────────────────────────────────────────


def _render(nodes: list, context: dict[str, Any], *, name: str = "") -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, _Text):
            out.append(node.text)
        elif isinstance(node, _Var):
            value = _apply_filters(_lookup(context, node.name), node.filters)
            if node.raw:
                # Triple braces: the caller asserts this is already safe. The
                # only current use is a body this module rendered, whose own
                # substitutions were escaped.
                out.append("" if value is None else str(value))
            elif value is None:
                out.append("")
            elif isinstance(value, bool):
                out.append("true" if value else "false")
            elif isinstance(value, (list, tuple, dict)):
                import json

                out.append(html.escape(json.dumps(value, default=str), quote=True))
            else:
                out.append(html.escape(str(value), quote=True))
        elif isinstance(node, _If):
            branch = node.then if node.expr.evaluate(context) else node.otherwise
            out.append(_render(branch, context, name=name))
        elif isinstance(node, _For):
            items = _lookup(context, node.iterable) or []
            if isinstance(items, dict):
                items = list(items.items())
            if not isinstance(items, (list, tuple)):
                continue
            for item in items:
                scoped = dict(context)
                scoped[node.name] = item
                out.append(_render(node.body, scoped, name=name))
        elif isinstance(node, _Include):
            out.append(render(node.name, context, name=name))
    return "".join(out)


def render_fragment(source: str, context: dict[str, Any] | None = None, *, name: str = "") -> str:
    """Parse and render a template source string."""
    nodes, end = _parse(source, 0, (), name)
    if end >= 0:
        raise TemplateError(f"unexpected block terminator in {name or 'template'}")
    return _render(nodes, dict(context or {}), name=name)


# ── Validation ──────────────────────────────────────────────────────


def validate_source(source: str, name: str = "") -> None:
    """Parse the template, rejecting anything this renderer cannot handle.

    Validating by parsing rather than by scanning the output is deliberate: a
    test case whose expected value is literally ``{{`` is legitimate data, so an
    output scan would reject correct content. A parse either succeeds or raises,
    which is exactly the property wanted -- an unimplemented construct must be a
    loud error, never literal text in a student's browser.
    """
    try:
        render_fragment(source, {}, name=name)
    except TemplateError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise TemplateError(f"could not parse {name or 'template'}: {exc}") from exc


def _read(name: str) -> str:
    target = (TEMPLATE_ROOT / name).resolve()
    try:
        target.relative_to(TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise TemplateError(f"include escapes the template root: {name!r}") from exc
    if not target.is_file():
        raise FileNotFoundError(f"no template named {name!r}")
    return target.read_text(encoding="utf-8")


def render(name: str, context: dict[str, Any] | None = None, **extra: Any) -> str:
    """Render a named template, wrapping it in the base layout by default.

    Set ``layout`` to ``None`` for a standalone page -- needed for the problem
    surface, which is a full-viewport two-pane layout and must not inherit the
    standard top bar.
    """
    ctx: dict[str, Any] = dict(context or {})
    ctx.update(extra)
    inner = render_fragment(_read(name), ctx, name=name)
    layout = ctx.get("layout", "base.html")
    if not layout:
        return inner
    shell = dict(ctx)
    shell["body"] = inner
    return render_fragment(_read(layout), shell, name=layout)
