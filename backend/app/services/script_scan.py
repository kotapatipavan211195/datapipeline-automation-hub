"""Read an existing pipeline script (and its YAML files) without running it.

Everything here is static (``ast`` + regex, no AI): the ``'''`` header, the step functions in the order ``main()``
calls them, the SQL each one runs, the YAML keys it reads, what kind of hub step it is, and how far the script is from
the team's standard. ``script_convert`` turns a fully mapped analysis into a hub process; ``script_rewrite`` checks a
Gemini rewrite of the rest against the same facts.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import subprocess
import sys
from pathlib import PurePath
from typing import Any

import yaml

from ..models.enhance import FunctionInfo, ScriptAnalysis, SqlBlock, StandardsItem
from ..models.spec import ENVIRONMENTS
from ..settings import Settings, get_settings

SQL_START_RE = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*"
    r"(select|with|insert|update|delete|merge|create|drop|truncate|declare|begin|alter|export|call)\b",
    re.I | re.S,
)
HEADER_LABELS = {"Author(s)": "authors", "Reviewer(s)": "reviewers", "Created on": "created_on", "JIRA/SN #": "jira",
                 "Description": "description", "Usage": "usage", "Updates": "updates"}
# Other spellings older headers use (matched case-insensitively): their values are read; the standards check still
# asks for the team's labels above.
HEADER_SYNONYMS = {
    "authors": ["author(s)", "authors", "author", "developer(s)", "developers", "developer", "created by", "written by"],
    "reviewers": ["reviewer(s)", "reviewers", "reviewer", "reviewed by", "peer reviewer"],
    "created_on": ["created on", "date created", "creation date", "created date", "create date", "created", "date"],
    "jira": ["jira/sn #", "jira/sn#", "jira #", "jira#", "jira", "sn #", "sn#", "service now #", "servicenow #", "ticket",
             "story"],
    "description": ["description", "purpose", "summary", "desc"],
    "usage": ["usage", "how to run"],
    "updates": ["updates", "update history", "modification history", "modifications", "revision history", "change log",
                "changelog", "changes", "history"],
}
HEADER_LINE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(label) for label in sorted({x for names in HEADER_SYNONYMS.values() for x in names},
                                                             key=len, reverse=True))
    + r")(?:\s*:|\s+-\s)\s*(.*)$", re.I)  # "Author: x" or "Author - x"
UPDATE_RE = re.compile(r"^\[\s*(\d{4}-\d{2}-\d{2})\s*,\s*([^\]]*)\]\s*:?\s*(.*)$")
LOOSE_UPDATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\s*[,:\-]?\s*([A-Za-z][A-Za-z0-9_]*)\s*[,:\-]?\s*(.*)$")
# Functions the team template (and the hub) use around the steps: never steps themselves.
HELPER_NAMES = frozenset("""
    main email send_email stream_file stream_csv collect_attachments get_gsm_secret process_file load_to_bq
    merge_to_main_table insert_to_main_table archive_data_file
""".split())
UNSUPPORTED_MODULES = {
    "jaydebeapi": "a JDBC source-system connection (jaydebeapi)", "pyodbc": "an ODBC connection (pyodbc)",
    "cx_Oracle": "an Oracle connection", "oracledb": "an Oracle connection", "psycopg2": "a PostgreSQL connection",
    "pymysql": "a MySQL connection", "sqlalchemy": "a SQLAlchemy connection", "requests": "HTTP calls (requests)",
    "paramiko": "SFTP/SSH (paramiko)", "pysftp": "SFTP (pysftp)", "ftplib": "FTP (ftplib)", "boto3": "AWS (boto3)",
    "snowflake": "Snowflake", "openpyxl": "Excel files (openpyxl)", "xlsxwriter": "Excel files (xlsxwriter)",
    "pandas_gbq": "pandas-gbq",
}
RESULT_ATTRS = {"to_dataframe", "to_arrow", "to_geodataframe", "to_dataframe_iterable"}
LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
HARDCODED_PROJECT_RE = re.compile(r"`([a-z][a-z0-9\-]{4,61}[a-z0-9])\.[A-Za-z0-9_]+\.[A-Za-z0-9_\-*]+`")
# Google access that is not ADC (the team standard: google.auth.default() = your IAM identity in lab, the Composer
# service account in value): call name → what it is
KEY_CALLS = {
    "from_service_account_file": "a service-account key file",
    "from_service_account_json": "a service-account key file",
    "from_service_account_info": "a service-account key (JSON)",
    "from_json_keyfile_name": "a service-account key file (oauth2client)",
    "from_json_keyfile_dict": "a service-account key (oauth2client)",
    "load_credentials_from_file": "a credentials file",
}
GAC = "GOOGLE_APPLICATION_CREDENTIALS"
CLIENT_CALLS = ("Client", "default", "Credentials", "SecretManagerServiceClient")  # of google modules: bigquery.Client(…) …
SECRET_CALLS = ("google_secret", "access_secret_version", "get_gsm_secret")


# ====================================================================== small AST helpers
def dotted(node: ast.AST) -> str:
    """bq_client.query, subprocess.run, pd.read_csv, config_data.get … ('' when it is not a plain name chain)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else f"?.{node.attr}"
    if isinstance(node, ast.Call):
        return dotted(node.func) + "()"
    return ""


def calls_in(nodes: list[ast.AST]) -> list[ast.Call]:
    out = [n for root in nodes for n in ast.walk(root) if isinstance(n, ast.Call)]
    return sorted(out, key=lambda c: (c.lineno, c.col_offset))


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(root) for child in ast.iter_child_nodes(parent)}


