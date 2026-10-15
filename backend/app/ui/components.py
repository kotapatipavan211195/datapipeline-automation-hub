"""Reusable wizard widgets: path inputs with live preview, schema editor, parameter editor, SQL panel, reports."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from nicegui import run, ui

from ..models.rules import resolve_template, suggest_function_name, template_problems
from ..models.schema import BQ_TYPES, Column, columns_from_bq_json
from ..models.spec import Parameter
from ..services import sql_utils
from ..services.gcp_tools import dry_run_sql, infer_from_gcs, infer_from_upload
from ..services.schema_infer import (
    GUESS_ROWS,
    InferResult,
    columns_from_bq_table,
    columns_from_ddl,
    columns_from_json_text,
    columns_from_layout,
    fixed_width_rows,
    guess_types_with_bigquery,
    lab_guess_target,
    preview_fixed_width,
)
from ..services.sql_analyzer import scan_projects
from ..services.validators import gcp_error_text
from .state import WizardState
from .theme import help_icon, hint, human_bytes, level_icon

PARAM_SOURCES = {
    "today": "Today (the day it runs)",
    "today_offset": "Today ± N days",
    "month_start": "First day of this month",
    "month_end": "Last day of this month",
    "prev_month_start": "First day of last month",
    "prev_month_end": "Last day of last month",
    "year_start": "1 January",
    "config": "Value in the YAML",
    "process_name": "Process name",
}
PARAM_TYPES = ["DATE", "TIMESTAMP", "DATETIME", "STRING", "INT64", "FLOAT64", "NUMERIC", "BOOL"]
PATH_TOKENS = ["{date:%Y%m%d}", "{date:%Y_%m_%d}", "{date:%Y-%m-%d}"]
LARGE_SQL_LINES = 150  # longer SQL starts folded away behind its summary bar


def sample_date() -> dt.date:
    return dt.date.today()


def opt(value: Any) -> str:
    return "" if value is None else str(value)


def split_list(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def list_input(label: str, target: dict, key: str, *, placeholder: str = "", classes: str = "w-72") -> ui.input:
    """A comma-separated text box for a short list (PIDs, tags) stored as target[key] = [...]. What is typed stays as
    typed (no Enter needed, nothing is dropped on blur) and saved values show when the page is opened again — unlike a
    chip select, whose new values only count after Enter and whose saved values must be among its options."""
    return ui.input(label, value=", ".join(target.get(key) or []), placeholder=placeholder,
                    on_change=lambda e: target.__setitem__(key, split_list(e.value))).props("outlined dense").classes(classes)


# ====================================================================== path / table templates
def template_input(
    obj: dict, key: str, label: str, state: WizardState, *, hint_text: str = "", tokens: bool = True,
    placeholder: str = "", optional: bool = False, classes: str = "w-full", allow_date: bool = False,
) -> ui.input:
    """Input for a {variable} template (plus {date:%Y%m%d} where allowed) with a live per-environment preview.
    The preview only fills in values; nothing is read from GCP."""
    with ui.column().classes(f"gap-0 {classes}"):
        inp = ui.input(label, placeholder=placeholder).props("outlined dense").classes("w-full")
        inp.bind_value(obj, key, forward=(lambda v: v or None) if optional else None, backward=(lambda v: v or "") if optional else None)
        preview = ui.label().classes("sf-preview")

        def refresh_preview() -> None:
            value = obj.get(key) or ""
            if not value:
                preview.text = hint_text
                preview.classes(replace="sf-hint")
                return
            problems = template_problems(value, state.variable_values(), allow_date=allow_date)
            if problems:
                preview.text = "⚠ " + problems[0]
                preview.classes(replace="sf-problem")
                return
            try:
                parts = []
                for env in state.active_envs():
                    resolved = resolve_template(value, state.variable_values(env), today=sample_date())
                    parts.append(f"{env}: {resolved or '—'}")
                preview.text = "→ " + "   ·   ".join(parts) + ("   (today)" if "{date" in value else "")
                preview.classes(replace="sf-preview")
            except (KeyError, ValueError) as exc:
                preview.text = f"⚠ {exc}"
                preview.classes(replace="sf-problem")

        inp.on_value_change(lambda _e: refresh_preview())
        refresh_preview()
        if tokens:
            with ui.row().classes("gap-1 items-center"):
                ui.label("Insert:").classes("sf-hint")
                for token in PATH_TOKENS if allow_date else []:
                    ui.button(token, on_click=lambda t=token: _append(inp, obj, key, t)).props("flat dense no-caps size=sm").classes("sf-mono")
                var_names = [n for n in state.variable_values() if n]
                if var_names:
                    with ui.button("variable", icon="data_object").props("flat dense no-caps size=sm"):
                        with ui.menu():
                            for name in var_names:
                                ui.menu_item("{" + name + "}", on_click=lambda n=name: _append(inp, obj, key, "{" + n + "}"))
    return inp


def _append(inp: ui.input, obj: dict, key: str, text: str) -> None:
    obj[key] = (obj.get(key) or "") + text
    inp.value = obj[key]


# ====================================================================== schema editor
class SchemaEditor:
    MODES = ["NULLABLE", "REQUIRED", "REPEATED"]
    GUESS_FORMATS = ("CSV", "TXT", "XLSX")  # sample formats BigQuery's guess reads (fixed width: the layout check)

    def __init__(self, step: dict, state: WizardState, svc: Any):
        self.step, self.state, self.svc = step, state, svc
        self.json_mode = False

    @property
    def fixed(self) -> bool:
        return self.step.get("file_format") == "FIXED"

    def guess_target(self, dataset: str) -> tuple[str, str | None]:
        """Lab project.dataset for BigQuery's temporary table (refused where another environment writes)."""
        s = self.state
        others = {env: s.variable_values(env) for env in s.active_envs() if env != "lab"}
        prod = [self.step.get("staging_dataset") or "", self.step.get("target_dataset") or ""]
        return lab_guess_target(dataset, s.variable_values("lab"), others, prod)

    async def bigquery_guess(self, columns: list[Column], rows: list[list[str]], dataset: str) -> InferResult:
        target, problem = self.guess_target(dataset)
        if problem:
            raise ValueError(problem)
        return await run.io_bound(guess_types_with_bigquery, self.svc.clients.bigquery(target.split(".")[0]), target,
                                  columns, rows, location=self.svc.settings.bq_location)

    def guess_controls(self, guess: dict[str, Any]) -> None:
        """The "Let BigQuery guess" switch and its lab dataset (bound to one dict shared by the dialog's tabs)."""
        with ui.row().classes("items-center gap-2 w-full"):
            ui.switch(f"Let BigQuery guess the types (loads up to {GUESS_ROWS:,} rows into a temporary lab table)").bind_value(
                guess, "on").props("dense").mark("bq-guess")
            ui.input("Lab dataset").bind_value(guess, "dataset").props("dense outlined").classes("w-56 sf-mono")
            help_icon("BigQuery reads the sample rows with schema auto-detect, the hub copies the types it picked and deletes "
                      "the table at once. Only a lab dataset is used (the staging dataset by default); never one prod writes to. "
                      "Without it the hub's own rules type the columns.")

    def render(self) -> None:
        with ui.row().classes("items-center gap-2"):
            ui.button("Add column", icon="add", on_click=self.add).props("outline dense no-caps")
            ui.button("Import columns…", icon="download", on_click=self.open_import).props("unelevated dense no-caps")
            ui.switch("Edit as JSON", on_change=self.toggle_json).props("dense")
            help_icon("Columns in the order they appear in the file. CSV files are loaded by position, JSON/Parquet/Avro by name. "
                      "Nested RECORD columns can be edited in JSON mode. Fixed-width files also need each column's length.")
        self.body()
        self.layout_check()

    @ui.refreshable_method
    def layout_check(self) -> None:
        """Fixed width: split a sample file by the lengths, to see that every value lands in its column."""
        if not self.fixed:
            return

        async def on_upload(e) -> None:
            data = await e.file.read()
            result.clear()
            with result:
                try:
                    columns = [Column.model_validate(c) for c in self.step["columns"]]
                    rows, problems = await run.io_bound(
                        preview_fixed_width, data, columns, header=str(self.step.get("header") or "None"),
                        skiprows=str(self.step.get("skiprows") or "None"))
                except Exception as exc:  # noqa: BLE001
                    ui.label(f"Could not split the sample: {str(exc).splitlines()[-1]}").classes("sf-problem")
                    return
                for problem in problems:
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        level_icon("warn")
                        ui.label(problem).classes("text-sm")
                if rows:
                    if not problems:
                        with ui.row().classes("items-center gap-1"):
                            level_icon("ok")
                            ui.label(f"Every line of {e.file.name} is {sum(c.width or 0 for c in columns)} characters, as the layout says")
                    ui.table(columns=[{"name": c.name, "label": c.name, "field": c.name, "align": "left"} for c in columns],
                             rows=rows).props("dense flat").classes("w-full").mark("layout-preview")
                    with ui.row().classes("items-center gap-2"):
                        dataset = ui.input("Lab dataset", value=self.step.get("staging_dataset") or "").props(
                            "dense outlined").classes("w-56 sf-mono")
                        ui.button("Guess the types with BigQuery (lab)", icon="auto_fix_high",
                                  on_click=lambda: guess_types(data, columns, dataset.value)).props(
                            "outline dense no-caps").mark("layout-guess")
                    guessed = ui.column().classes("w-full gap-1")

                    async def guess_types(data: bytes, columns: list[Column], dataset: str) -> None:
                        guessed.clear()
                        try:
                            sample = await run.io_bound(fixed_width_rows, data, columns, header=str(self.step.get("header") or "None"),
                                                        skiprows=str(self.step.get("skiprows") or "None"))
                            g = await self.bigquery_guess(columns, sample, dataset)
                        except Exception as exc:  # noqa: BLE001
                            with guessed:
                                ui.label(f"BigQuery could not guess the types: {gcp_error_text(exc)}").classes("sf-problem")
                            return
                        for col, new in zip(self.step["columns"], g.columns):
                            col["type"] = new.type
                        self.body.refresh()
                        with guessed:
                            for w in [f"BigQuery typed the columns from {g.detected['rows_loaded']} line(s); the temporary "
                                      "lab table is deleted", *g.warnings]:
                                with ui.row().classes("items-center gap-1 no-wrap"):
                                    level_icon("ok" if w.startswith("BigQuery typed") else "warn")
                                    ui.label(w).classes("text-sm")

        with ui.row().classes("items-center gap-2 w-full"):
            ui.upload(on_upload=on_upload, auto_upload=True, label="Check the layout against a sample file",
                      max_file_size=self.svc.settings.max_upload_mb * 1024 * 1024).props("accept=.txt,.dat,.csv flat dense").classes("w-80")
            hint("The first rows are split by the lengths above, exactly as the script will read them. Nothing is stored.")
        result = ui.column().classes("w-full gap-1")

    def toggle_json(self, e) -> None:
        self.json_mode = bool(e.value)
        self.body.refresh()

    def add(self) -> None:
        self.step["columns"].append({"name": f"column_{len(self.step['columns']) + 1}", "type": "STRING", "mode": "NULLABLE", "description": "", "fields": []})
        self.body.refresh()

    def move(self, i: int, delta: int) -> None:
        cols = self.step["columns"]
        j = i + delta
        if 0 <= j < len(cols):
            cols[i], cols[j] = cols[j], cols[i]
            self.body.refresh()

    def remove(self, i: int) -> None:
        del self.step["columns"][i]
        self.body.refresh()

    @ui.refreshable_method
    def body(self) -> None:
        cols = self.step["columns"]
        if self.json_mode:
            editor = ui.codemirror(json.dumps(columns_to_bq_json_safe(cols), indent=2), language="JSON").classes("w-full h-72")

            def apply() -> None:
                try:
                    parsed = columns_from_bq_json(json.loads(editor.value))
                except (ValueError, json.JSONDecodeError) as exc:
                    ui.notify(f"Not applied: {exc}", type="negative")
                    return
                self.step["columns"] = [c.model_dump() for c in parsed]
                ui.notify(f"{len(parsed)} column(s) applied", type="positive")

            ui.button("Apply JSON", icon="check", on_click=apply).props("unelevated dense no-caps")
            return
        if not cols:
            hint("No columns yet. Use “Import columns…” to read them from a sample file, a CREATE TABLE statement, "
                 "a BigQuery JSON schema or an existing table, or add them one by one.")
            return
        fixed = self.fixed
        with ui.row().classes("w-full gap-2 sf-hint no-wrap"):
            ui.label("#").classes("w-6")
            ui.label("Column name").classes("w-48")
            if fixed:
                ui.label("Length").classes("w-24")
                ui.label("Positions").classes("w-24")
            ui.label("Type").classes("w-36")
            ui.label("Mode").classes("w-32")
            ui.label("Description").classes("flex-grow")
        positions: list[ui.label] = []
        total: list[ui.label] = []  # filled after the rows (the inputs report changes while they are built)

        def update_positions() -> None:
            start = 1
            for label, c in zip(positions, cols):
                width = c.get("width") or 0
                label.text = f"{start}–{start + width - 1}" if width else "—"
                start += width
            if total:
                total[0].text = f"Record length: {start - 1} characters per line"

        for i, col in enumerate(cols):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.label(str(i + 1)).classes("w-6 sf-muted")
                ui.input(placeholder="column_name").bind_value(col, "name").props("dense outlined").classes("w-48 sf-mono")
                if fixed:
                    ui.number(min=1, precision=0, on_change=lambda _e: update_positions()).bind_value(
                        col, "width", forward=lambda v: int(v) if v else None).props("dense outlined").classes("w-24").mark(f"col-width-{i}")
                    positions.append(ui.label().classes("w-24 sf-mono text-xs"))
                ui.select(list(BQ_TYPES)).bind_value(col, "type").props("dense outlined").classes("w-36")
                ui.select(self.MODES).bind_value(col, "mode").props("dense outlined").classes("w-32")
                if col.get("type") == "RECORD":
                    ui.label(f"{len(col.get('fields') or [])} nested field(s) — edit in JSON").classes("sf-hint w-40")
                ui.input(placeholder="description (optional)").bind_value(col, "description").props("dense outlined").classes("flex-grow")
                ui.button(icon="arrow_upward", on_click=lambda i=i: self.move(i, -1)).props("flat dense round size=sm")
                ui.button(icon="arrow_downward", on_click=lambda i=i: self.move(i, 1)).props("flat dense round size=sm")
                ui.button(icon="delete_outline", on_click=lambda i=i: self.remove(i)).props("flat dense round size=sm color=negative")
        if fixed:
            total.append(ui.label().classes("sf-preview").mark("record-length"))
            update_positions()

    # ------------------------------------------------------------------ import dialog
    def open_import(self) -> None:
        state, step, svc = self.state, self.step, self.svc
        project = state.variable_values("lab").get("write_project") or svc.settings.gcp_project or ""  # lab only
        result: dict[str, Any] = {}
        dialog = ui.dialog().props("maximized=false")
        with dialog, ui.card().classes("w-[900px] max-w-full"):
            ui.label("Import columns").classes("text-lg font-semibold")
            with ui.tabs().classes("w-full") as tabs:
                t_layout = ui.tab("Fixed-width layout")
                t_upload = ui.tab("Upload a sample file")
                t_gcs = ui.tab("Sample file in GCS")
                t_json = ui.tab("BigQuery JSON")
                t_ddl = ui.tab("CREATE TABLE")
                t_table = ui.tab("Existing table")
            preview_box = ui.column().classes("w-full")

            def show(columns: list[dict], warnings: list[str], rows: list[dict] | None = None,
                     hub: dict[str, str] | None = None) -> None:
                """``hub``: the hub's own types by column, shown next to BigQuery's guess."""
                result["columns"] = columns
                preview_box.clear()
                with preview_box:
                    ui.label(f"{len(columns)} column(s) found").classes("font-semibold")
                    for w in warnings:
                        with ui.row().classes("items-center gap-1"):
                            level_icon("warn")
                            ui.label(w).classes("text-sm")
                    keys = ("name", "type", "mode") + (("width",) if any(c.get("width") for c in columns) else ())
                    labels = {"width": "Length", "type": "Type (BigQuery's guess)" if hub else "Type", "hub": "Hub rules"}
                    keys += ("hub",) if hub else ()
                    ui.table(
                        columns=[{"name": k, "label": labels.get(k, k.capitalize()), "field": k, "align": "left"} for k in keys],
                        rows=[{**{k: c.get(k) for k in keys}, **({"hub": hub.get(c.get("name"), "")} if hub else {})}
                              for c in columns],
                        row_key="name",
                    ).props("dense flat").classes("w-full max-h-72").mark("import-preview")
                    if rows:
                        hint(f"First sample row: {json.dumps(rows[0], default=str)[:300]}")

            def fail(exc: Exception) -> None:
                ui.notify(f"Could not read the schema: {gcp_error_text(exc)}", type="negative", multi_line=True)

            async def finish(r: InferResult, file_format: str) -> None:
                """Show a sample file's columns; with the switch on, BigQuery types them (the hub's rules otherwise)."""
                columns, warnings, hub = r.columns, list(r.warnings), None
                if guess["on"] and file_format in self.GUESS_FORMATS:
                    try:
                        g = await self.bigquery_guess(r.columns, r.rows, guess["dataset"])
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"BigQuery could not guess the types ({gcp_error_text(exc)}); the hub's own rules typed them")
                    else:
                        hub = {c.name: c.type for c in r.columns}
                        columns = g.columns
                        warnings += [f"BigQuery guessed the types from {g.detected['rows_loaded']} row(s); the temporary "
                                     "lab table is already deleted", *g.warnings]
                show([c.model_dump() for c in columns], warnings, r.sample_rows, hub)

            guess = {"dataset": step.get("staging_dataset") or ""}
            guess["on"] = self.guess_target(guess["dataset"])[1] is None
            sample_formats = ["CSV", "TXT", "NEWLINE_DELIMITED_JSON", "PARQUET", "XLSX"]
            current = step.get("file_format", "CSV")
            with ui.tab_panels(tabs, value=t_layout if self.fixed else t_upload).classes("w-full"):
                with ui.tab_panel(t_layout):
                    layout = ui.textarea("One column per line: name length [type]",
                                         placeholder="member_id 10\nmember_name 30 STRING\njoin_date 10 DATE\nbalance 12 NUMERIC").props(
                        "outlined").classes("w-full sf-mono").mark("layout-text")
                    hint("Paste the layout from the file specification: the column name, its length in characters and, "
                         "optionally, its BigQuery type (STRING when left out). Commas, tabs or spaces between them.")

                    def from_layout() -> None:
                        try:
                            show([c.model_dump() for c in columns_from_layout(layout.value)], [])
                        except ValueError as exc:
                            fail(exc)

                    ui.button("Read", icon="search", on_click=from_layout).props("unelevated dense no-caps").mark("layout-read")
                with ui.tab_panel(t_upload):
                    with ui.row().classes("gap-2"):
                        fmt = ui.select(sample_formats, value=current if current in sample_formats else "TXT", label="Format").props("dense outlined")
                        delim = ui.input("Delimiter (blank = detect)", value="").props("dense outlined").classes("w-48")
                        header = ui.switch("First row is a header", value=str(step.get("header", "0")).lower() != "none")
                    hint("A few hundred rows are enough. Compressed .gz files are fine. Nothing is stored.")
                    self.guess_controls(guess)

                    async def on_upload(e) -> None:
                        data = await e.file.read()
                        try:
                            r = await run.io_bound(infer_from_upload, data, e.file.name, file_format=fmt.value,
                                                   delimiter=delim.value or None, has_header=header.value,
                                                   limit=svc.settings.max_upload_mb * 1024 * 1024)
                        except Exception as exc:  # noqa: BLE001
                            fail(exc)
                            return
                        await finish(r, fmt.value)

                    ui.upload(on_upload=on_upload, auto_upload=True, max_file_size=svc.settings.max_upload_mb * 1024 * 1024,
                              label="Drop a sample file here").props("accept=.csv,.txt,.json,.jsonl,.ndjson,.parquet,.avro,.orc,.gz,.xlsx").classes("w-full")
                with ui.tab_panel(t_gcs):
                    uri = ui.input("gs://bucket/path/to/sample.csv", value=state.analyses.get(-1, {}).get("latest_uri", "")).props("dense outlined").classes("w-full")
                    gcs_formats = ["CSV", "TXT", "NEWLINE_DELIMITED_JSON", "PARQUET", "AVRO", "ORC", "XLSX"]
                    fmt2 = ui.select(gcs_formats, value=current if current in gcs_formats else "TXT", label="Format").props("dense outlined")
                    self.guess_controls(guess)

                    async def from_gcs() -> None:
                        try:
                            r = await run.io_bound(infer_from_gcs, svc.clients, project_id=project, uri=uri.value.strip(),
                                                   file_format=fmt2.value, delimiter=None, has_header=True,
                                                   sample_bytes=svc.settings.sample_read_bytes)
                        except Exception as exc:  # noqa: BLE001
                            fail(exc)
                            return
                        await finish(r, fmt2.value)

                    ui.button("Read schema", icon="search", on_click=from_gcs).props("unelevated dense no-caps")
                with ui.tab_panel(t_json):
                    text = ui.textarea("Paste a BigQuery JSON schema ([{name, type, mode}, …])").props("outlined").classes("w-full sf-mono")

                    def from_json() -> None:
                        try:
                            show([c.model_dump() for c in columns_from_json_text(text.value)], [])
                        except ValueError as exc:
                            fail(exc)

                    ui.button("Read", icon="search", on_click=from_json).props("unelevated dense no-caps")
                with ui.tab_panel(t_ddl):
                    ddl = ui.textarea("Paste CREATE TABLE … (col TYPE, …)").props("outlined").classes("w-full sf-mono")

                    def from_ddl() -> None:
                        try:
                            show([c.model_dump() for c in columns_from_ddl(ddl.value)], [])
                        except ValueError as exc:
                            fail(exc)

                    ui.button("Read", icon="search", on_click=from_ddl).props("unelevated dense no-caps")
                with ui.tab_panel(t_table):
                    table = ui.input("project.dataset.table (a lab table)").props("dense outlined").classes("w-full sf-mono")

                    async def from_table() -> None:
                        try:
                            cols = await run.io_bound(columns_from_bq_table, svc.clients.bigquery(project), table.value.strip("` "))
                        except Exception as exc:  # noqa: BLE001
                            fail(exc)
                            return
                        show([c.model_dump() for c in cols], [])

                    ui.button("Read", icon="search", on_click=from_table).props("unelevated dense no-caps")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                def apply() -> None:
                    if not result.get("columns"):
                        ui.notify("Read a schema first", type="warning")
                        return
                    step["columns"] = result["columns"]
                    dialog.close()
                    self.body.refresh()
                    if self.fixed and not all(c.get("width") for c in step["columns"]):
                        ui.notify("Fixed-width file: enter the length of every column", type="warning")
                    ui.notify(f"Imported {len(result['columns'])} column(s)", type="positive")

                ui.button("Use these columns", icon="check", on_click=apply).props("unelevated no-caps")
        dialog.open()


