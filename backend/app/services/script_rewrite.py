"""Enhance a script the hub cannot convert: Gemini rewrites it to the team standard, deterministic guards decide.

The proposal is only handed out when every guard passes: it parses, ruff finds no errors, every SQL text is the same
as before (after filling in each YAML's lab values), every function is still there (or the change list says why), and
the YAML still has the team layout with the same environments. The user always reviews a diff.
"""

from __future__ import annotations

import ast
import datetime as dt
import difflib
import logging
import sys
from pathlib import Path
from typing import Any

from .. import GENERATOR
from ..models.enhance import EnhanceHeader, GuardCheck, RewriteProposal, RewriteResult, ScriptAnalysis, SqlBlock
from ..models.rules import VAR_TOKEN_RE
from ..models.spec import ProcessSpec
from ..settings import Settings, get_settings
from . import sql_utils
from .gemini import GeminiError
from .renderer import (
    _ruff_check,
    format_python,
    header_block,
    jinja_env,
    render_process,
    run_command,
    step_line,
    usage_lines,
)
from .script_scan import (
    credential_findings,
    find_step_order,
    first_line,
    lab_values,
    module_sql_blocks,
    parse_config_yaml,
    scan_script,
)

log = logging.getLogger("dahub.enhance")
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "script_enhance.md"
EXAMPLE_SPEC: dict[str, Any] = {  # rendered by the hub itself, so the example always shows today's standard
    "spec_version": 3,
    "metadata": {"name": "example_pipeline", "script_name": "dl_example_pipeline", "owner": "developer@example.com",
                 "description": "Builds the daily sales summary and extracts it to the FTP bucket.",
                 "authors": ["P00000"], "jira": "DE-0000"},
    "variables": [
        {"name": "read_project", "group": "project", "values": {"lab": "acme-raw-dev", "value": "acme-raw-prod"}},
        {"name": "write_project", "group": "project", "values": {"lab": "acme-analytics-dev", "value": "acme-analytics-prod"}},
        {"name": "mart_dataset", "group": "dataset", "values": {"lab": "sales_mart_lab", "value": "sales_mart"}},
        {"name": "ftp_bucket", "group": "bucket", "values": {"lab": "acme-ftp-dev", "value": "acme-ftp-prod"}},
    ],
    "email": {"enabled": True, "receivers": {"lab": "developer@example.com", "value": "team@example.com"},
              "send_on": {"success": True, "failure": True}},
    "steps": [
        {"type": "sql", "function_name": "build_daily_summary", "description": "Rebuild the daily sales summary",
         "sql": "CREATE OR REPLACE TABLE `${write_project}.${mart_dataset}.daily_summary` AS\n"
                "SELECT store_id, SUM(amount) AS amount\nFROM `${read_project}.sales.orders`\n"
                "WHERE order_date = @run_day\nGROUP BY store_id",
         "statement_kind": "DDL", "parameters": [{"name": "run_day", "bq_type": "DATE", "source": "today_offset", "offset_days": -1}]},
        {"type": "export", "function_name": "extract_daily_summary", "description": "Extract the summary as a dated CSV",
         "source_table": "{write_project}.{mart_dataset}.daily_summary", "bucket": "{ftp_bucket}", "path": "outbound/sales",
         "file_name": "daily_summary_", "attach_to_email": True},
    ],
}
REQUIREMENTS = {  # import → pip requirement
    "google.cloud.bigquery": "google-cloud-bigquery>=3.25", "google.cloud.storage": "google-cloud-storage>=2.18",
    "google.cloud.secretmanager": "google-cloud-secret-manager>=2.20", "pandas": "pandas>=2.0", "numpy": "numpy",
    "jaydebeapi": "JayDeBeApi>=1.2", "requests": "requests>=2.31", "paramiko": "paramiko>=3.0", "pysftp": "pysftp",
    "openpyxl": "openpyxl", "xlsxwriter": "XlsxWriter", "sqlalchemy": "SQLAlchemy", "pyodbc": "pyodbc",
    "oracledb": "oracledb", "cx_Oracle": "cx_Oracle", "psycopg2": "psycopg2-binary", "pymysql": "PyMySQL",
    "boto3": "boto3", "pyarrow": "pyarrow>=14.0", "db_dtypes": "db-dtypes>=1.2",
}
_example_cache: dict[tuple, str] = {}