def _inside(node: ast.AST, parents: dict[ast.AST, ast.AST], kinds: tuple[type, ...]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, kinds):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return False


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def first_line(doc: str | None) -> str:
    lines = (doc or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def looks_like_sql(text: str) -> bool:
    return bool(SQL_START_RE.match(text or "")) and len(text.split()) >= 2


def credential_findings(roots: list[ast.AST]) -> list[str]:
    """Google access that is not ADC: service-account keys, impersonation, GOOGLE_APPLICATION_CREDENTIALS set in code,
    gcloud auth activate-service-account, oauth2client, pandas-gbq private keys ("line 12: …", in line order)."""
    found: list[tuple[int, str]] = []
    for root in roots:
        parents = _parents(root)
        for n in ast.walk(root):
            line = getattr(n, "lineno", 0)
            if isinstance(n, ast.Call):
                name = dotted(n.func)
                last = name.split(".")[-1]
                if last in KEY_CALLS:
                    found.append((line, f"{KEY_CALLS[last]} ({last})"))
                elif name.endswith("impersonated_credentials.Credentials"):
                    found.append((line, "impersonated service-account credentials"))
                elif last in ("read_gbq", "to_gbq") and _kwarg(n, "private_key") is not None:
                    found.append((line, f"a service-account key passed to pandas-gbq ({last}(private_key=…))"))
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
                if any(m.split(".")[0] == "oauth2client" for m in modules):
                    found.append((line, "oauth2client credentials"))
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                text = n.value
                if text == GAC:  # set (os.environ[...] = …, setdefault, putenv, {…: …}); reading it is fine
                    parent = parents.get(n)
                    sets = ((isinstance(parent, ast.Subscript) and isinstance(parent.ctx, ast.Store))
                            or isinstance(parent, ast.Dict)
                            or (isinstance(parent, ast.Call) and dotted(parent.func).split(".")[-1] in ("setdefault", "putenv")))
                    if sets:
                        found.append((line, f"{GAC} set in the code (a key file)"))
                elif "activate-service-account" in text or "--key-file" in text:
                    found.append((line, "gcloud auth activate-service-account (a key file)"))
    return [f"line {line}: {what}" for line, what in sorted(set(found))]


# ====================================================================== strings → templates
class Unresolved(Exception):
    """A string the scan cannot rebuild statically (the reason says why)."""


def string_parts(node: ast.AST, scope: list[ast.AST], module: ast.Module, before: int | None = None) -> list[tuple[str, str]]:
    """Rebuild a string expression as [("text", s) | ("name", n)]: constants, f-strings whose fields are plain names,
    + concatenation, and names assigned earlier in the same function (or at module level)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [("text", node.value)]
    if isinstance(node, ast.JoinedStr):
        parts: list[tuple[str, str]] = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(("text", str(v.value)))
            elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.conversion == -1 and v.format_spec is None:
                parts.append(("name", v.value.id))
            else:
                raise Unresolved(f"the f-string field {{{ast.unparse(v.value) if isinstance(v, ast.FormattedValue) else '?'}}} "
                                 "is not a plain name")
        return parts
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return string_parts(node.left, scope, module, before) + string_parts(node.right, scope, module, before)
    if isinstance(node, ast.Name):
        value = _assigned(node.id, scope, before if before is not None else getattr(node, "lineno", None))
        if value is None:
            value = _assigned(node.id, [module], None, top_level=True)
        if value is None:
            return [("name", node.id)]  # a parameter or a value read from the config
        return string_parts(value, scope, module, getattr(value, "lineno", None))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        raise Unresolved("the text is built with str.format()")
    raise Unresolved(f"the text is built from a Python expression ({ast.unparse(node)[:60]})")


def _assigned(name: str, scope: list[ast.AST], before: int | None, top_level: bool = False) -> ast.AST | None:
    """The value last assigned to ``name`` before line ``before`` (None = not assigned there)."""
    best: tuple[int, ast.AST] | None = None
    for root in scope:
        nodes = root.body if top_level and isinstance(root, ast.Module) else list(ast.walk(root))
        for n in nodes:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
                value = n.value
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name and n.value is not None:
                value = n.value
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
                if before is None or n.lineno < before:
                    raise Unresolved(f"{name} is built step by step (+=)")
                continue
            else:
                continue
            if (before is None or n.lineno < before) and (best is None or n.lineno > best[0]):
                best = (n.lineno, value)
    return best[1] if best else None


def render_parts(parts: list[tuple[str, str]], style: str = "sql") -> str:
    """${name} for SQL, {name} for path/table templates."""
    opener = "${" if style == "sql" else "{"
    return "".join(text if kind == "text" else f"{opener}{text}}}" for kind, text in parts)


# ====================================================================== config lookups
def config_paths(scopes: list[ast.AST]) -> dict[str, list[str]]:
    """Which YAML key each local name holds: write_project = config_data.get('write_project') → ['write_project'];
    data_sets = config_data.get('data_sets', {}); ds = data_sets.get('x') → ['data_sets', 'x']."""
    roots = {"config_data", "config", "cfg", "conf"}
    paths: dict[str, list[str]] = {}
    assigns = sorted((n for s in scopes for n in ast.walk(s) if isinstance(n, ast.Assign)
                      and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)), key=lambda n: n.lineno)
    for n in assigns:
        target, value = n.targets[0].id, n.value
        if isinstance(value, ast.Call) and dotted(value.func).endswith("read_configuration"):
            roots.add(target)
            continue
        if isinstance(value, ast.ListComp):  # [s.format(date=…) for s in block.get('sources', [])]
            value = value.generators[0].iter
        while isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr in ("format", "strip"):
            value = value.func.value  # block.get('destination').format(date=…)
        key = base = None
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "get" and value.args:
            key, base = _const_str(value.args[0]), value.func.value
        elif isinstance(value, ast.Subscript):
            key, base = _const_str(value.slice), value.value
        if key is None or not isinstance(base, ast.Name):
            continue
        if base.id in roots:
            paths[target] = [key]
        elif base.id in paths:
            paths[target] = paths[base.id] + [key]
    return paths


def yaml_lookup(block: dict[str, Any], path: list[str]) -> Any:
    value: Any = block
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def name_values(name: str, paths: dict[str, list[str]], config: dict[str, dict[str, Any]], env: str) -> Any:
    """The value a local name has in one environment: through its YAML path, else a key of that name (top level or
    under data_sets/datasets). None = unknown."""
    block = config.get(env) or {}
    if name in paths:
        return yaml_lookup(block, paths[name])
    for path in ([name], ["data_sets", name], ["datasets", name]):
        value = yaml_lookup(block, path)
        if value is not None:
            return value
    return None


# ====================================================================== header
def _header_text(tree: ast.Module, source: str) -> str | None:
    """The module docstring, else the leading # comment block (after a shebang / encoding line)."""
    doc = ast.get_docstring(tree, clean=False) if tree.body else None
    if doc:
        return doc
    lines: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped and not lines:
            continue
        if not stripped.startswith("#"):
            break
        if stripped.startswith("#!") or re.match(r"#.*\b(coding|vim)[:=]", stripped):
            continue
        lines.append(stripped.lstrip("#").strip(" =-*"))
    return "\n".join(lines) or None


def _iso_date(text: str) -> str | None:
    """2024-05-01 (or 05/01/2024, month first) as YYYY-MM-DD; None when it is no date."""
    text = text.strip()
    try:
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
            month, day, year = (int(x) for x in text.split("/"))
            return dt.date(year, month, day).isoformat()
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def parse_header(tree: ast.Module, source: str = "") -> dict[str, Any]:
    """The header block's values: Author(s), Reviewer(s), Created on, JIRA/SN #, Description, Updates. Older spellings
    (Author:, Reviewed by:, Purpose:, a # comment block …) are read too; ``labels`` holds only the team's exact labels,
    for the standards check."""
    doc = _header_text(tree, source)
    if not doc:
        return {}
    synonyms = {name: key for key, names in HEADER_SYNONYMS.items() for name in names}
    exact = {label.lower(): label for label in HEADER_LABELS}
    fields: dict[str, list[str]] = {}
    labels: list[str] = []
    current = None
    for line in doc.splitlines():
        m = HEADER_LINE_RE.match(line)
        if m:
            label = " ".join(m.group(1).lower().split())
            current = synonyms[label]
            if label in exact and exact[label] not in labels:
                labels.append(exact[label])
            fields.setdefault(current, []).append(m.group(2).strip())
        elif current and line.strip():
            if line.strip().startswith(("Steps (main()", "Generated by", "ADC identity", "Enhanced by")):
                current = None  # hub-generated extras
                continue
            fields[current].append(line.strip())
        elif current in ("description",) and not line.strip():
            fields[current].append("")
    if not fields:
        docstring = ast.get_docstring(tree)
        if not docstring:
            return {}  # a comment block that is no header (a licence, a note)
        first = docstring.strip().split("\n\n")[0]  # a docstring without labels: its first paragraph describes the script
        return {"labels": [], "authors": [], "reviewers": [], "jira": "", "created_on": None,
                "description": " ".join(first.split()), "updates": [], "todos": 0}

    def clean(values: list[str]) -> list[str]:
        return [v for v in values if v and "TODO" not in v and v != "[YYYY-MM-DD, PID]:"]

    out: dict[str, Any] = {"labels": [k for k in HEADER_LABELS if k in labels]}
    for key in ("authors", "reviewers"):
        out[key] = list(dict.fromkeys(p.strip() for v in clean(fields.get(key, []))
                                      for p in re.split(r"[,;]|\s+and\s+", v) if p.strip()))
    jira = clean(fields.get("jira", []))
    out["jira"] = jira[0] if jira else ""
    created = clean(fields.get("created_on", []))
    out["created_on"] = _iso_date(created[0].split()[0]) if created else None
    out["description"] = " ".join(" ".join(clean(fields.get("description", []))).split())
    updates = []
    for v in clean(fields.get("updates", [])):
        m = UPDATE_RE.match(v)
        if m:
            updates.append({"date": m.group(1), "pid": m.group(2).strip(), "note": m.group(3).strip()})
            continue
        m = LOOSE_UPDATE_RE.match(v)  # 2024-05-01 P12345 note, 05/01/2024 - P12345 - note
        if m and _iso_date(m.group(1)):
            updates.append({"date": _iso_date(m.group(1)), "pid": m.group(2), "note": m.group(3).strip()})
    out["updates"] = updates
    out["todos"] = sum(1 for values in fields.values() for v in values if "TODO" in v)
    return out


# ====================================================================== YAML files
def parse_config_yaml(text: str | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """{environment: block} from gcp_projects, and notes. Raises ValueError when it is not the team's layout."""
    if not text or not text.strip():
        return {}, []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"the YAML does not parse: {str(exc).splitlines()[0]}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("gcp_projects"), dict):
        raise ValueError("the YAML has no gcp_projects block (one block per environment: lab, value[, qa])")
    config, notes = {}, []
    for env, block in data["gcp_projects"].items():
        if env in ENVIRONMENTS and isinstance(block, dict):
            config[env] = block
        else:
            notes.append(f"YAML block gcp_projects.{env} was ignored (the hub knows lab, value and qa)")
    if "lab" not in config:
        raise ValueError("the YAML has no gcp_projects.lab block")
    return config, notes


def parse_schema_yaml(text: str | None) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"the table schema YAML does not parse: {str(exc).splitlines()[0]}") from exc
    definition = data.get("table_definition") if isinstance(data, dict) else None
    if not isinstance(definition, dict) or not isinstance(definition.get("table_schema"), dict):
        raise ValueError("the table schema YAML has no table_definition.table_schema")
    return definition


def classify_yaml(text: str) -> str:
    """'config' (gcp_projects), 'schema' (table_definition) or '' for an uploaded YAML file."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return ""
    if isinstance(data, dict) and "gcp_projects" in data:
        return "config"
    if isinstance(data, dict) and "table_definition" in data:
        return "schema"
    return ""


# ====================================================================== steps and their kinds
def find_step_order(tree: ast.Module, functions: dict[str, ast.FunctionDef]) -> tuple[list[str], dict[str, Any]]:
    """Step functions in the order main() calls them, plus how main() sends email."""
    main = functions.get("main")
    email = {"success": False, "failure": False, "no_file": False, "attach": []}
    if main is not None:
        body: list[ast.AST] = [main]
    else:
        guard = next((n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)), None)
        body = [guard] if guard else []
    order: list[str] = []
    parents = _parents(main) if main is not None else {}
    for call in calls_in(body):
        name = call.func.id if isinstance(call.func, ast.Name) else None
        if name is None or name not in functions:
            continue
        if name == "email" or "email" in name:
            status = next((_const_str(a) for a in call.args if _const_str(a)), "") or ""
            in_except = main is not None and _inside(call, parents, (ast.ExceptHandler,))
            if in_except or "FAIL" in status.upper():
                email["failure"] = True
            else:
                email["success"] = True
            continue
        if name in HELPER_NAMES or name in order:
            continue
        parent = parents.get(call)
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) and parent.func.attr == "append":
            email["attach"].append(name)
        order.append(name)
    if not order and main is None and not body:  # no main(): every function that does something, in file order
        order = [n for n in functions if n not in HELPER_NAMES]
    return order, email


def _helpers_of(fn: ast.FunctionDef, functions: dict[str, ast.FunctionDef], skip: set[str]) -> list[ast.FunctionDef]:
    seen: dict[str, ast.FunctionDef] = {}
    todo = [fn]
    while todo:
        current = todo.pop()
        for call in calls_in([current]):
            name = call.func.id if isinstance(call.func, ast.Name) else None
            if name and name in functions and name not in seen and name != fn.name and name not in skip:
                seen[name] = functions[name]
                todo.append(functions[name])
    return list(seen.values())


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name → top-level module it comes from (import jaydebeapi as jdb → jdb: jaydebeapi)."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                out[a.asname or a.name] = n.module.split(".")[0]
    return out


def _gcloud_command(call: ast.Call, scope: list[ast.AST], module: ast.Module) -> list[tuple[str, Any]] | None:
    """The argv of a gsutil / gcloud storage call as [(kind, value)]: ('text', 'cp'), ('parts', [...]), ('star', name)."""
    name = dotted(call.func)
    if not (name.startswith("subprocess.") or name == "os.system") or not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, (ast.List, ast.Tuple)):
        out = []
        for e in arg.elts:
            if isinstance(e, ast.Starred):
                out.append(("star", ast.unparse(e.value)))
            elif isinstance(e, ast.Name) and e.id in ("gcloud", "gsutil"):
                out.append(("text", e.id))
            else:
                try:
                    out.append(("parts", string_parts(e, scope, module, call.lineno)))
                except Unresolved:
                    out.append(("expr", ast.unparse(e)))
        words = [v if k == "text" else render_parts(v, "path") if k == "parts" else "" for k, v in out]
        return out if any(w in ("gsutil", "gcloud") or w.endswith(("/gsutil", "/gcloud")) for w in words) else None
    try:
        parts = string_parts(arg, scope, module, call.lineno)
    except Unresolved:
        return None
    text = render_parts(parts, "path")
    if not re.search(r"\b(gsutil|gcloud)\b", text):
        return None
    import shlex

    return [("parts", [("text", w)] if "{" not in w else _split_template(w)) for w in shlex.split(text)]


def _split_template(word: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    for literal, field in re.findall(r"([^{]*)(?:\{([A-Za-z_][A-Za-z0-9_]*)\})?", word):
        if literal:
            parts.append(("text", literal))
        if field:
            parts.append(("name", field))
    return parts


def classify(fn: ast.FunctionDef, functions: dict[str, ast.FunctionDef], tree: ast.Module,
             aliases: dict[str, str], main_paths: dict[str, list[str]]) -> FunctionInfo:
    info = FunctionInfo(name=fn.name, line=fn.lineno, doc=first_line(ast.get_docstring(fn)), is_step=True)
    email_helpers = {n for n in functions if "email" in n or n in ("stream_file", "stream_csv", "collect_attachments")}
    helpers = _helpers_of(fn, functions, email_helpers | {"main"})
    scope: list[ast.AST] = [fn, *helpers]
    calls = calls_in(scope)
    names = {n.id for s in scope for n in ast.walk(s) if isinstance(n, ast.Name)}
    paths = {**main_paths, **config_paths(scope)}
    info.details["config_paths"] = paths
    info.details["helpers"] = [h.name for h in helpers]

    for local, module in aliases.items():
        if local in names and module in UNSUPPORTED_MODULES:
            info.reasons.append(f"uses {UNSUPPORTED_MODULES[module]}: the hub has no step for it")
    dotted_calls = [dotted(c.func) for c in calls]
    if any(d.endswith(("read_gbq", "to_gbq")) for d in dotted_calls):
        info.reasons.append("reads or writes BigQuery through pandas-gbq")
    if any(d.split(".")[-1] in RESULT_ATTRS for d in dotted_calls):
        info.reasons.append("works with query results in Python (to_dataframe / to_arrow)")
    if any(isinstance(n, ast.Call) and dotted(n.func) == "open" and len(n.args) > 1 and "w" in (_const_str(n.args[1]) or "")
           for s in scope for n in ast.walk(s)):
        info.reasons.append("writes local files itself")

    loads = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr in
             ("load_table_from_dataframe", "load_table_from_uri", "load_table_from_file", "load_table_from_json")]
    extracts = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "extract_table"]
    copies = [cmd for c in calls if (cmd := _gcloud_command(c, scope, tree)) is not None]
    queries = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "query" and dotted(c.func) not in ("pd.read_sql", "pandas.read_sql")]
    smtp = any(d.startswith("smtplib.") for d in dotted_calls)

    kinds = [k for k, found in (("ingest", loads), ("export", extracts), ("copy", copies), ("sql", queries)) if found]
    if loads:
        info.kind = "ingest"
        _ingest_details(info, scope, calls, tree)
    elif len(kinds) > 1:
        info.kind = "unmapped"
        info.reasons.append(f"does several kinds of work in one function ({' and '.join(kinds)}): split it into steps")
    elif extracts:
        info.kind = "export"
        _export_details(info, fn, extracts[0], scope, calls, tree)
    elif copies:
        info.kind = "copy"
        info.details["commands"] = [[(k, v) for k, v in cmd] for cmd in copies]
        if len(copies) > 1:
            info.reasons.append("runs several gsutil / gcloud commands: split it into one copy job each")
    elif queries:
        info.kind = "sql"
        _sql_details(info, scope, queries, tree)
    elif smtp:
        info.kind, info.is_step = "email", False
    elif any(d.split(".")[-1] in SECRET_CALLS for d in dotted_calls):  # reads a Secret Manager secret, nothing else
        info.kind, info.is_step = "helper", False
        info.details["reads_secret"] = True
    elif credential_findings(scope) or any(d.split(".")[-1] in CLIENT_CALLS and aliases.get(d.split(".")[0]) == "google"
                                           for d in dotted_calls):
        info.kind, info.is_step = "auth", False  # builds credentials / clients: the standard script uses ADC instead
        info.details["credentials"] = credential_findings(scope)
    else:
        info.kind = "unmapped"
        info.reasons.append("does not run SQL, load files, export a table or copy files")
    if info.reasons and info.kind != "email":
        info.kind, info.is_step = "unmapped", True
    return info


def _sql_details(info: FunctionInfo, scope: list[ast.AST], queries: list[ast.Call], tree: ast.Module) -> None:
    fn = scope[0]
    parents = _parents(fn)
    for call in queries:
        arg = call.args[0] if call.args else _kwarg(call, "query")
        if arg is None:
            continue
        if _inside(call, parents, LOOPS):
            info.reasons.append(f"runs SQL in a loop (line {call.lineno})")
            continue
        try:
            parts = string_parts(arg, scope, tree, call.lineno)
        except Unresolved as exc:
            info.reasons.append(f"line {call.lineno}: {exc}")
            continue
        text = render_parts(parts)
        info.sql.append(SqlBlock(text=text, names=sorted({v for k, v in parts if k == "name"}), line=call.lineno))
        config = _kwarg(call, "job_config")
        if isinstance(config, ast.Name):
            config = _assigned(config.id, scope, call.lineno)
        if isinstance(config, ast.Call):
            _job_config(info, config, scope, tree)
    for c in calls_in(scope):
        if dotted(c.func).endswith("ArrayQueryParameter") or dotted(c.func).endswith("StructQueryParameter"):
            info.reasons.append("passes array/struct query parameters")


def _job_config(info: FunctionInfo, config: ast.Call, scope: list[ast.AST], tree: ast.Module) -> None:
    dest = _kwarg(config, "destination")
    if dest is not None:
        try:
            info.details["destination"] = render_parts(string_parts(dest, scope, tree, config.lineno), "path")
        except Unresolved as exc:
            info.reasons.append(f"query destination: {exc}")
        disposition = ast.unparse(_kwarg(config, "write_disposition") or ast.Constant("WRITE_EMPTY"))
        info.details["write_mode"] = "append" if "APPEND" in disposition else "truncate" if "TRUNCATE" in disposition else "empty"
    params = _kwarg(config, "query_parameters")
    for p in params.elts if isinstance(params, (ast.List, ast.Tuple)) else []:
        if not (isinstance(p, ast.Call) and dotted(p.func).endswith("ScalarQueryParameter") and len(p.args) >= 3):
            info.reasons.append(f"query parameter {ast.unparse(p)[:60]} is not a ScalarQueryParameter")
            continue
        name, bq_type = _const_str(p.args[0]), _const_str(p.args[1])
        source = _param_source(p.args[2])
        if not name or not bq_type or source is None:
            info.reasons.append(f"query parameter @{name or '?'} is computed in Python ({ast.unparse(p.args[2])[:60]})")
            continue
        info.details.setdefault("parameters", []).append({"name": name, "bq_type": bq_type.upper(), **source})


def _param_source(expr: ast.AST) -> dict[str, Any] | None:
    """date.today() → today; date.today() ± timedelta(days=N) → today_offset; .replace(day=1) → month_start."""
    text = ast.unparse(expr).replace("datetime.date.today()", "date.today()").replace(" ", "")
    text = re.sub(r"\.isoformat\(\)$|\.strftime\('%Y-%m-%d'\)$", "", text)
    if text in ("date.today()", "datetime.now().date()", "datetime.today().date()"):
        return {"source": "today", "offset_days": 0}
    m = re.fullmatch(r"\(?date\.today\(\)([+-])timedelta\(days=(\d+)\)\)?", text)
    if m:
        return {"source": "today_offset", "offset_days": int(m.group(2)) * (1 if m.group(1) == "+" else -1)}
    if text == "date.today().replace(day=1)":
        return {"source": "month_start", "offset_days": 0}
    if isinstance(expr, ast.Constant) and isinstance(expr.value, (str, int, float)) and not isinstance(expr.value, bool):
        return {"source": "config", "offset_days": 0, "default": str(expr.value)}
    return None


def _export_details(info: FunctionInfo, fn: ast.FunctionDef, call: ast.Call, scope: list[ast.AST], calls: list[ast.Call],
                    tree: ast.Module) -> None:
    d = info.details
    source = call.args[0] if call.args else _kwarg(call, "source")
    try:
        d["source_table"] = render_parts(string_parts(source, scope, tree, call.lineno), "path")
    except (Unresolved, TypeError) as exc:
        info.reasons.append(f"the table to export: {exc}")
    blocks = [p[0] for p in d["config_paths"].values() if len(p) == 2 and p[1] in ("bucket", "path", "file_name")]
    if blocks:
        d["config_key"] = blocks[0]
    else:
        info.reasons.append("the export destination is not read from a YAML block with bucket, path and file_name")
    for c in calls:
        if isinstance(c.func, ast.Attribute) and c.func.attr == "strftime" and _const_str(c.args[0] if c.args else None):
            d["date_suffix"] = _const_str(c.args[0])
            break
    texts = [n.value for s in scope for n in ast.walk(s) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    ext = next((t for t in texts if re.fullmatch(r"\.(csv|txt|json|jsonl|parquet|avro)(\.gz)?", t.lower())), "")
    fmt_node = next((c for c in calls if dotted(c.func).endswith("ExtractJobConfig")), None)
    fmt = "CSV"
    if fmt_node is not None:
        dest = _kwarg(fmt_node, "destination_format")
        dest_text = ast.unparse(dest) if dest is not None else "CSV"
        fmt = next((f for f in ("NEWLINE_DELIMITED_JSON", "PARQUET", "AVRO", "CSV") if f in dest_text), "CSV")
        delim = _const_str(_kwarg(fmt_node, "field_delimiter"))
        if delim:
            d["field_delimiter"] = delim
        header = _kwarg(fmt_node, "print_header")
        if isinstance(header, ast.Constant):
            d["print_header"] = bool(header.value)
        compression = _kwarg(fmt_node, "compression")
        if compression is not None:
            d["compression"] = next((c for c in ("GZIP", "DEFLATE", "SNAPPY", "ZSTD") if c in ast.unparse(compression)), "NONE")
    if fmt == "CSV" and ext.startswith(".txt"):
        fmt = "TXT"
    d["file_format"] = fmt
    if ext.endswith(".gz"):
        d["compression"] = "GZIP"
    d["sharded"] = any("*" in t for t in texts)  # name-*.csv: several files


def _ingest_details(info: FunctionInfo, scope: list[ast.AST], calls: list[ast.Call], tree: ast.Module) -> None:
    d = info.details
    dotted_calls = [dotted(c.func) for c in calls]
    reader = next((x.split(".")[-1] for x in dotted_calls
                   if x.split(".")[-1] in ("read_csv", "read_fwf", "read_json", "read_parquet", "read_excel")), None)
    if reader is None:
        info.reasons.append("loads data into BigQuery, but not from files read with pandas "
                            "(read_csv / read_fwf / read_json / read_parquet / read_excel)")
    d["reader"] = reader
    if reader == "read_csv":
        call = next(c for c in calls if dotted(c.func).endswith("read_csv"))
        sep = _kwarg(call, "sep") or _kwarg(call, "delimiter")
        d["separator_literal"] = _const_str(sep)
    if not any(x.endswith("list_blobs") for x in dotted_calls):
        info.reasons.append("loads files, but does not list them in a bucket (list_blobs)")
    texts = [n.value for s in scope for n in ast.walk(s) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    joined = "\n".join(texts) + "\n" + "\n".join(ast.unparse(s) for s in scope)
    merge = re.search(r"\bMERGE\b", joined, re.I)
    d["load_mode"] = "merge" if merge else ("truncate" if re.search(r"\bTRUNCATE\s+TABLE\b", joined, re.I) else "append")
    keys = re.findall(r"T\.`?(\w+)`?\s*=\s*S\.`?\1`?", joined)
    key_list = re.search(r"key_columns\s*=\s*\[([^\]]*)\]", joined)
    if key_list:
        keys = re.findall(r"['\"](\w+)['\"]", key_list.group(1)) or keys
    d["merge_keys"] = list(dict.fromkeys(keys))
    order = re.search(r"ORDER\s+BY\s+`?(\w+)`?\s+DESC", joined, re.I)
    d["merge_order_by"] = order.group(1) if order else None
    d["history"] = "_history" in joined
    d["archive"] = any(x.endswith(("copy_blob", "rename_blob")) for x in dotted_calls)
    d["no_file_email"] = any("NO FILE" in t.upper() for t in texts)
    d["audit_columns"] = "file_batch_id" in joined
    d["fails_without_files"] = bool(re.search(r"raise\s+\w*Error\(f?['\"]No (valid )?files", joined))


# ====================================================================== standards
def standards(tree: ast.Module, source: str, analysis: ScriptAnalysis, functions: dict[str, ast.FunctionDef],
              settings: Settings) -> list[StandardsItem]:
    items: list[StandardsItem] = []

    def add(key: str, ok: bool, title: str, detail: str = "") -> None:
        items.append(StandardsItem(key=key, ok=ok, title=title, detail=detail))

    header = analysis.header
    missing = [k for k in HEADER_LABELS if k not in header.get("labels", [])]
    add("header", bool(header) and not missing,
        "A ''' header block: Author(s), Reviewer(s), Created on, JIRA/SN #, Description, Usage, Updates",
        "no header block" if not header else (f"missing {', '.join(missing)}" if missing else
                                              (f"{header['todos']} TODO(s) left" if header.get("todos") else "")))
    inserted = re.findall(r"sys\.path\.insert\(\s*0\s*,\s*['\"]([^'\"]+)['\"]", source)
    lacking = [p for p in settings.module_paths if p not in inserted]
    add("module_paths", not lacking, "Module paths: sys.path.insert for the team's module folders",
        f"missing {', '.join(lacking)}" if lacking else "")
    add("config_module", "read_config_yaml" in source and "read_configuration(" in source,
        "Configuration read with modules.read_config_yaml (config.read_configuration(environment, args.cf))")
    add("logger", '"LOG_FORMAT"' in source.replace("'", '"') and "logging.basicConfig" in source and "getLogger" in source,
        "CONFIG dict (LOG_FORMAT, LOG_LEVEL, SCRIPT_NAME, EXEC_DATETIME) with logging.basicConfig and a logger")
    found = analysis.credentials
    add("credentials", "google.auth.default()" in source and not found,
        "Credentials from google.auth.default() (ADC/IAM: your identity in lab, the Composer service account in value; "
        "no service-account keys)",
        "; ".join(found[:4]) + (f" (+{len(found) - 4} more)" if len(found) > 4 else "") if found else
        ("" if "google.auth.default()" in source else "google.auth.default() not used"))
    steps = [functions[n] for n in analysis.step_order if n in functions]
    untimed = [f.name for f in steps if "execution begin" not in ast.unparse(f) or "execution end" not in ast.unparse(f)]
    add("step_timing", bool(steps) and not untimed, "One function per step, logging 'execution begin' and 'execution end: N seconds'",
        f"without timing: {', '.join(untimed)}" if untimed else ("" if steps else "no step functions found"))
    unguarded = [f.name for f in steps if not any(isinstance(n, ast.Try) and any(
        isinstance(r, ast.Raise) and "RuntimeError" in ast.unparse(r) for h in n.handlers for r in ast.walk(h)) for n in ast.walk(f))]
    add("step_errors", bool(steps) and not unguarded, "Each step: try / except Exception as e → raise RuntimeError(str(e))",
        f"not wrapped: {', '.join(unguarded)}" if unguarded else "")
    exits = sorted({f.name for f in functions.values() if f.name != "main" for n in ast.walk(f)
                    if isinstance(n, ast.Call) and dotted(n.func) in ("sys.exit", "exit", "quit", "os._exit")})
    add("no_exit", not exits, "Helpers raise errors instead of calling sys.exit()", f"sys.exit() in {', '.join(exits)}" if exits else "")
    main = functions.get("main")
    add("main", main is not None and [a.arg for a in main.args.args] == ["args"], "def main(args): reads the config, calls the steps in order",
        "" if main is not None else "no main() function")
    parser = re.search(r"add_argument\(\s*['\"]environment['\"][^)]*choices\s*=\s*\[([^\]]*)\]", source)
    envs_ok = bool(parser) and all(e in parser.group(1) for e in ("lab", "value"))
    add("argparse", envs_ok and re.search(r"add_argument\(\s*['\"]-cf['\"]", source) is not None,
        "argparse: environment (lab / value / qa) and -cf <config file>, environment given last: py ./x.py -cf ./x.yaml lab")
    prints = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and dotted(n.func) == "print")
    add("no_print", not prints, "Messages go to the logger, not print()", f"{prints} print() call(s)" if prints else "")
    hard = sorted({m for f in analysis.functions for b in f.sql for m in HARDCODED_PROJECT_RE.findall(b.text)})
    add("projects_in_config", not hard, "GCP projects come from the YAML, not hard-coded in SQL",
        f"hard-coded: {', '.join(hard[:5])}" if hard else "")
    if any(f.sql for f in analysis.functions):
        add("sql_logger", "sql_logger" in source and "write_to_log" in source, "SQL is captured with modules.sql_logger")
    unused = unused_imports(source, analysis.stem)
    add("imports", not unused, "Only the imports the script uses", f"unused: {', '.join(unused[:8])}" if unused else "")
    return items


def unused_imports(source: str, stem: str = "script") -> list[str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "F401", "--output-format", "concise",
             "--stdin-filename", f"{stem}.py", "-"],
            input=source, capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return re.findall(r"`([^`]+)` imported but unused", proc.stdout)


# ====================================================================== entry point
def scan_script(source: str, file_name: str = "script.py", *, config_yaml: str | None = None,
                schema_yaml: str | None = None, settings: Settings | None = None) -> ScriptAnalysis:
    settings = settings or get_settings()
    stem = PurePath(file_name or "script.py").stem
    analysis = ScriptAnalysis(file_name=file_name, stem=stem)
    try:
        analysis.config, notes = parse_config_yaml(config_yaml)
        analysis.notes += notes
        analysis.environments = list(analysis.config)
    except ValueError as exc:
        analysis.blockers.append(str(exc))
    try:
        analysis.table_schema = parse_schema_yaml(schema_yaml)
    except ValueError as exc:
        analysis.blockers.append(str(exc))
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        analysis.parse_error = f"line {exc.lineno}: {exc.msg}"
        analysis.blockers.insert(0, f"the script does not parse ({analysis.parse_error})")
        return analysis
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    analysis.header = parse_header(tree, source)
    order, analysis.email = find_step_order(tree, functions)
    mailers = [f for name, f in functions.items() if "email" in name or name in ("stream_file", "stream_csv", "collect_attachments")]
    analysis.email["blocks"] = sorted({p[0] for p in config_paths(mailers).values() if len(p) > 1 and p[0] != "email"})
    analysis.email["sends"] = bool(mailers) and any(dotted(c.func).startswith("smtplib.") for c in calls_in(mailers))
    aliases = _module_aliases(tree)
    main_paths = config_paths([functions["main"]]) if "main" in functions else {}
    analysis.credentials = credential_findings([tree])
    infos = {}
    for name in order:
        info = classify(functions[name], functions, tree, aliases, main_paths)
        infos[name] = info
        if info.kind == "auth":
            analysis.notes.append(f"{name}() builds credentials or clients: it is left out (the standard script uses "
                                  "ADC: google.auth.default())")
        elif info.details.get("reads_secret"):
            analysis.notes.append(f"{name}() reads a Secret Manager secret: it is left out (no hub step of this script "
                                  "uses it; add it under Variables & secrets if a custom step needs it)")
    analysis.step_order = [n for n in order if infos[n].is_step]
    for name, fn in functions.items():
        info = infos.get(name) or FunctionInfo(name=name, line=fn.lineno, doc=first_line(ast.get_docstring(fn)),
                                               kind="email" if any(dotted(c.func).startswith("smtplib.") for c in calls_in([fn]))
                                               else "helper")
        if name not in infos and name not in HELPER_NAMES and info.kind == "helper":
            if not any(isinstance(c.func, ast.Name) and c.func.id == name for f in functions.values() for c in calls_in([f])):
                analysis.notes.append(f"{name}() is never called: it is left out")
        analysis.functions.append(info)
    analysis.standards = standards(tree, source, analysis, functions, settings)
    steps = analysis.steps()
    unmapped = [f for f in steps if f.kind == "unmapped"]
    if not steps:
        analysis.blockers.append("no step functions found (main() calls none of the script's functions)")
    for f in unmapped:
        analysis.blockers.append(f"{f.name}(): " + "; ".join(f.reasons))
    ingests = [f for f in steps if f.kind == "ingest"]
    if len(ingests) > 1:
        analysis.blockers.append(f"{len(ingests)} file ingestions: the hub takes one per process (one -ts table schema)")
    for f in ingests:
        if analysis.table_schema is None:
            analysis.blockers.append(f"{f.name}() loads files: add its table schema YAML (the -ts file)")
        if not analysis.config:
            analysis.blockers.append(f"{f.name}() loads files: add its YAML (the gcs block with the bucket and file name)")
    analysis.convertible = not analysis.blockers
    return analysis


def module_sql_blocks(source: str) -> list[SqlBlock]:
    """Every SQL-looking string in a script (the rewrite guard compares these before and after)."""
    tree = ast.parse(source)
    out: list[SqlBlock] = []
    parents = _parents(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)
                                               and not isinstance(parents.get(node), ast.JoinedStr)):
            try:
                parts = string_parts(node, [], tree)
            except Unresolved:
                parts = [("text", ast.unparse(node))]
            text = render_parts(parts)
            if looks_like_sql(text):
                out.append(SqlBlock(text=text, names=sorted({v for k, v in parts if k == "name"}), line=getattr(node, "lineno", 0)))
    return sorted(out, key=lambda b: b.line)


def lab_values(source: str, config: dict[str, dict[str, Any]]) -> dict[str, str]:
    """{local name: lab value} for the names the script reads from its YAML (for comparing SQL before and after)."""
    tree = ast.parse(source)
    paths = config_paths([tree])
    names = set(paths) | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    out = {}
    for name in names:
        value = name_values(name, paths, config, "lab")
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out[name] = str(value)
    return out