def columns_to_bq_json_safe(cols: list[dict]) -> list[dict]:
    out = []
    for c in cols:
        d = {"name": c.get("name", ""), "type": c.get("type", "STRING"), "mode": c.get("mode", "NULLABLE")}
        if c.get("description"):
            d["description"] = c["description"]
        if c.get("fields"):
            d["fields"] = columns_to_bq_json_safe(c["fields"])
        if c.get("width"):
            d["width"] = c["width"]  # fixed-width files (not part of a BigQuery schema)
        out.append(d)
    return out


# ====================================================================== parameters editor
class ParamsEditor:
    def __init__(self, params: list[dict], get_sql: Callable[[], str], state: WizardState):
        self.params, self.get_sql, self.state = params, get_sql, state

    def render(self) -> None:
        with ui.row().classes("items-center gap-2"):
            ui.label("Query parameters").classes("font-semibold")
            help_icon("@name values in the SQL. They are passed to BigQuery as query parameters, never pasted into the SQL text. "
                      "Dates are relative to the day the script runs, or a value in the YAML that can differ per environment.")
            ui.button("Add", icon="add", on_click=self.add).props("flat dense no-caps")
            ui.button("Detect @parameters in the SQL", icon="search", on_click=self.detect).props("flat dense no-caps")
        self.rows()

    def add(self, name: str = "") -> None:
        self.params.append({"name": name or f"param_{len(self.params) + 1}", "bq_type": "DATE", "source": "today",
                            "offset_days": 0, "default": None, "original_literal": None, "description": ""})
        self.rows.refresh()

    def detect(self) -> None:
        try:
            from ..models.rules import substitute_sql_vars

            sql = substitute_sql_vars(self.get_sql() or "", {k: (v or "x") for k, v in self.state.variable_values().items()})
        except KeyError:
            sql = self.get_sql() or ""
        found = sql_utils.find_query_params(sql)
        existing = {p["name"] for p in self.params}
        new = [n for n in found if n not in existing]
        for n in new:
            p = {"name": n, "bq_type": "DATE" if "date" in n.lower() else "STRING", "source": "today" if "date" in n.lower() else "config",
                 "offset_days": 0, "default": None if "date" in n.lower() else "", "original_literal": None, "description": ""}
            self.params.append(p)
        self.rows.refresh()
        ui.notify(f"Added {len(new)} parameter(s)" if new else "Every @parameter is already defined", type="info")

    def remove(self, i: int) -> None:
        del self.params[i]
        self.rows.refresh()

    @ui.refreshable_method
    def rows(self) -> None:
        if not self.params:
            hint("No parameters. Dates should be @parameters (relative to today, or a YAML value per environment).")
            return
        for i, p in enumerate(self.params):
            rendered = p.get("source")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.input("Name", placeholder="report_date").bind_value(p, "name").props("dense outlined prefix=@").classes("w-40 sf-mono")
                ui.select(PARAM_TYPES, label="Type").bind_value(p, "bq_type").props("dense outlined").classes("w-32")
                ui.select(PARAM_SOURCES, label="Value", value=rendered, on_change=lambda e, r=rendered: self.rows.refresh() if e.value != r else None).bind_value(p, "source").props("dense outlined").classes("w-56")
                if p.get("source") == "today_offset":
                    ui.number("Days (±)", precision=0).bind_value(p, "offset_days", forward=lambda v: int(v or 0)).props("dense outlined").classes("w-28")
                if p.get("source") == "config":
                    ui.input("Value (YAML)").bind_value(p, "default", forward=lambda v: v if v is not None else "", backward=opt).props("dense outlined").classes("w-40")
                ui.input("Description").bind_value(p, "description").props("dense outlined").classes("flex-grow")
                ui.button(icon="delete_outline", on_click=lambda i=i: self.remove(i)).props("flat dense round size=sm color=negative")