def standard_example(settings: Settings) -> str:
    key = (tuple(settings.module_paths), settings.sql_logger_enabled, settings.smtp_host)
    if key not in _example_cache:
        result = render_process(ProcessSpec.model_validate(EXAMPLE_SPEC), smoke_run=False, adc_email="developer@example.com",
                                settings=settings, today=dt.date(2026, 1, 1))
        _example_cache[key] = result.files["dl_example_pipeline.py"]
    return _example_cache[key]


def gsm_pattern() -> str:
    """The hub's Secret Manager reader (the same function generated scripts get)."""
    return jinja_env().get_template("partials/_secrets.py.j2").render().rstrip()


def build_prompt(analysis: ScriptAnalysis, source: str, config_yaml: str | None, settings: Settings) -> str:
    gaps = [f"- {s.title}" + (f": {s.detail}" if s.detail else "") for s in analysis.standards if not s.ok]
    return "\n".join([
        "## The team standard (an example the team's tool generated)", "```python", standard_example(settings).rstrip(), "```",
        "", "## Reading Google Secret Manager the team way (only when the script reads secrets)",
        "main() fills SECRETS[<name>] = get_gsm_secret(gsm_project, '<secret id>', 'latest') before the steps (import "
        "`from modules import gsm_module as gsm`, gsm_project from the YAML); the steps pick the keys they need from the "
        "whole payload, e.g. SECRETS['geocode_api_key']['api_credentials'][0].get('api').",
        "```python", gsm_pattern(), "```",
        "", f"## What {analysis.file_name} misses today", *(gaps or ["- nothing obvious: tidy it to match the example"]),
        "", "## Google access that is not ADC (replace every one, see the credentials rule)",
        *([f"- {c}" for c in analysis.credentials] or ["- none found"]),
        "", f"## The script: {analysis.file_name}", "```python", source.rstrip(), "```",
        "", "## Its YAML", "```yaml", (config_yaml or "(none given)").rstrip(), "```",
    ])


# ====================================================================== guards
def _fill(text: str, values: dict[str, str]) -> str:
    return VAR_TOKEN_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _same_sql(a: str, b: str) -> bool:
    if " ".join(a.split()) == " ".join(b.split()):
        return True
    return sql_utils.equivalent(a, b)[0]


def compare_sql(before: list[SqlBlock], after: list[SqlBlock], values_before: dict[str, str],
                values_after: dict[str, str]) -> list[str]:
    """Problems when the SQL texts differ (each SQL before must match one after, and none may be new). Each side fills
    in its own YAML's lab values first, then the other side's: a name only one YAML knows still compares."""
    left = [(b, _fill(b.text, {**values_after, **values_before})) for b in before]
    right = [(b, _fill(b.text, {**values_before, **values_after})) for b in after]
    problems = []
    for block, text in left:
        match = next((i for i, (_b, t) in enumerate(right) if _same_sql(text, t)), None)
        if match is None:
            problems.append(f"SQL changed or removed (line {block.line}): {' '.join(block.text.split())[:100]}")
        else:
            right.pop(match)
    problems += [f"new SQL (line {b.line}): {' '.join(b.text.split())[:100]}" for b, _t in right]
    return problems


