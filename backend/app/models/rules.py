"""Naming rules and template-token helpers shared by the spec model, renderer and API.

Two placeholder syntaxes exist on purpose:

* ``${name}`` — config variables inside **SQL text**. SQL may legitimately contain ``{``/``}``
  (regex quantifiers, JSON literals), so SQL placeholders need a distinct marker. The
  renderer turns them into ``{name}`` (a local read from the YAML) inside the generated f-string.
* ``{name}`` — variables inside **paths, bucket names and table names**. The renderer resolves them per
  environment (lab / value / qa) when it writes the YAML, so the YAML holds plain values like the team's
  configs. ``{date:%Y%m%d}`` (copy paths, email attachments) stays in the YAML and the script fills it in
  with today's date.
"""

from __future__ import annotations

import builtins
import datetime as dt
import keyword
import re
import string
import sys
from collections.abc import Mapping, Iterable

PROCESS_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
SCRIPT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
SECRET_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,255}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Values allowed for config variables (they are interpolated into SQL identifiers).
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
VAR_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]{1,220}[a-z0-9]$")
SA_EMAIL_RE = re.compile(r"^[a-z0-9\-]+@[a-z0-9\-]+\.iam\.gserviceaccount\.com$")
LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,62}$")
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_\-]{0,63}$")

CONFIG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Characters a value written into the YAML (bucket, folder, file name) must not contain.
UNSAFE_PATH_CHARS = ("'", '"', "\\", "\n", "\r", "`")

EMAIL_SUBJECT_TOKENS = {
    "status": "SUCCESS, FAILED or NO FILE",
    "script": "the script file name",
    "process": "the process name",
    "environment": "lab, value or qa",
    "date": "today's date (supports formats like {date:%Y-%m-%d})",
}

RUNTIME_TOKENS = {
    "date": "today's date when the script runs (supports formats like {date:%Y%m%d})",
}

# Names the generated script defines or imports — step functions must not shadow them.
GENERATED_SCRIPT_NAMES = frozenset(
    """
    main email send_email stream_file collect_attachments process_file load_to_bq merge_to_main_table
    insert_to_main_table archive_data_file get_gsm_secret config sql_logger gsm capture CONFIG SECRETS logger
    bq_client storage_client credentials project_id argparse ast io json logging os shutil smtplib subprocess
    sys time uuid warnings datetime date timedelta pd pandas google bigquery storage EmailMessage
    read_project write_project gsm_project data_sets config_data table_def environment args parser
    """.split()
)

# Module names a generated script must not be called: it would shadow what the script imports
# (python dl_script.py puts the script's folder first on sys.path).
RESERVED_SCRIPT_NAMES = frozenset(
    set(sys.stdlib_module_names)
    | {"yaml", "google", "pytest", "conftest", "setup", "pandas", "modules", "read_config_yaml", "sql_logger", "gsm_module"}
)
RESERVED_FUNCTION_NAMES = frozenset(set(dir(builtins)) | set(keyword.kwlist) | GENERATED_SCRIPT_NAMES)


def function_name_problem(name: str) -> str | None:
    """Return a human-readable problem with a proposed step function name, or None if valid."""
    if not name:
        return "function name is required"
    if not IDENTIFIER_RE.match(name):
        return f"'{name}' is not a valid Python function name (letters, digits, underscores; no leading digit)"
    if name != name.lower():
        return f"'{name}' should be lower_snake_case"
    if name.startswith("_"):
        return f"'{name}' must not start with an underscore"
    if keyword.iskeyword(name) or keyword.issoftkeyword(name):
        return f"'{name}' is a Python keyword"
    if name in RESERVED_FUNCTION_NAMES:
        return f"'{name}' clashes with a name the generated script already uses"
    if len(name) > 60:
        return f"'{name}' is longer than 60 characters"
    return None


def script_name_problem(name: str) -> str | None:
    """Return a problem with a proposed script file name (without .py), or None if valid."""
    if not SCRIPT_NAME_RE.match(name or ""):
        return "script name must be lower_snake_case, 3-63 characters, starting with a letter (e.g. dl_script)"
    if name in RESERVED_SCRIPT_NAMES:
        return f"'{name}' is a Python module name (standard library or a dependency); {name}.py would shadow that module"
    if name.startswith("test_"):
        return "script names starting with test_ are collected by pytest as tests; choose another name"
    return None