# ====================================================================== tables found in SQL (automatic)
class TablesCard:
    """Watches a SQL text and lists the tables it reads and writes, with each GCP project as a config variable.

    The scan is automatic (sqlglot, no AI) and runs a moment after the text stops changing. ``on_scan(scan, previous)``
    sees every new scan (previous = the text scanned before, None on the first look after the page opened) and may
    return a note to show. With ``apply``, a button rewrites the projects in place (editors that hold the final SQL).
    """

    def __init__(self, get_sql: Callable[[], str], state: WizardState, *, step_name: str = "",
                 on_scan: Callable[[dict, str | None], str] | None = None, apply: Callable[[dict], None] | None = None):
        self.get_sql, self.state, self.step_name = get_sql, state, step_name
        self.on_scan, self.apply = on_scan, apply
        self.scan: dict[str, Any] | None = None
        self.note = ""
        self._scanned: str | None = None  # text of the last scan
        self._pending: str | None = None
        self._baseline: str | None = None  # text when the page opened

    def render(self) -> None:
        self._baseline = self.get_sql() or ""
        self.view()
        ui.timer(0.8, self.tick)

    async def tick(self) -> None:
        text = self.get_sql() or ""
        if text == self._scanned:
            return
        if text != self._pending:  # still typing: look again on the next tick
            self._pending = text
            return
        previous = self._scanned
        if previous is None and text != self._baseline:
            previous = self._baseline  # changed before the first scan finished (e.g. pasted straight away)
        self._scanned = text
        if not text.strip():
            self.scan, self.note = None, ""
            self.view.refresh()
            return
        result = await run.io_bound(scan_projects, text, self.state.variable_values("lab"), step=self.step_name,
                                    env_values=self.state.all_env_values(), defaults=self.state.default_projects)
        if result is None:  # the page closed while scanning
            return
        self.scan = json.loads(result.model_dump_json())
        self.note = (self.on_scan(self.scan, previous) if self.on_scan else "") or ""
        self.view.refresh()

    def _apply(self) -> None:
        if self.scan and self.apply:
            self.apply(self.scan)

    @ui.refreshable_method
    def view(self) -> None:
        s = self.scan
        if not s:
            return
        with ui.card().classes("sf-card w-full bg-slate-50 gap-1").mark("tables-card"):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.icon("table_view").classes("text-primary")
                ui.label("Tables in this SQL").classes("font-semibold")
                hint("found automatically; hard-coded GCP projects become config variables")
                ui.space()
                ui.chip(s["statement_kind"], icon="category").props("outline dense")
            if s.get("parse_error"):
                with ui.row().classes("items-center gap-1 no-wrap"):
                    level_icon("warn")
                    ui.label(f"The SQL does not parse yet: {s['parse_error']}").classes("text-sm")
                return
            if not s["tables"]:
                hint("No tables referenced.")
            for t in s["tables"]:
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.chip(t["role"], color="teal" if t["role"] == "reads" else "orange", text_color="white").props("dense square")
                    ui.label(t["name"]).classes("sf-mono")
                    if t.get("variable"):
                        ui.label(f"project → ${{{t['variable']}}}").classes("sf-preview")
                    elif t.get("project") is None and t.get("dataset"):
                        ui.label("no project: runs in the credentials' project").classes("sf-hint")
            for f in s["findings"]:
                with ui.row().classes("items-center gap-1 no-wrap"):
                    level_icon({"warning": "warn", "error": "error"}.get(f["severity"], "info"))
                    ui.label(f["message"]).classes("text-sm")
            if self.apply and s.get("changed"):
                new = ", ".join(f"{v['name']} = {v['values']['lab']} (lab)" for v in s["variables"])
                with ui.row().classes("items-center gap-2"):
                    ui.button("Use config variables for the projects", icon="data_object", on_click=self._apply).props(
                        "outline dense no-caps").mark("apply-projects")
                    if new:
                        hint(f"adds {new}")
            if self.note:
                hint(self.note)