def requirements_for(script: str, stem: str) -> str:
    tree = ast.parse(script)
    modules: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            modules += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            modules += [f"{n.module}.{a.name}" for a in n.names] + [n.module]
    lines = [f"# Runtime dependencies for {stem}.py (the team modules come from /data/data_eng/py_scripts/modules)"]
    wanted = ["google-auth>=2.30", "PyYAML>=6.0"]
    for module in modules:
        top = module.split(".")[0]
        if top in sys.stdlib_module_names or top in ("modules", "__future__"):
            continue
        requirement = next((r for name, r in REQUIREMENTS.items() if module == name or module.startswith(name + ".")), None)
        if requirement is None and top != "google":
            requirement = f"{top}  # check the package name and version"
        if requirement:
            wanted.append(requirement)
    if "load_table_from_dataframe" in script or "to_dataframe" in script:
        wanted += ["pyarrow>=14.0", "db-dtypes>=1.2"]
    return "\n".join(lines + sorted(dict.fromkeys(wanted), key=str.lower)) + "\n"


def strip_module_header(source: str) -> str:
    """The script without its leading module docstring (the header block); other lines stay."""
    tree = ast.parse(source)
    first = tree.body[0] if tree.body else None
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)):
        return source
    lines = source.splitlines(keepends=True)
    return ("".join(lines[: first.lineno - 1]) + "".join(lines[first.end_lineno:])).lstrip("\n")


def with_header(script: str, header: EnhanceHeader, *, stem: str, file_name: str, envs: list[str], model: str,
                today: dt.date) -> str:
    """The script with the hub's header block on top: the values confirmed on the Enhance page, the steps main() calls,
    the run commands and the Updates lines (a header Gemini wrote is replaced)."""
    body = strip_module_header(script)
    tree = ast.parse(body)
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    order, _email = find_step_order(tree, functions)
    steps = [step_line(f"{i}.", name, first_line(ast.get_docstring(functions[name])) or name)
             for i, name in enumerate(order, 1)]
    block = header_block(
        authors=header.authors, reviewers=header.reviewers,
        created_on=header.created_on or "TODO: date of the first deployment (YYYY-MM-DD)", jira=header.jira,
        description=header.description, steps=steps, usage=usage_lines(stem, None, envs),
        updates=[(u.get("date", ""), u.get("pid", ""), u.get("note", "")) for u in header.all_updates(today)],
        footer=[f"Enhanced by {GENERATOR} on {today.isoformat()} from {file_name} (Gemini {model}; checked by the hub)."],
    )
    return format_python(block + "\n\n" + body)


