"""Jinja filters that turn user-supplied text into *safe* Python source.

The important one is :func:`embed_sql`: it writes the approved SQL into the generated step
function as an f-string, converting ``${var}`` placeholders into ``{var}`` (a local the step reads from
the YAML) and doubling every literal brace. :func:`verify_embedded_sql` then proves — without executing anything — that
the emitted expression evaluates back to exactly the approved SQL.
"""

from __future__ import annotations

import ast
import json
import textwrap
from collections.abc import Mapping

import yaml

from ..models.rules import VAR_TOKEN_RE


class EmbedError(ValueError):
    pass


def _reindent(sql: str, indent: int) -> str:
    """Dedent the SQL and re-indent it for readability, unless it contains triple-quoted
    SQL strings (whose content would change if lines moved)."""
    sql = sql.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    if "'''" in sql or '"""' in sql:
        return sql.strip("\n")
    body = textwrap.dedent(sql).strip("\n")
    pad = " " * indent
    return "\n".join((pad + line) if line.strip() else "" for line in body.split("\n"))


def expected_embedded_value(sql: str, variables: Mapping[str, str], indent: int) -> str:
    """The exact string the embedded expression must evaluate to (with variables filled in)."""
    body = _reindent(sql, indent)
    filled = VAR_TOKEN_RE.sub(lambda m: str(variables[m.group(1)]), body)
    return "\n" + filled + "\n" + " " * max(indent - 4, 0)


def _ref(name: str, var_object: str | None) -> str:
    return f"{var_object}.{name}" if var_object else name


def embed_sql(sql: str, indent: int = 8, var_object: str | None = None) -> str:
    """Python expression source for ``sql`` (with ${var} → {var}) placed at ``indent``.

    The literal is an f-string when the SQL uses variables (raw only when the SQL contains backslashes, so
    regexes keep working). The SQL must not end with a quote of the kind used to close the literal."""
    body = _reindent(sql, indent)
    has_vars = bool(VAR_TOKEN_RE.search(body))
    closing_pad = " " * max(indent - 4, 0)
    raw = "r" if "\\" in body else ""
    for quote in ('"""', "'''"):
        if quote in body:
            continue
        if has_vars:
            parts = []
            pos = 0
            for m in VAR_TOKEN_RE.finditer(body):
                parts.append(body[pos : m.start()].replace("{", "{{").replace("}", "}}"))
                parts.append("{" + _ref(m.group(1), var_object) + "}")
                pos = m.end()
            parts.append(body[pos:].replace("{", "{{").replace("}", "}}"))
            content = "".join(parts)
            prefix = raw + "f"
        else:
            content = body
            prefix = raw
        return f"{prefix}{quote}\n{content}\n{closing_pad}{quote}"
    # Both triple-quote styles occur inside the SQL: fall back to explicit concatenation.
    return _concat_fallback(sql, indent, var_object)


def _concat_fallback(sql: str, indent: int, var_object: str | None) -> str:
    body = "\n" + _reindent(sql, indent) + "\n" + " " * max(indent - 4, 0)
    pieces: list[str] = []
    pos = 0
    for m in VAR_TOKEN_RE.finditer(body):
        if m.start() > pos:
            pieces.append(repr(body[pos : m.start()]))
        pieces.append(_ref(m.group(1), var_object))
        pos = m.end()
    if pos < len(body):
        pieces.append(repr(body[pos:]))
    pad = " " * indent
    lines = [f"{pad}{pieces[0]}"] + [f"{pad}+ {p}" for p in pieces[1:]]
    return "(\n" + "\n".join(lines) + "\n" + " " * max(indent - 4, 0) + ")"


def verify_embedded_sql(
    expr_src: str, sql: str, variables: Mapping[str, str], indent: int, var_object: str | None = None
) -> None:
    """Statically evaluate ``expr_src`` and compare it with the approved SQL. Raises EmbedError."""
    try:
        tree = ast.parse(expr_src.strip(), mode="eval")
    except SyntaxError as exc:
        raise EmbedError(f"embedded SQL is not valid Python: {exc}") from exc
    value = _static_eval(tree.body, variables, var_object)
    expected = expected_embedded_value(sql, variables, indent)
    if value != expected:
        raise EmbedError("embedded SQL does not round-trip to the approved SQL")


def _static_eval(node: ast.AST, variables: Mapping[str, str], var_object: str | None = None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_static_eval(v, variables, var_object) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        if node.conversion != -1 or node.format_spec is not None:
            raise EmbedError("unexpected conversion/format in embedded SQL")
        return _static_eval(node.value, variables, var_object)
    if var_object is None and isinstance(node, ast.Name) and node.id in variables:
        return str(variables[node.id])
    if (
        var_object is not None
        and isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == var_object
        and node.attr in variables
    ):
        return str(variables[node.attr])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_eval(node.left, variables, var_object) + _static_eval(node.right, variables, var_object)
    raise EmbedError(f"unexpected expression in embedded SQL: {ast.dump(node)[:120]}")


# ---------------------------------------------------------------- small text filters
def py_str(value: object) -> str:
    """Python literal for a string (or None)."""
    return repr(value)


def py_docline(value: str) -> str:
    """Single line safe inside a triple-double-quoted docstring."""
    text = " ".join(str(value or "").split())
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"').rstrip('"')


def py_comment(value: str) -> str:
    return " ".join(str(value or "").split())


def yaml_value(value: object) -> str:
    """Inline YAML scalar/flow value for the commented config template."""
    if value is None:
        return "null"
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10_000, sort_keys=False)
    dumped = dumped.strip()
    if dumped.endswith("\n..."):
        dumped = dumped[: -len("\n...")]
    if dumped.endswith("..."):
        dumped = dumped[:-3].strip()
    return dumped


def json_pretty(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


FILTERS = {
    "py_str": py_str,
    "py_docline": py_docline,
    "py_comment": py_comment,
    "yaml_value": yaml_value,
    "json_pretty": json_pretty,
}