# ====================================================================== SQL box
def sql_box(obj: dict, key: str, *, name: str = "query", mark: str | None = None, file_key: str | None = None,
            forward: Callable[[Any], Any] | None = None, backward: Callable[[Any], Any] | None = None) -> ui.codemirror:
    """A SQL editor with a summary bar (attached file, lines, size). Long SQL starts folded away so the page stays
    readable; it can be shown, hidden or downloaded to read in your own editor."""
    opened = {"value": (obj.get(key) or "").count("\n") < LARGE_SQL_LINES}
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("items-center gap-2 w-full"):
            summary = ui.label().classes("sf-hint")
            toggle = ui.button(on_click=lambda: flip()).props("flat dense no-caps size=sm")
            ui.button("Download .sql", icon="download", on_click=lambda: ui.download.content(
                obj.get(key) or "", f"{name}.sql", "text/plain")).props("flat dense no-caps size=sm")
        editor = ui.codemirror(language="SQL").bind_value(obj, key, forward=forward, backward=backward).classes("w-full h-56")
        if mark:
            editor.mark(mark)

    def flip() -> None:
        opened["value"] = not opened["value"]
        show()

    def show() -> None:
        text = obj.get(key) or ""
        lines = text.count("\n") + 1 if text else 0
        if file_key and obj.get(file_key) and not text.strip():
            obj[file_key] = ""  # the attached SQL was cleared
        source = f"📎 {obj[file_key]} · " if file_key and obj.get(file_key) else ""
        size = f"{len(text.encode('utf-8')) / 1024:.1f} KB" if text else "empty"
        summary.text = f"{source}{lines:,} line{'s' if lines != 1 else ''} · {size}" + (
            "" if opened["value"] else " · hidden to keep the page readable")
        toggle.text, toggle.props["icon"] = ("Hide SQL", "visibility_off") if opened["value"] else ("Show SQL", "visibility")
        toggle.update()
        editor.visible = opened["value"]

    show()
    ui.timer(1.0, show)  # the text also changes by typing, attaching a file or applying an analysis
    return editor