def to_snake_case(text: str, max_len: int = 60) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text or "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if s and s[0].isdigit():
        s = f"step_{s}"
    return s[:max_len].rstrip("_")


def suggest_function_name(base: str, taken: Iterable[str] = ()) -> str:
    taken_set = set(taken)
    name = to_snake_case(base) or "run_step_1"
    if function_name_problem(name):
        name = f"step_{name}"
    candidate, n = name, 2
    while candidate in taken_set or function_name_problem(candidate):
        candidate = f"{name}_{n}"
        n += 1
    return candidate


# ---------------------------------------------------------------- SQL ${var} tokens
def sql_var_tokens(sql: str) -> list[str]:
    return list(dict.fromkeys(VAR_TOKEN_RE.findall(sql or "")))


def substitute_sql_vars(sql: str, values: Mapping[str, str]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(key)
        return str(values[key])

    return VAR_TOKEN_RE.sub(repl, sql)


# ---------------------------------------------------------------- {token} templates
def template_fields(template: str) -> list[tuple[str, str]]:
    """List (field_name, format_spec) used in a ``{token}`` template. Raises ValueError if malformed."""
    out: list[tuple[str, str]] = []
    for _literal, field_name, format_spec, _conv in string.Formatter().parse(template or ""):
        if field_name is None:
            continue
        if field_name == "" or not IDENTIFIER_RE.match(field_name):
            raise ValueError(f"invalid placeholder '{{{field_name}}}' in {template!r}")
        out.append((field_name, format_spec or ""))
    return out


def template_problems(template: str, variable_names: Iterable[str], *, allow_date: bool = False) -> list[str]:
    """Problems with a ``{variable}`` template. ``{date:%Y%m%d}`` is only allowed where the script formats the
    value at run time (copy paths, email attachments)."""
    known = set(variable_names)
    try:
        fields = template_fields(template)
    except ValueError as exc:
        return [str(exc)]
    problems = []
    for name, spec in fields:
        if name == "date":
            if not allow_date:
                problems.append("'{date}' is not available here (only in copy paths and email attachments)")
            continue
        if name not in known:
            problems.append(f"unknown placeholder '{{{name}}}' (define it as a variable on 'Variables & secrets')")
        elif spec:
            problems.append(f"placeholder '{{{name}}}' does not take a format (only {{date}} does)")
    return problems


def resolve_template(template: str, variables: Mapping[str, str], *, today: dt.date | None = None) -> str:
    """Fill ``{variable}`` and ``{date:%fmt}`` (preview only; the YAML keeps ``{date}``)."""
    tokens: dict[str, object] = dict(variables)
    tokens["date"] = today or dt.date.today()
    return template.format_map(tokens)


def fill_variables(template: str, values: Mapping[str, str]) -> str:
    """Replace {variable} placeholders; keep {date:...} (and unknown names) exactly as written."""
    try:
        parts = list(string.Formatter().parse(template or ""))
    except ValueError:
        return template
    out = []
    for literal, name, spec, _conv in parts:
        out.append(literal)
        if name is None:
            continue
        if name in values and not spec and name != "date":
            out.append(str(values[name]))
        else:
            out.append("{" + name + (f":{spec}" if spec else "") + "}")
    return "".join(out)


def parse_receivers(text: str) -> tuple[list[str], list[str]]:
    """Split a comma-separated list of email addresses. Returns (addresses, problems)."""
    text = (text or "").strip()
    if not text:
        return [], []
    problems: list[str] = []
    if ";" in text:
        problems.append("separate addresses with commas, not semicolons")
    addresses: list[str] = []
    for raw in text.replace(";", ",").split(","):
        addr = raw.strip()
        if not addr:
            continue
        if any(ch.isspace() for ch in addr):
            problems.append(f"'{addr}' contains spaces: separate addresses with commas")
            continue
        if not EMAIL_RE.match(addr):
            problems.append(f"'{addr}' is not an email address")
            continue
        if addr.lower() not in {a.lower() for a in addresses}:
            addresses.append(addr)
    return addresses, problems


def split_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    rest = uri[5:]
    bucket, _, path = rest.partition("/")
    if not bucket:
        raise ValueError(f"missing bucket in {uri!r}")
    return bucket, path
