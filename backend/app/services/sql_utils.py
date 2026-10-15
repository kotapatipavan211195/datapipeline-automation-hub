"""Deterministic SQL helpers built on sqlglot (BigQuery dialect).

Everything here is pure (no GCP, no LLM) so it can back both the guardrails and the tests.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

DIALECT = "bigquery"

_DATE_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")


@dataclass
class SqlFacts:
    statements: list[str] = field(default_factory=list)  # one entry per statement: its kind
    kind: str = "OTHER"
    sources: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    target_ops: dict[str, str] = field(default_factory=dict)  # target table -> MERGE / INSERT / CREATE TABLE / …
    literals: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    parse_error: str | None = None
    risks: list[tuple[str, str]] = field(default_factory=list)  # (severity, message)


def parse(sql: str) -> list[exp.Expression]:
    return [e for e in sqlglot.parse(sql, read=DIALECT) if e is not None]


def table_name(t: exp.Table) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p)


def statement_kind(e: exp.Expression) -> str:
    if isinstance(e, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        return "SELECT"
    if isinstance(e, exp.Merge):
        return "MERGE"
    if isinstance(e, (exp.Insert, exp.Update, exp.Delete)):
        return "DML"
    if isinstance(e, (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)):
        return "DDL"
    if isinstance(e, exp.Export):
        return "EXPORT"
    if isinstance(e, (exp.Declare, exp.Set, exp.Command, exp.Transaction, exp.Commit)):
        return "SCRIPT"
    return "OTHER"


def overall_kind(kinds: list[str]) -> str:
    if not kinds:
        return "OTHER"
    if len(kinds) > 1:
        return "SCRIPT"
    return {"EXPORT": "OTHER"}.get(kinds[0], kinds[0])


def _target_node(e: exp.Expression) -> exp.Table | None:
    if isinstance(e, exp.TruncateTable):  # TRUNCATE TABLE keeps its table(s) in .expressions
        first = (e.expressions or [None])[0]
        return first if isinstance(first, exp.Table) else None
    if isinstance(e, (exp.Insert, exp.Merge, exp.Update, exp.Delete, exp.Create, exp.Drop)):
        target = e.this
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table):
            return target
    return None


def operation_label(e: exp.Expression) -> str:
    """What a statement does to its target, in words for the README (INSERT, MERGE, CREATE TABLE, …)."""
    if isinstance(e, exp.Create):
        kind = str(e.args.get("kind") or "TABLE").upper()
        replace = "CREATE OR REPLACE" if e.args.get("replace") else "CREATE"
        return f"{replace} {kind}"
    if isinstance(e, exp.TruncateTable):
        return "TRUNCATE"
    return type(e).__name__.upper()  # Insert, Merge, Update, Delete, Drop, Alter


def facts(sql: str) -> SqlFacts:
    """Parse once and collect everything the prompt, validators and order checks need."""
    out = SqlFacts(params=find_query_params(sql))
    try:
        statements = parse(sql)
    except (ParseError, TokenError) as exc:
        out.parse_error = _short_error(exc)
        return out
    local_names: set[str] = set()  # CTEs and temp tables are not physical sources/targets
    sources: list[str] = []
    targets: list[str] = []
    for e in statements:
        out.statements.append(statement_kind(e))
        for cte in e.find_all(exp.CTE):
            local_names.add(cte.alias_or_name.lower())
        target = _target_node(e)
        if target is not None:
            name = table_name(target)
            if isinstance(e, exp.Create) and e.find(exp.TemporaryProperty) is not None:
                local_names.add(name.lower())
            else:
                targets.append(name)
                previous = out.target_ops.get(name)
                op = operation_label(e)
                out.target_ops[name] = op if previous in (None, op) else f"{previous}, {op}"
        for t in e.find_all(exp.Table):
            if t is target:
                continue
            name = table_name(t)
            if name:
                sources.append(name)
        for lit in e.find_all(exp.Literal):
            if lit.is_string and _DATE_LITERAL_RE.match(lit.this or ""):
                out.literals.append(lit.this)
        _collect_risks(e, out.risks)
    out.targets = _dedupe([t for t in targets if t.lower() not in local_names])
    out.target_ops = {t: op for t, op in out.target_ops.items() if t.lower() not in local_names}
    out.sources = _dedupe([s for s in sources if s.lower() not in local_names])
    out.literals = _dedupe(out.literals)
    out.risks = list(dict.fromkeys(out.risks))
    out.kind = overall_kind(out.statements)
    if out.literals:
        out.risks.append(("info", f"hard-coded date value(s) {', '.join(out.literals[:5])}: consider a date parameter "
                                  "(relative to today, or a value per environment in the YAML)"))
    return out


def _collect_risks(e: exp.Expression, risks: list[tuple[str, str]]) -> None:
    for sel in e.find_all(exp.Select):
        if any(isinstance(p, exp.Star) for p in sel.expressions):
            risks.append(("warning", "SELECT * — columns added upstream will change the output; list columns explicitly"))
            break
    if isinstance(e, (exp.Delete, exp.Update)) and not e.args.get("where"):
        risks.append(("error", f"{type(e).__name__.upper()} without WHERE affects every row of the table"))


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        seen.setdefault(i, None)
    return list(seen)


def _short_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if errors:
        e = errors[0]
        line, col = e.get("line"), e.get("col")
        desc = e.get("description") or str(exc)
        return f"line {line}, column {col}: {desc}" if line else desc
    return str(exc).split("\n")[0]


def table_templates(sql: str) -> tuple[list[str], list[str]]:
    """Sources/targets of SQL that still contains ${var} placeholders, returned as {var} templates
    (e.g. "{project_id}.{mart_dataset}.sales_daily") so they can be resolved per environment."""
    sources, target_ops = table_template_ops(sql)
    return sources, list(target_ops)


def table_template_ops(sql: str) -> tuple[list[str], dict[str, str]]:
    """Like table_templates, but targets map to what the SQL does to them (MERGE, INSERT, CREATE TABLE, …)."""
    f = facts(mark_vars(sql))
    if f.parse_error:
        return [], {}
    return [unmark_vars(x) for x in f.sources], {unmark_vars(t): f.target_ops.get(t, "") for t in f.targets}


def mark_vars(sql: str) -> str:
    """${var} → __sfvar_var__ so SQL with config placeholders still parses (identifiers stay identifiers)."""
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"__sfvar_\1__", sql)


def unmark_vars(name: str, opener: str = "{") -> str:
    """Back from __sfvar_var__ to {var} (path-template syntax) or, with opener="${", to ${var} (SQL syntax)."""
    return re.sub(r"__sfvar_([A-Za-z_][A-Za-z0-9_]*?)__", lambda m: opener + m.group(1) + "}", name)


# ---------------------------------------------------------------- @params
def _param_spans(sql: str) -> list[tuple[int, int, str]] | None:
    """(start, end_exclusive, name) of every @name parameter, or None if the SQL cannot be tokenized."""
    try:
        tokens = sqlglot.tokenize(sql, read=DIALECT)
    except (TokenError, ParseError):
        return None
    spans = []
    for i, tok in enumerate(tokens):
        if tok.token_type != TokenType.PARAMETER:
            continue
        prev_at = i > 0 and tokens[i - 1].token_type == TokenType.PARAMETER and tokens[i - 1].end + 1 == tok.start
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
        if prev_at or next_tok is None or next_tok.token_type == TokenType.PARAMETER:
            continue  # part of @@system_variable, or a dangling @
        if next_tok.start == tok.end + 1:
            spans.append((tok.start, next_tok.end + 1, next_tok.text))
    return spans


def find_query_params(sql: str) -> list[str]:
    """Named query parameters (@name), ignoring @@system variables, strings and comments."""
    spans = _param_spans(sql)
    if spans is None:
        return _dedupe(re.findall(r"(?<![@\w])@([A-Za-z_][A-Za-z0-9_]*)", sql))
    return _dedupe([name for _s, _e, name in spans])


def substitute_params(sql: str, literals: dict[str, str]) -> str:
    """Replace @name with its original literal text (for the equivalence check / dry-runs)."""
    spans = _param_spans(sql)
    if spans is None:
        for name, literal in sorted(literals.items(), key=lambda kv: -len(kv[0])):
            sql = re.sub(rf"(?<![@\w])@{re.escape(name)}\b", lambda _m, lit=literal: lit, sql)
        return sql
    for start, end, name in reversed(spans):
        if name in literals:
            sql = sql[:start] + literals[name] + sql[end:]
    return sql


# ---------------------------------------------------------------- equivalence
def normalize(sql: str) -> tuple[str, list[str]]:
    """Canonical form of a SQL script: ('ast', statements) or ('tokens', token texts)."""
    try:
        return "ast", [e.sql(dialect=DIALECT, comments=False) for e in parse(sql)]
    except (ParseError, TokenError):
        pass
    try:
        toks = sqlglot.tokenize(sql, read=DIALECT)
        return "tokens", [f"{t.token_type.name}:{t.text.upper() if t.token_type != TokenType.STRING else t.text}" for t in toks]
    except (ParseError, TokenError):
        return "text", [" ".join(sql.split())]


def equivalent(a: str, b: str) -> tuple[bool, str]:
    method_a, norm_a = normalize(a)
    method_b, norm_b = normalize(b)
    if method_a != method_b:
        # one side parses and the other does not — compare on tokens
        method_a, norm_a = "tokens", normalize_tokens(a)
        method_b, norm_b = "tokens", normalize_tokens(b)
    return norm_a == norm_b, method_a


def normalize_tokens(sql: str) -> list[str]:
    try:
        toks = sqlglot.tokenize(sql, read=DIALECT)
        return [f"{t.token_type.name}:{t.text.upper() if t.token_type != TokenType.STRING else t.text}" for t in toks]
    except (ParseError, TokenError):
        return [" ".join(sql.split())]


def unified_diff(a: str, b: str, a_name: str = "original", b_name: str = "proposed") -> str:
    return "".join(
        difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True), a_name, b_name, n=2)
    )


def pretty(sql: str) -> str:
    try:
        return ";\n\n".join(e.sql(dialect=DIALECT, pretty=True) for e in parse(sql))
    except (ParseError, TokenError):
        return sql


def short_table(name: str) -> str:
    return name.split(".")[-1] if name else name


SQL_FILE_EXTENSIONS = (".sql", ".txt")


def decode_sql_file(data: bytes, name: str = "", *, max_bytes: int = 5 * 1024 * 1024) -> str:
    """Text of an attached .sql/.txt file (UTF-8, with or without BOM, or Windows-1252), with Unix line ends."""
    label = name or "the file"
    if name and not name.lower().endswith(SQL_FILE_EXTENSIONS):
        raise ValueError(f"{label}: attach a .sql or .txt file")
    if len(data) > max_bytes:
        raise ValueError(f"{label} is {len(data) / 1024 / 1024:.1f} MB; the limit is {max_bytes / 1024 / 1024:g} MB")
    if b"\x00" in data[:8192]:
        raise ValueError(f"{label} is not a text file")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{label} is neither UTF-8 nor Windows-1252 text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError(f"{label} is empty")
    return text