def attach_sql(on_text: Callable[[str, str], None], max_mb: int) -> ui.upload:
    """Upload a .sql/.txt file and hand its text (UTF-8 or Windows-1252, Unix line ends) to on_text(text, file_name)."""

    async def on_upload(e) -> None:
        data = await e.file.read()
        try:
            text = sql_utils.decode_sql_file(data, e.file.name, max_bytes=max_mb * 1024 * 1024)
        except ValueError as exc:
            ui.notify(f"Not attached: {exc}", type="negative")
            return
        on_text(text, e.file.name)
        upload.reset()
        ui.notify(f"Attached {e.file.name}: {text.count(chr(10)) + 1:,} lines", type="positive")

    upload = ui.upload(on_upload=on_upload, auto_upload=True, label="Attach a .sql / .txt file",
                       max_file_size=max_mb * 1024 * 1024).props("accept=.sql,.txt flat dense").classes("w-72")
    return upload


# ====================================================================== SQL panel
class SqlPanel:
    """Paste SQL → analyze (Gemini + equivalence guard) → apply → dry-run. Used by SQL steps and query exports."""

    def __init__(self, step: dict, index: int, state: WizardState, svc: Any, *, mode: str, on_applied: Callable[[], None]):
        self.step, self.index, self.state, self.svc, self.mode = step, index, state, svc, mode
        self.on_applied = on_applied
        self.sql_key = "sql" if mode == "sql" else "source_sql"
        self.original_key = "original_sql" if mode == "sql" else None
        self.analysis: dict[str, Any] | None = state.analyses.get(index)
        self._last_auto: str | None = None  # embedded SQL the automatic scan last produced

    def render(self) -> None:
        name = self.step.get("function_name") or ""
        max_mb = self.svc.settings.max_sql_file_mb
        if self.original_key:
            with ui.row().classes("items-center gap-4 w-full"):
                ui.label("1. Paste your SQL, or attach it as a file").classes("font-semibold")
                attach_sql(self._attached, max_mb).mark("attach-sql")
            hint("Paste it exactly as you run it today, with hard-coded projects, datasets and dates (a long script is easier "
                 "to attach as a .sql or .txt file). The hub lists its tables and turns each GCP project into a config "
                 "variable straight away; “Analyze & parameterize” also handles datasets and dates, and every change is "
                 "proven to keep the logic the same. The SQL is still embedded in the script.")
            sql_box(self.step, self.original_key, name=f"{name or 'step'}_original", mark="original-sql", file_key="sql_file")
            TablesCard(lambda: self.step.get(self.original_key) or "", self.state, step_name=name, on_scan=self._on_scan).render()
            with ui.row().classes("gap-2 items-center"):
                self.analyze_btn = ui.button("Analyze & parameterize", icon="auto_awesome", on_click=self.analyze).props("unelevated no-caps").mark("analyze")
                ui.button("Use my SQL as-is", icon="content_copy", on_click=self.use_as_is).props("outline no-caps").mark("use-as-is")
                ui.label("Gemini" if self.svc.gemini else "Rule-based (Gemini is off)").classes("sf-hint")
            self.analysis_view()
            ui.label("2. SQL embedded in the script").classes("font-semibold mt-2")
        else:
            with ui.row().classes("items-center gap-2"):
                ui.label("Query to export").classes("font-semibold")
                ui.button("Analyze & parameterize", icon="auto_awesome", on_click=self.analyze).props("flat dense no-caps")
                attach_sql(self._attached, max_mb).mark("attach-sql")
            self.analysis_view()
        hint("This exact text goes into the step function. ${name} = config variable, @name = query parameter.")
        sql_box(self.step, self.sql_key, name=f"{name or 'step'}_embedded", forward=lambda v: v or "", backward=opt)
        if not self.original_key:
            TablesCard(lambda: self.step.get(self.sql_key) or "", self.state, step_name=name,
                       apply=lambda scan: apply_project_scan(self.step, self.sql_key, scan, self.state, self.on_applied)).render()
        self.params_editor = ParamsEditor(self.step.setdefault("parameters", []), lambda: self.step.get(self.sql_key) or "", self.state)
        self.params_editor.render()
        with ui.row().classes("items-center gap-2"):
            ui.button("Dry run on BigQuery", icon="play_circle_outline", on_click=self.dry_run).props("outline no-caps")
            help_icon("BigQuery checks the SQL without running it: syntax, tables, permissions and how many bytes it would scan. "
                      "Nothing is executed or written.")
            self.dry_label = ui.label().classes("text-sm")

    def _attached(self, text: str, file_name: str) -> None:
        """An attached file replaces the pasted SQL (SQL steps) or the query to export; the scan picks it up."""
        if self.original_key:
            self.step[self.original_key] = text
            self.step["sql_file"] = file_name
        else:
            self.step[self.sql_key] = text

    def _on_scan(self, scan: dict, previous: str | None) -> str:
        """Keep the embedded SQL in step with the pasted SQL for as long as it is the automatic version of it."""
        if previous is None:  # first look after the page opened: show the tables, change nothing
            self._last_auto = scan["sql"]
            return ""
        if scan.get("parse_error"):
            return ""
        current = self.step.get(self.sql_key) or ""
        automatic = not current.strip() or current in (previous, self._last_auto)
        self._last_auto = scan["sql"]
        if not automatic:
            return ("The SQL embedded below was edited or analyzed, so it was left as it is. "
                    "Click “Analyze & parameterize” to refresh it from the pasted SQL.")
        self.step["statement_kind"] = scan["statement_kind"]
        self.step["sources"], self.step["targets"] = scan["sources"], scan["targets"]
        apply_project_scan(self.step, self.sql_key, scan, self.state, self.on_applied)
        return ""

    def use_as_is(self) -> None:
        self.step["sql"] = self.step.get("original_sql") or ""
        facts = sql_utils.facts(self.step["sql"])
        self.step["statement_kind"] = facts.kind
        self.step["sources"], self.step["targets"] = facts.sources, facts.targets
        self.on_applied()
        ui.notify("Copied. Consider “Analyze” to turn hard-coded dates and datasets into parameters.", type="info")

    async def analyze(self) -> None:
        sql = (self.step.get(self.original_key) if self.original_key else self.step.get(self.sql_key)) or ""
        if not sql.strip():
            ui.notify("Paste the SQL first", type="warning")
            return
        n = ui.notification("Analyzing the SQL…", spinner=True, timeout=None)
        try:
            result = await run.io_bound(
                self.svc.analyzer.analyze, sql,
                existing_variables=self.state.variable_values("lab"),
                taken_function_names=self.state.taken_names(self.index),
                env_values=self.state.all_env_values(), defaults=self.state.default_projects,
            )
        except Exception as exc:  # noqa: BLE001
            n.dismiss()
            ui.notify(f"Analysis failed: {exc}", type="negative")
            return
        n.dismiss()
        self.analysis = json.loads(result.model_dump_json())
        self.state.analyses[self.index] = self.analysis
        self.analysis_view.refresh()

    @ui.refreshable_method
    def analysis_view(self) -> None:
        a = self.analysis
        if not a:
            return
        with ui.card().classes("sf-card w-full bg-slate-50"):
            eq = a["equivalence"]
            with ui.row().classes("items-center gap-2"):
                if eq["ok"]:
                    ui.icon("verified").classes("text-green-700")
                    ui.label(f"Verified: same logic as your SQL (compared by {eq['method']})").classes("text-green-800 font-medium")
                else:
                    ui.icon("gpp_bad").classes("text-red-700")
                    ui.label("Not verified — the parameterized SQL is not equivalent; kept your SQL").classes("text-red-800 font-medium")
                ui.space()
                ui.chip(a["statement_kind"], icon="category").props("outline dense")
                if a.get("ai_used"):
                    ui.chip(a.get("model") or "Gemini", icon="auto_awesome").props("outline dense color=accent")
            if a.get("summary"):
                ui.label(a["summary"]).classes("text-sm")
            with ui.row().classes("gap-6 w-full"):
                with ui.column().classes("gap-1"):
                    ui.label("Variables").classes("text-xs uppercase sf-muted")
                    for v in a["variables"] or [{"name": "—", "values": {"lab": "none new"}}]:
                        ui.label(f"{v['name']} = {v['values']['lab']}").classes("sf-mono")
                with ui.column().classes("gap-1"):
                    ui.label("Parameters").classes("text-xs uppercase sf-muted")
                    for p in a["parameters"] or [{"name": "—", "bq_type": "", "source": "none", "original_literal": ""}]:
                        was = f" (was {p['original_literal']})" if p.get("original_literal") else ""
                        ui.label(f"@{p['name']} {p['bq_type']} ← {PARAM_SOURCES.get(p['source'], p['source'])}{was}").classes("sf-mono")
                with ui.column().classes("gap-1"):
                    ui.label("Tables").classes("text-xs uppercase sf-muted")
                    for t in a["sources"]:
                        ui.label(f"reads  {t}").classes("sf-mono")
                    for t in a["targets"]:
                        ui.label(f"writes {t}").classes("sf-mono")
            for f in a["findings"]:
                with ui.row().classes("items-center gap-1 no-wrap"):
                    level_icon({"warning": "warn", "error": "error"}.get(f["severity"], "info"))
                    ui.label(f["message"]).classes("text-sm")
            if a.get("rejected_proposal") or eq.get("diff"):
                with ui.expansion("Show the rejected proposal", icon="difference").classes("w-full"):
                    ui.code(eq.get("diff") or a.get("rejected_proposal") or "", language="diff").classes("w-full sf-code")
            with ui.row().classes("gap-2 items-center"):
                ui.button("Apply to this step", icon="done_all", on_click=self.apply).props("unelevated no-caps").mark("apply-analysis")
                if a.get("suggested_function_name") and self.mode == "sql":
                    ui.label(f"Suggested function name: {a['suggested_function_name']}").classes("sf-hint sf-mono")

    def apply(self) -> None:
        a = self.analysis or {}
        self.step[self.sql_key] = a.get("sql", "")
        existing = {p["name"]: p for p in self.step.setdefault("parameters", [])}
        for p in a.get("parameters", []):
            existing[p["name"]] = p
        self.step["parameters"] = list(existing.values())
        added = self.state.add_variables(a.get("variables", []))
        if self.mode == "sql":
            self.step["statement_kind"] = a.get("statement_kind") or self.step.get("statement_kind", "OTHER")
            self.step["sources"], self.step["targets"] = a.get("sources", []), a.get("targets", [])
            if a.get("suggested_destination_table") and not self.step.get("destination_table"):
                self.step["destination_table"] = a["suggested_destination_table"]
            fn = self.step.get("function_name", "")
            if a.get("suggested_function_name") and (fn.startswith("run_sql") or not fn):
                self.step["function_name"] = suggest_function_name(a["suggested_function_name"], self.state.taken_names(self.index))
            if not self.step.get("description") and a.get("summary"):
                self.step["description"] = a["summary"].split(". ")[0].rstrip(".")[:200]
        self.on_applied()
        ui.notify(f"Applied. {len(added)} variable(s) added, {len(a.get('parameters', []))} parameter(s).", type="positive")

    async def dry_run(self) -> None:
        sql = self.step.get(self.sql_key) or ""
        if not sql.strip():
            ui.notify("There is no SQL to check yet", type="warning")
            return
        values = self.state.variable_values("lab")
        if not values.get("write_project"):
            ui.notify("Enter the lab write_project on Basics first (dry runs run in lab)", type="warning")
            return
        try:
            params = [Parameter.model_validate(p) for p in self.step.get("parameters", [])]
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Fix the parameters first: {exc}", type="negative")
            return
        self.dry_label.text = "Checking in lab…"
        result = await run.io_bound(
            dry_run_sql, self.svc.clients, sql=sql, project_id=values["write_project"],
            location=self.svc.settings.bq_location, variables=values, parameters=params,
        )
        if result["ok"]:
            self.dry_label.text = f"✓ Valid in lab — would process {human_bytes(result['bytes'])} (dates as of {result['as_of']})"
            self.dry_label.classes(replace="text-sm text-green-800")
        else:
            where = f" (line {result['line']})" if result.get("line") else ""
            self.dry_label.text = f"✗ {result['error']}{where}"
            self.dry_label.classes(replace="text-sm text-red-700")


