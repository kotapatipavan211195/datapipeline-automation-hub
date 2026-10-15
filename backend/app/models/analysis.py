"""Models for SQL analysis: the Gemini response schema and the API result returned to the wizard."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .spec import Parameter, Variable


# ---------------------------------------------------------------- Gemini structured output
# Kept deliberately flat (no dicts, no unions) so google-genai can convert it to a response_schema.
class GeminiVariable(BaseModel):
    name: str = Field(description="lower_snake_case config key, e.g. write_project, read_project, strategic01_dataset")
    group: str = Field(description="project | dataset | other")
    value: str = Field(description="the exact hard-coded value it replaces, e.g. strategic01")
    description: str = Field(description="one short sentence")


class GeminiParameter(BaseModel):
    name: str = Field(description="query parameter name without @, e.g. report_date or cutoff_date")
    bq_type: str = Field(description="DATE | TIMESTAMP | DATETIME | STRING | INT64 | FLOAT64 | NUMERIC | BOOL")
    source: str = Field(
        description=(
            "today | today_offset | month_start | month_end | prev_month_start | prev_month_end | "
            "year_start | process_name | config"
        )
    )
    offset_days: int = Field(description="days added to today when source is today_offset (e.g. -1 = yesterday), else 0")
    original_literal: str = Field(description="the exact SQL text that @name replaced, copied verbatim")
    default: str = Field(description="for source=config: the value (YAML, same in every environment at first); otherwise empty")
    description: str = Field(description="one short sentence")


class GeminiFinding(BaseModel):
    severity: str = Field(description="info | warning | error")
    message: str


class GeminiSqlAnalysis(BaseModel):
    summary: str = Field(description="2-4 plain-English sentences: what the SQL does and what it writes")
    suggested_function_name: str = Field(description="verb-first lower_snake_case python function name")
    statement_kind: str = Field(description="SELECT | DML | DDL | MERGE | SCRIPT | OTHER")
    parameterized_sql: str = Field(description="the SQL with ${var} and @param placeholders, otherwise unchanged")
    variables: list[GeminiVariable]
    parameters: list[GeminiParameter]
    source_tables: list[str]
    target_tables: list[str]
    suggested_destination_table: str = Field(description="for a SELECT only, else empty")
    findings: list[GeminiFinding]


# ---------------------------------------------------------------- API result
class Finding(BaseModel):
    severity: Literal["info", "warning", "error"] = "info"
    message: str
    source: Literal["gemini", "rules"] = "rules"


class EquivalenceResult(BaseModel):
    ok: bool
    method: Literal["ast", "tokens", "text", "skipped"] = "skipped"
    reason: str = ""
    diff: str = ""


class TableRef(BaseModel):
    """One table referenced by a SQL text, split into its parts (automatic scan, no AI)."""

    role: Literal["reads", "writes"]
    name: str  # as written in the SQL
    project: str | None = None  # None: dataset.table, which BigQuery runs in the credentials' project
    dataset: str | None = None
    table: str
    variable: str | None = None  # config variable that now holds the project


class ProjectScan(BaseModel):
    """Result of the automatic scan of pasted SQL: its tables, and the SQL with each GCP project as a ${variable}."""

    original_sql: str
    sql: str  # == original_sql when nothing changed (or the rewrite was not provably equivalent)
    changed: bool = False
    statement_kind: str = "OTHER"
    variables: list[Variable] = Field(default_factory=list)  # new variables (projects not yet in the config)
    tables: list[TableRef] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # {var} templates, like SqlAnalysis
    targets: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    equivalence: EquivalenceResult = Field(default_factory=lambda: EquivalenceResult(ok=True))
    parse_error: str | None = None


class SqlAnalysis(BaseModel):
    summary: str = ""
    suggested_function_name: str = ""
    statement_kind: str = "OTHER"
    statements: list[str] = Field(default_factory=list)
    original_sql: str
    sql: str  # proposed approved SQL (parameterized when the equivalence check passed)
    variables: list[Variable] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    suggested_destination_table: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    equivalence: EquivalenceResult = Field(default_factory=lambda: EquivalenceResult(ok=True))
    rejected_proposal: str | None = None
    parse_error: str | None = None
    ai_used: bool = False
    model: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