def rewrite_script(analysis: ScriptAnalysis, source: str, gemini: Any, *, config_yaml: str | None = None,
                   settings: Settings | None = None, header: EnhanceHeader | None = None,
                   today: dt.date | None = None) -> RewriteResult:
    """Gemini's rewrite, kept only when every guard passes. ``header``: the header block confirmed on the Enhance page
    (default: the old script's); the hub writes it on top of the rewrite itself."""
    settings = settings or get_settings()
    header = header or EnhanceHeader.from_analysis(analysis)
    today = today or dt.date.today()
    stem = analysis.stem
    result = RewriteResult(ok=False, stem=stem, standards_before=analysis.standards)
    if analysis.parse_error:
        result.problems.append(f"the script does not parse ({analysis.parse_error}): fix that first")
        return result
    if gemini is None:
        result.problems.append("rewriting needs Gemini, which is turned off on this hub (SF_GEMINI_ENABLED / SF_GCP_PROJECT)")
        return result
    try:
        proposal, _usage = gemini.generate_structured(
            build_prompt(analysis, source, config_yaml, settings), RewriteProposal,
            system_instruction=PROMPT_PATH.read_text(encoding="utf-8"), model=settings.gemini_model)
    except GeminiError as exc:
        result.problems.append(f"Gemini could not rewrite the script: {exc}")
        return result
    result.model = settings.gemini_model
    result.changes, result.todos = proposal.changes, proposal.todos
    checks = result.checks

    config_before = analysis.config
    new_yaml = proposal.config_yaml if proposal.config_yaml.strip() else (config_yaml or "")
    try:
        config_after, _notes = parse_config_yaml(new_yaml)
        same_envs = not config_before or set(config_after) == set(config_before)
        yaml_check = GuardCheck(name="YAML layout", ok=same_envs, detail="" if same_envs else
                                f"environments changed: {sorted(config_before)} → {sorted(config_after)}")
    except ValueError as exc:
        config_after = {}
        yaml_check = GuardCheck(name="YAML layout", ok=not new_yaml.strip(), detail=str(exc))
    envs = [e for e in ("lab", "value", "qa") if e in config_after] or ["lab", "value"]

    try:
        script = with_header(format_python(proposal.script), header, stem=stem, file_name=analysis.file_name, envs=envs,
                             model=result.model, today=today)
        ast.parse(script)
        checks.append(GuardCheck(name="python syntax", ok=True))
    except Exception as exc:  # noqa: BLE001
        checks.append(GuardCheck(name="python syntax", ok=False, detail=str(exc).splitlines()[0][:300]))
        result.problems = [f"{c.name}: {c.detail}" for c in checks if not c.ok]
        return result
    lint = _ruff_check({f"{stem}.py": script}, [f"{stem}.py"])
    checks.append(GuardCheck(name="ruff lint (E9, F)", ok=lint.ok, detail=lint.detail))
    checks.append(yaml_check)

    sql_problems = compare_sql(module_sql_blocks(source), module_sql_blocks(script),
                               lab_values(source, config_before), lab_values(script, config_after))
    checks.append(GuardCheck(name="SQL unchanged", ok=not sql_problems, detail="; ".join(sql_problems[:5])))

    old_fns = {n.name for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    new_fns = {n.name for n in ast.parse(script).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    explained = " ".join(proposal.changes)
    lost = sorted(f for f in old_fns - new_fns if f not in explained)
    checks.append(GuardCheck(name="functions kept", ok=not lost, detail=f"missing without a word in the changes: {', '.join(lost)}" if lost else ""))

    remaining = credential_findings([ast.parse(script)])
    adc = "google.auth.default()" in script
    checks.append(GuardCheck(name="ADC credentials", ok=adc and not remaining,
                             detail="still uses " + "; ".join(remaining[:4]) if remaining else
                             ("" if adc else "google.auth.default() is not used")))

    after = scan_script(script, f"{stem}.py", config_yaml=new_yaml or None, settings=settings)
    result.standards_after = after.standards
    result.diff = "".join(difflib.unified_diff(source.splitlines(keepends=True), script.splitlines(keepends=True),
                                               fromfile=f"{stem}.py (before)", tofile=f"{stem}.py (enhanced)"))
    result.ok = all(c.ok for c in checks)
    result.problems = [f"{c.name}: {c.detail}" for c in checks if not c.ok]
    if not result.ok:
        log.info("rewrite of %s rejected: %s", stem, result.problems)
        return result
    files = {f"{stem}.py": script}
    if new_yaml.strip():
        files[f"{stem}.yaml"] = new_yaml if new_yaml.endswith("\n") else new_yaml + "\n"
    files["requirements.txt"] = requirements_for(script, stem)
    files["README.md"] = jinja_env().get_template("enhance_README.md.j2").render(
        stem=stem, file_name=analysis.file_name, description=header.description or after.header.get("description", ""),
        generator=GENERATOR, today=today.isoformat(), model=result.model,
        authors=", ".join(header.authors), reviewers=", ".join(header.reviewers), jira=header.jira,
        credentials=analysis.credentials,
        run_lines=[run_command(stem, None, env) for env in envs], changes=result.changes, todos=result.todos,
        standards=list(zip(result.standards_before, _aligned(result.standards_before, result.standards_after))),
        checks=checks, has_yaml=f"{stem}.yaml" in files)
    result.files = files
    return result


def _aligned(before: list, after: list) -> list:
    by_key = {s.key: s for s in after}
    return [by_key.get(s.key) for s in before]
