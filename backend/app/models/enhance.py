"""Models for "Enhance a script": the analysis of an existing script, and the guarded AI rewrite.

An uploaded script is either *converted* into a hub process (every step function maps to a hub step type) or, when it
holds code the hub cannot model, *rewritten in place* by Gemini under deterministic guards.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

FunctionKind = Literal["sql", "export", "copy", "ingest", "email", "helper", "auth", "unmapped"]
ENHANCE_NOTE = "Enhanced to the team script standard"


class StandardsItem(BaseModel):
    """One rule of the team's script standard, checked on a script."""

    key: str
    ok: bool
    title: str  # the rule, e.g. "One function per step, logging execution begin / end"
    detail: str = ""  # what was found


class SqlBlock(BaseModel):
    """A SQL text found in the script. f-string fields that are plain names become ${name}."""

    text: str
    names: list[str] = Field(default_factory=list)
    line: int = 0


class FunctionInfo(BaseModel):
    name: str
    line: int = 0
    doc: str = ""
    is_step: bool = False  # called by main() as a pipeline step
    kind: FunctionKind = "helper"
    sql: list[SqlBlock] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)  # unmapped: why the hub cannot model it
    details: dict[str, Any] = Field(default_factory=dict)  # what the converter needs (export settings, copy command, …)


class ScriptAnalysis(BaseModel):
    file_name: str
    stem: str
    header: dict[str, Any] = Field(default_factory=dict)  # authors, reviewers, jira, created_on, description, updates
    functions: list[FunctionInfo] = Field(default_factory=list)
    step_order: list[str] = Field(default_factory=list)  # step functions in the order main() calls them
    standards: list[StandardsItem] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)  # gcp_projects blocks found in the YAML
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)  # environment -> its YAML block
    table_schema: dict[str, Any] | None = None  # table_definition of a -ts YAML
    email: dict[str, Any] = Field(default_factory=dict)  # success / failure / no_file: what main() sends
    credentials: list[str] = Field(default_factory=list)  # service-account / key-file access found ("line 12: …")
    convertible: bool = False
    blockers: list[str] = Field(default_factory=list)  # why it cannot become a hub process
    notes: list[str] = Field(default_factory=list)
    parse_error: str | None = None

    def function(self, name: str) -> FunctionInfo | None:
        return next((f for f in self.functions if f.name == name), None)

    def steps(self) -> list[FunctionInfo]:
        return [f for name in self.step_order if (f := self.function(name)) is not None]


class EnhanceHeader(BaseModel):
    """The ''' header block an enhanced script carries: the old header's values (edited on the Enhance page) plus an
    Updates line for this enhancement. Both the converted process and the rewritten script get it."""

    authors: list[str] = Field(default_factory=list)  # developer PIDs
    reviewers: list[str] = Field(default_factory=list)
    jira: str = ""
    created_on: str | None = None  # initial deployment date (YYYY-MM-DD), when known
    description: str = ""
    updates: list[dict[str, str]] = Field(default_factory=list)  # the old Updates lines: {date, pid, note}
    update_pid: str = ""  # this enhancement's Updates line ('' = the first author)
    update_note: str = ENHANCE_NOTE

    @field_validator("authors", "reviewers", mode="before")
    @classmethod
    def _people(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.split(",")
        if isinstance(v, list):
            return [str(p).strip() for p in v if str(p or "").strip()]
        return v

    @field_validator("created_on")
    @classmethod
    def _created_on(cls, v: str | None) -> str | None:
        if v:
            dt.date.fromisoformat(v)
        return v or None

    @classmethod
    def from_analysis(cls, analysis: ScriptAnalysis) -> EnhanceHeader:
        h = analysis.header
        authors = list(h.get("authors") or [])
        return cls(authors=authors, reviewers=list(h.get("reviewers") or []), jira=h.get("jira") or "",
                   created_on=h.get("created_on"), description=h.get("description") or "",
                   updates=[dict(u) for u in h.get("updates") or []], update_pid=authors[0] if authors else "")

    def all_updates(self, today: dt.date) -> list[dict[str, str]]:
        """The old Updates lines, then this enhancement's (when it has a note)."""
        out = [dict(u) for u in self.updates]
        if self.update_note.strip():
            pid = self.update_pid.strip() or (self.authors[0] if self.authors else "")
            out.append({"date": today.isoformat(), "pid": pid, "note": " ".join(self.update_note.split())})
        return out


# ---------------------------------------------------------------- Gemini structured output (kept flat)
class RewriteProposal(BaseModel):
    script: str = Field(description="the complete enhanced Python script")
    config_yaml: str = Field(description="the complete YAML configuration (gcp_projects with one block per environment); "
                                         "empty when the script reads no configuration")
    changes: list[str] = Field(description="every change made, one short sentence each (renamed or merged functions included)")
    todos: list[str] = Field(description="what a developer still has to do or check by hand")


class GuardCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class RewriteResult(BaseModel):
    ok: bool
    stem: str
    files: dict[str, str] = Field(default_factory=dict)
    checks: list[GuardCheck] = Field(default_factory=list)
    diff: str = ""
    changes: list[str] = Field(default_factory=list)
    todos: list[str] = Field(default_factory=list)
    standards_before: list[StandardsItem] = Field(default_factory=list)
    standards_after: list[StandardsItem] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    model: str | None = None