def apply_project_scan(target: dict, key: str, scan: dict, state: WizardState, on_applied: Callable[[], None]) -> None:
    """Put the scanned SQL (projects as ${variables}) into target[key] and add the new variables to the config."""
    target[key] = scan["sql"]
    added = state.add_variables(scan.get("variables") or [])
    on_applied()
    if added:
        ui.notify(f"GCP project{'s' if len(added) > 1 else ''} → config variable{'s' if len(added) > 1 else ''}: "
                  + ", ".join(added), type="positive")


# ====================================================================== validation report
def render_report(report: dict, *, on_fix: Callable[[dict], None] | None = None, on_goto: Callable[[str | None], None] | None = None) -> None:
    counts = report["counts"]
    with ui.row().classes("items-center gap-3"):
        for level, label in (("error", "errors"), ("warn", "warnings"), ("info", "notes"), ("ok", "passed")):
            with ui.row().classes("items-center gap-1"):
                level_icon(level)
                ui.label(f"{counts.get(level, 0)} {label}")
        ui.label(f"checked in {report.get('environment', 'lab')} on {report.get('checked_on', '')}"
                 + ("" if report.get("gcp_checks") else " · GCP checks skipped")).classes("sf-hint")
    groups: dict[str, list[dict]] = {}
    for item in report["items"]:
        groups.setdefault(item.get("step") or item.get("section") or "general", []).append(item)
    order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    for group, items in groups.items():
        worst = min(items, key=lambda i: order[i["level"]])["level"]
        with ui.expansion(group, icon="list", value=worst in ("error", "warn")).classes("w-full sf-card"):
            for item in sorted(items, key=lambda i: order[i["level"]]):
                with ui.row().classes("items-start gap-2 no-wrap w-full"):
                    level_icon(item["level"])
                    with ui.column().classes("gap-0 flex-grow"):
                        ui.label(item["title"]).classes("text-sm")
                        if item.get("detail"):
                            ui.label(item["detail"]).classes("sf-hint")
                    if item.get("fix") and on_fix:
                        ui.button("Fix", icon="build", on_click=lambda i=item: on_fix(i["fix"])).props("flat dense no-caps")
                    if item.get("step") and on_goto:
                        ui.button("Go to step", on_click=lambda i=item: on_goto(i["step"])).props("flat dense no-caps")


# ====================================================================== file viewer
def render_files(files: list[dict]) -> None:
    if not files:
        return
    lang = {"python": "python", "yaml": "yaml", "json": "json", "markdown": "markdown", "shell": "bash", "text": "text"}
    with ui.splitter(value=22).classes("w-full sf-card") as splitter:
        with splitter.before:
            listing = ui.list().props("dense separator").classes("w-full")
        with splitter.after:
            viewer = ui.code("", language="python").classes("w-full sf-code")

        def show(f: dict) -> None:
            viewer.language = lang.get(f["language"], "text")
            viewer.set_content(f["content"])

        with listing:
            for f in files:
                with ui.item(on_click=lambda f=f: show(f)).props("clickable"):
                    with ui.item_section():
                        ui.item_label(f["path"]).classes("sf-mono text-xs")
        show(next((f for f in files if f["path"].endswith(".py") and "/" not in f["path"]), files[0]))
