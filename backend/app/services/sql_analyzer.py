"""SQL analysis: sqlglot facts → Gemini parameterization → deterministic equivalence guard.

Gemini proposes ${var} / @param placeholders. We never trust that blindly: the placeholders are substituted back
with the original values and the result must be AST-equivalent to the user's SQL. If it is not, the proposal is
rejected and a conservative rule-based parameterization (also verified) is used instead.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import OrderedDict
from pathlib import Path

import sqlglot
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

from ..models.analysis import EquivalenceResult, Finding, GeminiSqlAnalysis, ProjectScan, SqlAnalysis, TableRef
from ..models.rules import (
    IDENTIFIER_RE,
    SAFE_VALUE_RE,
    VAR_TOKEN_RE,
    function_name_problem,
    sql_var_tokens,
    suggest_function_name,
    to_snake_case,
)
from ..models.spec import BUILTIN_PROJECTS, DATE_SOURCES, Parameter, Variable
from . import sql_utils
from .gemini import GeminiError, GeminiService

log = logging.getLogger("dahub.sql")
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "sql_analyze.md"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BQ_TYPES = {"DATE", "TIMESTAMP", "DATETIME", "STRING", "INT64", "FLOAT64", "NUMERIC", "BOOL"}
_SOURCES = DATE_SOURCES | {"process_name", "config"}
_GROUPS = {"project", "dataset", "bucket", "other"}
# Environment words dropped from project ids when naming their variable: acme-raw-prod -> acme_raw_project,
# value-mkt -> mkt_project (lab and value are the team's environment names).
_ENV_TOKENS = frozenset(
    "dev development qa uat test tst stg stage staging prod prd production sbx sandbox nonprod np preprod lab value".split()
)
ENVS = ("lab", "value", "qa")
_BUILTIN_WHAT = {"write_project": "project the process writes to", "read_project": "project the SQL reads from"}


class SqlAnalyzer:
    def __init__(self, gemini: GeminiService | None = None, cache_size: int = 256):
        self.gemini = gemini
        self._cache: OrderedDict[str, SqlAnalysis] = OrderedDict()
        self._cache_size = cache_size

    # ------------------------------------------------------------------ public
    def analyze(
        self,
        sql: str,
        *,
        existing_variables: dict[str, str] | None = None,
        taken_function_names: list[str] | None = None,
        use_ai: bool = True,
        env_values: dict[str, dict[str, str]] | None = None,
        defaults: dict[str, str] | None = None,
    ) -> SqlAnalysis:
        """``existing_variables``: every variable of the process with its **lab** value ('' when not set yet);
        ``env_values``: {env: {variable: value}} of the other environments; ``defaults``: the hub's default project per
        environment (see ProjectPlacer)."""
        existing = dict(existing_variables or {})
        for b in BUILTIN_PROJECTS:
            existing.setdefault(b, "")
        context = (existing, env_values or {}, defaults or {})
        key = hashlib.sha256(
            "\x1f".join([sql, repr(sorted(existing.items())), repr(sorted((e, sorted(v.items())) for e, v in context[1].items())),
                         repr(sorted(context[2].items())), str(use_ai), self._model_tag()]).encode()
        ).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key].model_copy(deep=True)
        result = self._analyze(sql, context, taken_function_names or [], use_ai)
        self._cache[key] = result
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return result.model_copy(deep=True)

    def _model_tag(self) -> str:
        if self.gemini is None:
            return "none"
        return f"{self.gemini.settings.gemini_model}:{self.gemini.settings.prompt_version}"

    # ------------------------------------------------------------------ pipeline
    def _analyze(self, sql: str, context: tuple, taken: list[str], use_ai: bool) -> SqlAnalysis:
        facts = sql_utils.facts(sql)
        findings = [Finding(severity=sev, message=msg) for sev, msg in facts.risks]  # type: ignore[arg-type]
        base = SqlAnalysis(
            original_sql=sql,
            sql=sql,
            statement_kind=facts.kind,
            statements=facts.statements,
            sources=facts.sources,
            targets=facts.targets,
            parse_error=facts.parse_error,
            findings=findings,
        )
        if facts.parse_error:
            base.findings.insert(0, Finding(severity="error", message=f"SQL does not parse: {facts.parse_error}"))

        proposal: SqlAnalysis | None = None
        if use_ai and self.gemini is not None and self.gemini.available:
            try:
                proposal = self._gemini_proposal(sql, facts, context, base)
            except GeminiError as exc:
                log.warning("Gemini analysis failed: %s", exc)
                base.findings.append(Finding(severity="info", message=f"AI analysis unavailable ({exc}); used rule-based analysis"))
        if proposal is not None and proposal.equivalence.ok:
            result = proposal
        else:
            result = self._rule_based(sql, facts, context, base)
            if proposal is not None:  # Gemini changed the logic: keep its text for the diff view only
                result.rejected_proposal = proposal.rejected_proposal
                result.findings.insert(
                    0,
                    Finding(
                        severity="warning",
                        message="Gemini's parameterized SQL was not equivalent to your SQL, so it was rejected; "
                        "a conservative rule-based version is shown instead. " + proposal.equivalence.reason,
                    ),
                )
                result.summary = proposal.summary or result.summary
                result.suggested_function_name = proposal.suggested_function_name or result.suggested_function_name
                result.ai_used, result.model, result.tokens = True, proposal.model, proposal.tokens
        result.suggested_function_name = self._function_name(result, taken)
        result.sources, result.targets = _template_tables(result.sql, facts)
        return result

    # ------------------------------------------------------------------ gemini
    def _gemini_proposal(self, sql: str, facts: sql_utils.SqlFacts, context: tuple, base: SqlAnalysis) -> SqlAnalysis:
        assert self.gemini is not None
        existing = context[0]
        system = PROMPT_PATH.read_text(encoding="utf-8")
        existing_lines = "\n".join(f"- {k} = {v or '(not set yet)'}" for k, v in existing.items())
        prompt = (
            f"Existing variables (reuse when the value matches):\n{existing_lines}\n\n"
            f"<facts>\nstatements: {', '.join(facts.statements) or 'unknown'}\n"
            f"tables referenced: {', '.join(facts.sources + facts.targets) or 'none found'}\n"
            f"date literals: {', '.join(facts.literals) or 'none'}\n"
            f"existing @parameters: {', '.join(facts.params) or 'none'}\n</facts>\n\n"
            f"<sql>\n{sql}\n</sql>"
        )
        g, usage = self.gemini.generate_structured(prompt, GeminiSqlAnalysis, system_instruction=system)
        placer = ProjectPlacer(*context, write_projects=_target_projects(sql))
        variables, renames, problems, fill = _merge_variables(g, existing, placer)
        parameterized = VAR_TOKEN_RE.sub(lambda m: "${" + renames.get(m.group(1), m.group(1)) + "}", g.parameterized_sql)
        parameters, param_problems = _clean_parameters(g)
        problems += param_problems
        all_values = {**existing, **fill}
        equivalence = check_equivalence(sql, parameterized, all_values, {p.name: p.original_literal or "" for p in parameters})
        if problems and equivalence.ok:
            equivalence = EquivalenceResult(ok=False, method=equivalence.method, reason="; ".join(problems))
        result = base.model_copy(deep=True)
        result.summary = g.summary.strip()
        result.suggested_function_name = g.suggested_function_name.strip()
        result.statement_kind = g.statement_kind.upper() if g.statement_kind.upper() in {"SELECT", "DML", "DDL", "MERGE", "SCRIPT", "OTHER"} else base.statement_kind
        dest = (g.suggested_destination_table or "").strip()
        result.suggested_destination_table = _dest_template(dest) if dest and result.statement_kind == "SELECT" else None
        result.findings += [
            Finding(severity=f.severity if f.severity in ("info", "warning", "error") else "info", message=f.message, source="gemini")
            for f in g.findings
            if f.message.strip()
        ]
        result.ai_used, result.model, result.tokens = True, self.gemini.settings.gemini_model, usage.as_dict()
        result.equivalence = equivalence
        if equivalence.ok:
            result.sql = parameterized
            result.variables = [v for v in variables if v.name in sql_var_tokens(parameterized)]
            used = set(sql_utils.find_query_params(_fill_vars(parameterized, all_values)))
            result.parameters = [p for p in parameters if p.name in used]
        else:
            result.rejected_proposal = parameterized
        return result

    # ------------------------------------------------------------------ rule-based fallback
    def _rule_based(self, sql: str, facts: sql_utils.SqlFacts, context: tuple, base: SqlAnalysis) -> SqlAnalysis:
        result = base.model_copy(deep=True)
        if facts.parse_error:
            return result
        existing, env_values, defaults = context
        parameterized, variables, parameters, fill = rule_based_parameterize(sql, existing, env_values=env_values,
                                                                             defaults=defaults)
        values = {**existing, **fill}
        equivalence = check_equivalence(sql, parameterized, values, {p.name: p.original_literal or "" for p in parameters})
        if equivalence.ok:
            result.sql, result.variables, result.parameters = parameterized, variables, parameters
        result.equivalence = equivalence
        if not result.summary:
            result.summary = _rule_summary(facts)
        return result

    def _function_name(self, result: SqlAnalysis, taken: list[str]) -> str:
        name = to_snake_case(result.suggested_function_name or "")
        if name and not function_name_problem(name) and name not in taken:
            return name
        target = sql_utils.short_table(result.targets[0]) if result.targets else ""
        target = re.sub(r"\{[^}]*\}", "", target)
        verb = {"MERGE": "merge", "DML": "load", "DDL": "create", "SELECT": "build", "SCRIPT": "run"}.get(result.statement_kind, "run")
        return suggest_function_name(f"{verb}_{target}" if target else f"{verb}_sql", taken)


# ====================================================================== helpers
def _fill_vars(sql: str, values: dict[str, str]) -> str:
    return VAR_TOKEN_RE.sub(lambda m: values.get(m.group(1), m.group(0)), sql)


def check_equivalence(original: str, parameterized: str, variables: dict[str, str], literals: dict[str, str]) -> EquivalenceResult:
    """Substitute ${var} and @param back to their original values and compare normalized SQL."""
    missing_vars = [t for t in sql_var_tokens(parameterized) if t not in variables]
    if missing_vars:
        return EquivalenceResult(ok=False, reason=f"unknown variable(s): {', '.join(missing_vars)}")
    used_params = sql_utils.find_query_params(_fill_vars(parameterized, variables))
    original_params = set(sql_utils.find_query_params(original))
    new_params = [p for p in used_params if p not in original_params]
    missing = [p for p in new_params if not literals.get(p)]
    if missing:
        return EquivalenceResult(ok=False, reason=f"no original value recorded for @{', @'.join(missing)}")
    concrete = sql_utils.substitute_params(_fill_vars(parameterized, variables), {p: literals[p] for p in new_params})
    ok, method = sql_utils.equivalent(original, concrete)
    if ok:
        return EquivalenceResult(ok=True, method=method)  # type: ignore[arg-type]
    return EquivalenceResult(
        ok=False,
        method=method,  # type: ignore[arg-type]
        reason="with the placeholders filled back in, the SQL differs from the original",
        diff=sql_utils.unified_diff(sql_utils.pretty(original), sql_utils.pretty(concrete)),
    )


def _merge_variables(
    g: GeminiSqlAnalysis, existing: dict[str, str], placer: ProjectPlacer
) -> tuple[list[Variable], dict[str, str], list[str], dict[str, str]]:
    """Gemini's variables, reusing existing ones by (lab) value; projects are placed like the automatic scan does
    (ProjectPlacer). Returns (new variables, Gemini name → final name, problems, final name → the value it replaced)."""
    by_value = {v: k for k, v in existing.items() if v}
    variables: list[Variable] = []
    renames: dict[str, str] = {}
    problems: list[str] = []
    fill: dict[str, str] = {}
    taken = {k for k, v in existing.items() if v or k not in BUILTIN_PROJECTS}
    for gv in g.variables:
        name = to_snake_case(gv.name)
        value = gv.value.strip().strip("`")
        if not SAFE_VALUE_RE.match(value) or not IDENTIFIER_RE.match(name or "-"):
            problems.append(f"invalid variable {gv.name}={gv.value!r}")
            continue
        group = (gv.group or "").strip().lower()
        if group == "project" or name in BUILTIN_PROJECTS or (group not in _GROUPS and name.endswith("_project")):
            final = placer.place(value, hint=name, description=gv.description.strip()[:200])
            renames[gv.name] = final
            fill[final] = value
            taken.add(final)
            continue
        if value in by_value:  # reuse an existing variable with the same value
            renames[gv.name] = by_value[value]
            fill[by_value[value]] = value
            continue
        final = name
        n = 2
        while final in taken or final in placer.names() or final == "date":
            final = f"{name}_{n}"
            n += 1
        renames[gv.name] = final
        fill[final] = value
        taken.add(final)
        by_value[value] = final
        if group not in _GROUPS:
            group = "dataset" if final.endswith("_dataset") else "other"
        variables.append(Variable(name=final, group=group, values={"lab": value}, description=gv.description.strip()[:200]))
    return placer.variables() + variables, renames, problems, fill


def _clean_parameters(g: GeminiSqlAnalysis) -> tuple[list[Parameter], list[str]]:
    params: list[Parameter] = []
    problems: list[str] = []
    for gp in g.parameters:
        bq_type = gp.bq_type.upper().replace("INTEGER", "INT64").replace("FLOAT", "FLOAT64").replace("FLOAT6464", "FLOAT64")
        bq_type = {"BOOLEAN": "BOOL"}.get(bq_type, bq_type)
        source = gp.source.strip().lower()
        if bq_type not in _BQ_TYPES or source not in _SOURCES or not IDENTIFIER_RE.match(gp.name):
            problems.append(f"invalid parameter @{gp.name} ({gp.bq_type}, {gp.source})")
            continue
        try:
            params.append(
                Parameter(
                    name=gp.name,
                    bq_type=bq_type,  # type: ignore[arg-type]
                    source=source,  # type: ignore[arg-type]
                    offset_days=gp.offset_days if source == "today_offset" else 0,
                    default=(gp.default or gp.original_literal or "").strip("'\"") if source == "config" else None,
                    original_literal=gp.original_literal or None,
                    description=gp.description[:200],
                )
            )
        except ValueError as exc:
            problems.append(f"parameter @{gp.name}: {exc}")
    return params, problems


def _dest_template(dest: str) -> str:
    """Gemini writes ${var}.x.y; destination tables in the spec use {var} path-template syntax."""
    return VAR_TOKEN_RE.sub(lambda m: "{" + m.group(1) + "}", dest.strip("`"))


def _template_tables(sql: str, facts: sql_utils.SqlFacts) -> tuple[list[str], list[str]]:
    if VAR_TOKEN_RE.search(sql):
        sources, targets = sql_utils.table_templates(sql)
        if sources or targets:
            return sources, targets
    return facts.sources, facts.targets


def _rule_summary(facts: sql_utils.SqlFacts) -> str:
    parts = [f"{facts.kind} SQL"]
    if facts.sources:
        parts.append(f"reading {', '.join(facts.sources[:4])}")
    if facts.targets:
        parts.append(f"writing {', '.join(facts.targets[:4])}")
    return " ".join(parts) + "."


def project_variable_name(project: str) -> str:
    """Config variable name for a GCP project id, without its environment word: acme-raw-prod -> acme_raw_project."""
    parts = [p for p in re.split(r"[^a-z0-9]+", project.lower()) if p]
    kept = [p for p in parts if p not in _ENV_TOKENS] or parts
    base = "_".join(kept)[:50].strip("_")
    if not base:
        return "src_project"
    if base[0].isdigit():
        base = f"p_{base}"
    return f"{base}_project"


def rule_based_parameterize(
    sql: str,
    existing: dict[str, str],
    *,
    kinds: tuple[str, ...] = ("project", "dataset"),
    dates: bool = True,
    env_values: dict[str, dict[str, str]] | None = None,
    defaults: dict[str, str] | None = None,
) -> tuple[str, list[Variable], list[Parameter], dict[str, str]]:
    """Conservative, deterministic parameterization (used when Gemini is off or its proposal was rejected):

    * project/dataset parts of 3-part table references → ${variables}. Projects are placed by ProjectPlacer (built-in
      read/write_project, an existing variable, or a new <name>_project); datasets reuse existing variables by lab
      value. ``kinds`` limits this to projects only for the automatic scan
    * a single distinct ``DATE 'YYYY-MM-DD'`` typed literal → @cutoff_date read from the YAML (when ``dates``), so
      the SQL keeps its meaning until someone changes the value per environment

    Returns (sql, new variables, parameters, {variable: the value it replaced}) — the last one fills the SQL back for
    the equivalence check.
    """
    try:
        tokens = sqlglot.tokenize(sql, read=sql_utils.DIALECT)
    except (TokenError, ParseError):
        return sql, [], [], {}
    by_value = {v: k for k, v in existing.items() if v}
    placer = ProjectPlacer(existing, env_values, defaults, write_projects=_target_projects(sql))
    new_vars: dict[str, Variable] = {}
    fill: dict[str, str] = {}

    def var_for(value: str, kind: str) -> str:
        if kind == "project":
            name = placer.place(value)
            fill[name] = value
            return name
        if value in by_value:
            fill[by_value[value]] = value
            return by_value[value]
        base = value.lower() if value.lower().endswith(("dataset", "_ds")) else f"{value.lower()}_dataset"
        base = to_snake_case(base) or kind
        name, n = base, 2
        while name in existing or name in new_vars or name in placer.names():
            name = f"{base}_{n}"
            n += 1
        new_vars[name] = Variable(name=name, group=kind, values={"lab": value}, description="BigQuery dataset (was hard-coded in the SQL)")
        by_value[value] = name
        fill[name] = value
        return name

    edits: list[tuple[int, int, str]] = []
    ident_types = (TokenType.IDENTIFIER, TokenType.VAR)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # `project.dataset.table` in one backticked identifier
        if tok.token_type == TokenType.IDENTIFIER and tok.text.count(".") == 2:
            proj, ds, table = tok.text.split(".")
            if SAFE_VALUE_RE.match(proj) and SAFE_VALUE_RE.match(ds):
                proj_part = "${" + var_for(proj, "project") + "}" if "project" in kinds else proj
                ds_part = "${" + var_for(ds, "dataset") + "}" if "dataset" in kinds else ds
                if (proj_part, ds_part) != (proj, ds):
                    edits.append((tok.start + 1, tok.end, f"{proj_part}.{ds_part}.{table}"))  # inside the backticks
        # project.dataset.table as separate (optionally backticked) parts. An unquoted project may contain dashes
        # (FROM acme-ref-prod.reference.stores), which the tokenizer splits into name - name - name.
        elif tok.token_type in ident_types and (i == 0 or tokens[i - 1].token_type != TokenType.DOT) and _is_table_context(tokens, i):
            j = _dashed_end(tokens, i)
            if (
                j + 4 < len(tokens)
                and tokens[j + 1].token_type == TokenType.DOT
                and tokens[j + 2].token_type in ident_types
                and tokens[j + 3].token_type == TokenType.DOT
                and tokens[j + 4].token_type in ident_types
                and (j + 5 >= len(tokens) or tokens[j + 5].token_type != TokenType.DOT)
            ):
                ds_tok = tokens[j + 2]
                parts = (
                    (sql[tok.start : tokens[j].end + 1] if j > i else tok.text, tok.start, tokens[j].end,
                     j == i and tok.token_type == TokenType.IDENTIFIER, "project"),
                    (ds_tok.text, ds_tok.start, ds_tok.end, ds_tok.token_type == TokenType.IDENTIFIER, "dataset"),
                )
                for text, first, last, quoted, kind in parts:
                    if kind in kinds and SAFE_VALUE_RE.match(text):
                        start = first + (1 if quoted else 0)
                        end = last + (0 if quoted else 1)
                        edits.append((start, end, "${" + var_for(text, kind) + "}"))
                i = j + 5
                continue
        i += 1

    params: list[Parameter] = []
    date_spans = []
    for j, tok in enumerate(tokens[:-1] if dates else []):
        nxt = tokens[j + 1]
        if tok.token_type == TokenType.DATE and nxt.token_type == TokenType.STRING and _DATE_RE.match(nxt.text):
            date_spans.append((tok.start, nxt.end + 1, sql[tok.start : nxt.end + 1], nxt.text))
    if len({d[3] for d in date_spans}) == 1 and "cutoff_date" not in sql_utils.find_query_params(sql):
        literal, value = date_spans[0][2], date_spans[0][3]
        for start, end, _text, _value in date_spans:
            edits.append((start, end, "@cutoff_date"))
        params.append(Parameter(name="cutoff_date", bq_type="DATE", source="config", default=value, original_literal=literal,
                                description="date that was hard-coded in the SQL; set it per environment in the YAML"))
    out = sql
    for start, end, text in sorted(edits, key=lambda e: -e[0]):
        out = out[:start] + text + out[end:]
    return out, placer.variables() + list(new_vars.values()), params, fill


class ProjectPlacer:
    """Where a hard-coded GCP project of pasted SQL goes, the same way for the automatic scan and both analyses:

    1. the value of a variable in any environment (lab first) → that variable; when it is both built-ins, the one of
       its role (write_project for a project the SQL writes into, else read_project);
    2. the hub's default project of an environment (e.g. value-gcp in value) → the built-in of its role, filling only
       that environment (lab is left to type);
    3. a project of the role's built-in that has no lab value yet → the built-in, with it as the lab value (the SQL
       is lab SQL). When read_project and write_project are one project in prod (both value-gcp), both get it, and
       once that project's lab name is known any other project goes to 4;
    4. any other project → its own <name>_project, with the same name in every environment (as it is used in prod).

    A variable stands for one project per SQL text, so filling the placeholders back gives the original SQL.
    """

    def __init__(self, existing: dict[str, str], env_values: dict[str, dict[str, str]] | None = None,
                 defaults: dict[str, str] | None = None, *, write_projects: set[str] | None = None):
        self.values = {env: {} for env in ENVS}
        self.values["lab"] = {k: v for k, v in existing.items() if v}
        for env, values in (env_values or {}).items():
            if env in ENVS and env != "lab":
                self.values[env] = {k: v for k, v in values.items() if v}
        self.defaults = {env: p for env, p in (defaults or {}).items() if env in ENVS and p}
        self.known = set(existing) | {k for values in (env_values or {}).values() for k in values}
        self.write_projects = write_projects or set()
        self.new: dict[str, Variable] = {}
        self.found: dict[str, str] = {}  # variable -> the project it stands for in this SQL

    def names(self) -> set[str]:
        return self.known | set(self.new)

    def variables(self) -> list[Variable]:
        return list(self.new.values())

    def _free_for(self, name: str, project: str) -> bool:
        return self.found.get(name, project) == project

    def _remember(self, name: str, project: str, envs: dict[str, str], description: str) -> str:
        self.found[name] = project
        for env, value in envs.items():
            self.values[env][name] = value
        if name in self.new:
            self.new[name] = self.new[name].model_copy(update={"values": self.new[name].values.model_copy(update=envs)})
        elif envs:
            self.new[name] = Variable(name=name, group="project", values=envs, description=description)
        return name

    def place(self, project: str, *, hint: str = "", description: str = "") -> str:
        writes = project in self.write_projects
        role = ["write_project", "read_project"] if writes else ["read_project", "write_project"]
        what = _BUILTIN_WHAT[role[0]]
        for env in ENVS:  # 1. already the value of a variable
            names = [n for n, v in self.values[env].items() if v == project and self._free_for(n, project)]
            if names:
                name = next((b for b in role if b in names), names[0])
                return self._remember(name, project, {}, "")
        envs = [env for env, p in self.defaults.items() if p == project]
        if envs:  # 2. the hub's default project of an environment
            for name in role:
                if self._free_for(name, project) and not any(self.values[env].get(name) for env in envs):
                    return self._remember(name, project, {env: project for env in envs}, description or what)
        name, other = role  # 3. the role's built-in, still without a lab value
        prod = [(self.values[env].get(name), self.values[env].get(other)) for env in ENVS if env != "lab"]
        one_project = any(mine for mine, _ in prod) and all(mine == theirs for mine, theirs in prod)  # e.g. both value-gcp
        if not self.values["lab"].get(name) and self._free_for(name, project) and not (
                one_project and self.values["lab"].get(other)):  # one project whose lab name is known: P is another one
            self._remember(name, project, {"lab": project}, description or what)
            if one_project and self._free_for(other, project):  # one project in prod → one project in lab
                self._remember(other, project, {"lab": project}, _BUILTIN_WHAT[other])
            return name
        base = hint if hint and hint.endswith("_project") and hint not in BUILTIN_PROJECTS else project_variable_name(project)
        name, n = base, 2  # 4. a project of its own, named the same in every environment
        while name in self.names() or name in BUILTIN_PROJECTS:
            name = f"{base}_{n}"
            n += 1
        return self._remember(name, project, {env: project for env in ENVS},
                              description or "GCP project (was hard-coded in the SQL; the same in every environment)")


def _target_projects(sql: str) -> set[str]:
    """Projects of the tables the SQL writes (3-part names only)."""
    facts = sql_utils.facts(sql_utils.mark_vars(sql))
    return {t.split(".")[0].strip("`") for t in facts.targets if t.count(".") >= 2}


def scan_projects(sql: str, existing: dict[str, str], *, step: str = "", env_values: dict[str, dict[str, str]] | None = None,
                  defaults: dict[str, str] | None = None) -> ProjectScan:
    """Automatic (on paste, no AI): list the tables a SQL text reads and writes, and turn every hard-coded GCP project
    in them into a config ${variable} (see ProjectPlacer): the project written to becomes ${write_project}, the one
    read from ${read_project}, others ${<name>_project}. ``existing`` maps variable names to their **lab** values,
    ``env_values`` gives the other environments' values and ``defaults`` the hub's default project per environment
    (value-gcp in value). The rewrite is kept only if it is provably equivalent."""
    existing = dict(existing)
    for b in BUILTIN_PROJECTS:
        existing.setdefault(b, "")
    marked = sql_utils.mark_vars(sql)
    facts = sql_utils.facts(marked)
    scan = ProjectScan(original_sql=sql, sql=sql, statement_kind=facts.kind, parse_error=facts.parse_error)
    if facts.parse_error:
        return scan
    new_sql, new_vars, _, fill = rule_based_parameterize(sql, existing, kinds=("project",), dates=False,
                                                         env_values=env_values, defaults=defaults)
    values = {**existing, **fill}
    if new_sql != sql:
        eq = check_equivalence(_fill_vars(sql, values), new_sql, values, {})  # SQL may already hold ${variables}
        if eq.ok:
            where = f" of {step}" if step else ""
            scan.sql, scan.changed = new_sql, True
            scan.variables = [v if v.name in BUILTIN_PROJECTS else v.model_copy(update={
                "description": f"GCP project (found in the SQL{where}; the same in every environment)"}) for v in new_vars]
        else:
            scan.equivalence = eq
            scan.findings.append(Finding(severity="warning", message="Projects were left as written: replacing them with "
                                         "variables did not give provably the same SQL. " + eq.reason))
    by_value = {v: k for k, v in (values if scan.changed else existing).items() if v}
    for role, names in (("reads", facts.sources), ("writes", facts.targets)):
        for name in names:
            parts = [sql_utils.unmark_vars(p, "${") for p in name.split(".")]
            ref = TableRef(role=role, name=sql_utils.unmark_vars(name, "${"), table=parts[-1])  # type: ignore[arg-type]
            if len(parts) >= 2:
                ref.dataset = parts[-2]
            if len(parts) >= 3:
                ref.project = parts[-3]
                placeholder = VAR_TOKEN_RE.fullmatch(ref.project)
                ref.variable = placeholder.group(1) if placeholder else by_value.get(ref.project)
            scan.tables.append(ref)
    two_part = [t.name for t in scan.tables if t.project is None and t.dataset]
    if two_part:
        scan.findings.append(Finding(
            severity="info",
            message=f"{', '.join(two_part[:5])} {'has' if len(two_part) == 1 else 'have'} no project, so BigQuery uses the "
                    f"credentials' project (your gcloud project in lab, the service account's in prod) for "
                    f"{'it' if len(two_part) == 1 else 'them'}: prefer ${{write_project}}.dataset.table.",
        ))
    scan.sources, scan.targets = _template_tables(scan.sql, facts)
    return scan


def _dashed_end(tokens: list, i: int) -> int:
    """Index of the last token of an unquoted dashed name that starts at i (acme-ref-prod = 5 tokens); i if none."""
    j = i
    if tokens[i].token_type != TokenType.VAR:
        return i
    while (
        j + 2 < len(tokens)
        and tokens[j + 1].token_type == TokenType.DASH
        and tokens[j + 1].start == tokens[j].end + 1
        and tokens[j + 2].start == tokens[j + 1].end + 1
        and re.fullmatch(r"[A-Za-z0-9_]+", tokens[j + 2].text)
    ):
        j += 2
    return j


def _is_table_context(tokens: list, i: int) -> bool:
    """Previous meaningful token suggests a table reference (FROM/JOIN/INTO/UPDATE/MERGE/TABLE/USING/,)."""
    if i == 0:
        return False
    prev = tokens[i - 1].token_type
    return prev in {
        TokenType.FROM, TokenType.JOIN, TokenType.INTO, TokenType.UPDATE, TokenType.MERGE, TokenType.TABLE,
        TokenType.USING, TokenType.COMMA, TokenType.L_PAREN,
    } or tokens[i - 1].text.upper() in {"FROM", "JOIN", "INTO", "UPDATE", "MERGE", "TABLE", "USING", "EXISTS"}
