"""Detail forms for each step type (file ingestion, SQL, export, copy files, data quality, custom process).

Values that differ between environments are typed as {variables}; every input previews what it becomes in lab and
value (prod). Look-ups against GCP (browse, matching files) only ever use the lab values.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from nicegui import run, ui

from ..models.rules import fill_variables
from ..models.spec import (
    COPY_DIRECTIONS,
    EXPORT_COMPRESSION,
    FILE_EXTENSIONS,
    INGEST_EXTENSIONS,
    copy_direction,
    has_header_rows,
    is_delimited,
)
from ..services.custom_step import check_custom_step, write_custom_code
from ..services.gcp_tools import gcs_browse, gcs_match
from ..services.renderer import render_custom_preview
from ..services.validators import gcp_error_text
from .components import SchemaEditor, SqlPanel, TablesCard, apply_project_scan, opt, template_input
from .state import WizardState, new_check
from .theme import help_icon, hint, human_bytes, level_icon, section

INGEST_FORMATS = {"CSV": "CSV (.csv)", "TXT": "TXT — delimited text (.txt)", "FIXED": "TXT — fixed width (column lengths)",
                  "NEWLINE_DELIMITED_JSON": "JSON (one object per line)", "PARQUET": "Parquet", "XLSX": "Excel (.xlsx)"}
EXPORT_FORMATS = {"CSV": "CSV (.csv)", "TXT": "TXT — delimited text (.txt)", "NEWLINE_DELIMITED_JSON": "JSON (one object per line)",
                  "PARQUET": "Parquet", "AVRO": "Avro", "XLSX": "Excel (.xlsx)"}
DELIMITERS = {",": "comma", "|": "pipe |", "\t": "tab", ";": "semicolon"}
LOAD_MODES = {
    "merge": "MERGE into the target (update matching keys, insert new ones)",
    "append": "Append the rows to the target",
    "truncate": "Replace the target's rows with this run's files",
}
SQL_WRITE = {"truncate": "Replace the destination table", "append": "Append to the destination table", "empty": "Only if empty"}
NO_FILES = {"email": "Email the team (“no file received today”)", "warn": "Log a warning and continue", "fail": "Fail the run"}
COPY_OPS = {"cp": "Copy", "mv": "Move", "rsync": "Sync a folder"}
COPY_SIDES = {  # (label, example) for a bucket side and a local side of a copy job
    "bucket_source": ("Source {n}: gs://bucket/folder/files", "e.g. gs://{vendor_bucket}/outbound/Daily File_*.csv"),
    "local_source": ("Source {n}: a file or folder on the machine that runs the script",
                     "e.g. /data/exports/members_{date:%Y%m%d}.csv"),
    "bucket_destination": ("Destination: gs://bucket/folder/", "a folder ends with /, e.g. gs://{landing_bucket}/{landing_path}/"),
    "local_destination": ("Destination: a folder on the machine that runs the script", "a folder ends with /, e.g. /data/inbound/"),
}
DATE_SUFFIXES = {"%Y_%m_%d": "_2026_01_31 style (%Y_%m_%d)", "%Y%m%d": "20260131 (%Y%m%d)", "%Y-%m-%d": "2026-01-31 (%Y-%m-%d)",
                 "%m%d%Y": "01312026 (%m%d%Y)", "": "no date"}
DQ_KINDS = {
    "row_count_min": "At least N rows",
    "not_null": "Columns are never empty (NULL)",
    "unique": "Column combination is unique",
    "accepted_values": "Column only has allowed values",
    "freshness": "Newest timestamp is recent enough",
    "custom_sql": "Custom query returns no rows",
}


def step_header(step: dict, index: int, state: WizardState) -> None:
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label(f"Step {index + 1}").classes("sf-muted")
        ui.label().bind_text_from(step, "function_name", backward=lambda v: f"def {v}(…)").classes("text-lg font-semibold sf-mono")
        ui.space()
        ui.switch("Enabled").bind_value(step, "enabled")
    ui.input("What this step does (becomes the docstring and the comment in main())").bind_value(step, "description").props("outlined dense").classes("w-full")


def problems_box(state: WizardState, index: int) -> None:
    @ui.refreshable
    def box() -> None:
        errors = state.step_errors(index) if index < len(state.steps) else []
        if errors:
            with ui.card().classes("sf-card w-full bg-red-50"):
                for e in errors[:8]:
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        ui.icon("error_outline").classes("text-red-700")
                        ui.label(e).classes("text-sm text-red-800")

    box()
    ui.timer(2.0, box.refresh)


def config_key_input(step: dict) -> None:
    ui.input("YAML block", placeholder="ftp").bind_value(step, "config_key").props("outlined dense").classes("w-40 sf-mono")
    help_icon("The key of this step's settings inside each environment block of the YAML (gcp_projects.lab.<key>).")


def delimiter_input(step: dict, key: str, label: str = "Delimiter") -> None:
    current = step.get(key) or ","
    options = dict(DELIMITERS)
    if current not in options:
        options[current] = current
    ui.select(options, label=label, value=current, new_value_mode="add-unique").bind_value(step, key).props("outlined dense").classes("w-40")


# ====================================================================== file ingestion
def ingest_form(step: dict, index: int, state: WizardState, svc: Any, refresh: Callable[[], None]) -> None:
    with section("Where are the files?", "The script lists the folder and loads every file whose name contains the text "
                                         "below (the team's file-ingestion pattern).", "folder_open"):
        with ui.row().classes("w-full gap-4"):
            template_input(step, "read_bucket", "Bucket", state, classes="flex-1", hint_text="bucket name or {variable}, e.g. {landing_bucket}")
            template_input(step, "read_path", "Folder in the bucket", state, classes="flex-1", hint_text="e.g. {landing_path} or inbound/engagement")
        with ui.row().classes("w-full gap-4 items-start"):
            template_input(step, "filename", "File name contains", state, classes="flex-1", tokens=False,
                           hint_text="e.g. Daily Engagement File_  (files are matched by this text and the extension)")

            @ui.refreshable
            def format_box() -> None:
                fmt = step.get("file_format") or "CSV"
                with ui.row().classes("gap-2 items-center"):
                    ui.select(INGEST_FORMATS, label="Format", value=fmt, on_change=lambda e, f=fmt: on_format(e.value, f)).bind_value(
                        step, "file_format").props("outlined dense").classes("w-64").mark("ingest-format")
                    if is_delimited(fmt):
                        delimiter_input(step, "separator", "Separator")
                    elif fmt == "FIXED":
                        help_icon("Every line has the same layout: each column takes a fixed number of characters. Enter "
                                  "each column's length in the table schema below (or paste the layout under Import columns).")
                    elif fmt == "XLSX":
                        ui.input("Sheet", placeholder="the first sheet").bind_value(step, "sheet_name").props(
                            "outlined dense").classes("w-48").mark("ingest-sheet")
                        help_icon("The workbook's sheet to load (empty = the first one). Columns are read by position; "
                                  "use Header row / Skip rows for title rows above the header.")

            def on_format(value: str, previous: str) -> None:
                if value == previous:
                    return
                if value == "FIXED" and str(step.get("header")) == "0":
                    step["header"] = "None"  # fixed-width files rarely have a header row
                format_box.refresh()
                rows_box.refresh()
                schema_editor.body.refresh()
                schema_editor.layout_check.refresh()

            format_box()
        with ui.row().classes("gap-2 items-center"):
            ui.button("Check which files match (lab)", icon="search", on_click=lambda: check_files()).props("outline dense no-caps")
            ui.button("Browse lab bucket…", icon="folder_open", on_click=lambda: browse_dialog(step, state, svc)).props("flat dense no-caps")
            match_label = ui.label().classes("text-sm")

        async def check_files() -> None:
            values = state.variable_values("lab")
            match_label.text = "Listing in lab…"
            fmt = step.get("file_format") or "CSV"
            r = await run.io_bound(
                gcs_match, svc.clients, project_id=values.get("write_project") or "", bucket=step.get("read_bucket") or "",
                folder=step.get("read_path") or "", filename=step.get("filename") or "", extensions=INGEST_EXTENSIONS[fmt],
                variables=values, exclude_prefix=step.get("archive_path") or "" if step.get("archive") else "",
            )
            if not r["ok"]:
                match_label.text = "⚠ " + "; ".join(r.get("problems", []))
                return
            if r["count"]:
                match_label.text = f"✓ {r['count']} file(s) in lab ({human_bytes(r['total_bytes'])}), latest {r['latest']}"
                state.analyses[-1] = {"latest_uri": f"gs://{fill_variables(step.get('read_bucket') or '', values)}/{r['latest']}"}
            else:
                match_label.text = f"No file matches {r['resolved']} in lab right now"

        @ui.refreshable
        def rows_box() -> None:
            if not has_header_rows(step.get("file_format") or "CSV"):
                return
            with ui.row().classes("gap-4 items-center"):
                ui.input("Header row").bind_value(step, "header").props("outlined dense").classes("w-32 sf-mono").mark("ingest-header")
                help_icon("pandas header: 0 = the first row is a header (its names are replaced by the schema's), None = no header row.")
                ui.input("Skip rows").bind_value(step, "skiprows").props("outlined dense").classes("w-32 sf-mono")
                help_icon("pandas skiprows: None, a number of lines, or a list such as [0, 2].")

        rows_box()

    with section("Table schema", "Written to <script>_table_schema.yaml (the -ts file). pandas reads each column with "
                                 "this type; dates and timestamps are parsed. Fixed-width files also need each column's "
                                 "length (column_widths in the same file).", "schema"):
        schema_editor = SchemaEditor(step, state, svc)
        schema_editor.render()

    with section("Tables", "Each file is loaded into the staging table, appended to the history table and, when the "
                           "target table is on, written into it.", "table_chart"):
        step.setdefault("target_enabled", True)

        @ui.refreshable
        def tables_box() -> None:
            target, history = step.get("target_enabled"), step.get("history")
            with ui.row().classes("w-full gap-4"):
                template_input(step, "staging_dataset", "Staging dataset", state, tokens=True, classes="flex-1",
                               hint_text="dataset or {variable}, e.g. {temporary_dataset}")
                if target or history:
                    template_input(step, "target_dataset", "Target dataset", state, tokens=True, classes="flex-1",
                                   hint_text="dataset of the history" + (" and target tables" if target else " table"))
            with ui.row().classes("w-full gap-4 items-center"):
                ui.input("Staging table (table_name)").bind_value(step, "table_name").props("outlined dense").classes("w-72 sf-mono")
                if target:
                    ui.input("Target table", placeholder="same as the staging table").bind_value(step, "target_table").props(
                        "outlined dense").classes("w-72 sf-mono")
                ui.switch("Keep a history table (<table_name>_history)", value=history,
                          on_change=lambda e, h=history: tables_box.refresh() if e.value != h else None).bind_value(step, "history")
                ui.switch("Add batch columns").bind_value(step, "add_audit_columns")
                help_icon("file_batch_id, inserted_ts, updated_ts and file_name are added to every row.")
            ui.switch("Load into a target table (MERGE / append / replace)", value=target,
                      on_change=lambda e, t=target: tables_box.refresh() if e.value != t else None).bind_value(
                step, "target_enabled").mark("ingest-target")
            if target:
                merge_box()
            else:
                hint("The raw rows stay in the staging table for the next steps: the run's first file empties it and "
                     "every file of the run is added, so the next SQL steps read exactly this run's rows (it is emptied "
                     "when no file arrives)." + (" The history table keeps every file." if history else ""))

        @ui.refreshable
        def merge_box() -> None:
            mode = step.get("load_mode") or "merge"
            ui.select(LOAD_MODES, label="Into the target table", value=mode,
                      on_change=lambda e, m=mode: merge_box.refresh() if e.value != m else None).bind_value(step, "load_mode").props("outlined dense").classes("w-[28rem]")
            cols = [c.get("name") for c in step.get("columns") or [] if c.get("name")]
            if mode == "merge":
                with ui.row().classes("w-full gap-4 items-center"):
                    ui.select(cols, label="Key column(s)", multiple=True, new_value_mode="add-unique").bind_value(step, "merge_keys").props("outlined dense use-chips").classes("w-80")
                    ui.select([""] + cols, label="Newest row wins by", new_value_mode="add-unique").bind_value(
                        step, "merge_order_by", forward=lambda v: v or None, backward=lambda v: v or "").props("outlined dense").classes("w-64")
                    help_icon("Rows with the same key keep the newest by this column (row_number … ORDER BY it DESC); "
                              "existing rows are only updated by newer ones.")
            ui.input("Only rows where… (optional SQL filter on the staged rows)", placeholder="upper(Campaign) NOT LIKE '%UAT%'").bind_value(
                step, "merge_filter").props("outlined dense").classes("w-full sf-mono").mark("ingest-merge-filter")

        tables_box()
        ui.select({"WRITE_APPEND": "WRITE_APPEND", "WRITE_TRUNCATE": "WRITE_TRUNCATE"}, label="History write disposition").bind_value(
            step, "write_disposition").props("outlined dense").classes("w-56")

    with section("After loading", icon="inventory_2"):
        @ui.refreshable
        def archive_box() -> None:
            ui.switch("Move processed files to an archive folder", value=step.get("archive"),
                      on_change=lambda _e: archive_box.refresh()).bind_value(step, "archive")
            if step.get("archive"):
                with ui.row().classes("w-full gap-4"):
                    template_input(step, "archive_bucket", "Archive bucket", state, classes="flex-1", hint_text="empty = the same bucket")
                    template_input(step, "archive_path", "Archive folder", state, classes="flex-1", hint_text="e.g. {landing_path}/Archive")
            else:
                hint("Without archiving the same files are loaded again on the next run.")

        archive_box()
        with ui.row().classes("gap-2 items-center"):
            ui.select(NO_FILES, label="When no file arrives").bind_value(step, "on_no_files").props("outlined dense").classes("w-96")
    problems_box(state, index)


def browse_dialog(step: dict, state: WizardState, svc: Any) -> None:
    values = state.variable_values("lab")
    project = values.get("write_project") or svc.settings.gcp_project or ""
    nav = {"bucket": fill_variables(step.get("read_bucket") or "", values), "prefix": ""}
    if "{" in nav["bucket"]:
        nav["bucket"] = ""
    dialog = ui.dialog()
    with dialog, ui.card().classes("w-[760px] max-w-full"):
        ui.label("Browse Cloud Storage (lab)").classes("text-lg font-semibold")
        hint("Read-only, with the hub's ADC identity. Pick a folder to read the files from.")
        body = ui.column().classes("w-full max-h-[60vh] overflow-auto")

        async def load() -> None:
            body.clear()
            with body:
                ui.label(f"gs://{nav['bucket']}/{nav['prefix']}" if nav["bucket"] else "Buckets").classes("sf-mono")
                try:
                    data = await run.io_bound(gcs_browse, svc.clients, project_id=project, bucket=nav["bucket"], prefix=nav["prefix"])
                except Exception as exc:  # noqa: BLE001
                    ui.label(gcp_error_text(exc)).classes("sf-problem")
                    if nav["bucket"]:
                        ui.button("Back to buckets", on_click=lambda: (nav.update(bucket="", prefix=""), load())).props("flat no-caps")
                    return
                if not nav["bucket"]:
                    for b in data["buckets"]:
                        ui.button(b, icon="inventory", on_click=lambda b=b: (nav.update(bucket=b, prefix=""), load())).props("flat no-caps align=left").classes("w-full")
                    return
                if nav["prefix"]:
                    parent = "/".join(nav["prefix"].rstrip("/").split("/")[:-1])
                    ui.button(".. (up)", icon="arrow_upward", on_click=lambda: (nav.update(prefix=f"{parent}/" if parent else ""), load())).props("flat no-caps")
                for p in data["prefixes"]:
                    ui.button(p[len(nav["prefix"]):], icon="folder", on_click=lambda p=p: (nav.update(prefix=p), load())).props("flat no-caps align=left").classes("w-full")
                for o in data["objects"][:200]:
                    ui.label(f"{o['name'][len(nav['prefix']):]}  ·  {human_bytes(o['size'])}").classes("sf-mono text-xs pl-8")

        def use() -> None:
            if not step.get("read_bucket") or "{" not in step.get("read_bucket", ""):
                step["read_bucket"] = nav["bucket"]
            step["read_path"] = nav["prefix"].rstrip("/")
            dialog.close()
            ui.notify(f"Using gs://{nav['bucket']}/{nav['prefix']} (lab); use a {{variable}} if prod differs", type="positive")

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Use this folder", icon="check", on_click=use).props("unelevated no-caps")
    dialog.open()
    ui.timer(0.05, load, once=True)


# ====================================================================== SQL
def sql_form(step: dict, index: int, state: WizardState, svc: Any, refresh: Callable[[], None]) -> None:
    @ui.refreshable
    def dest_box() -> None:
        if step.get("statement_kind") == "SELECT":
            hint("A SELECT needs somewhere to put its results:")
            template_input(step, "destination_table", "Destination table", state, optional=True, tokens=False,
                           hint_text="e.g. {write_project}.{mart_dataset}.daily_summary")
            ui.select(SQL_WRITE, label="Each run should…").bind_value(step, "write_mode").props("outlined dense").classes("w-72")
        elif step.get("targets"):
            hint("Writes: " + ", ".join(step["targets"]))

    with section("SQL", "Hard-coded projects become ${write_project} / ${read_project}, datasets and dates become variables and "
                        "@parameters; the hub proves the result is equivalent to your SQL.", "code"):
        SqlPanel(step, index, state, svc, mode="sql", on_applied=lambda: (refresh(), dest_box.refresh())).render()

    with section("Statement and output", icon="output"):
        with ui.row().classes("gap-4 items-center"):
            ui.select(["SELECT", "DML", "MERGE", "DDL", "SCRIPT", "OTHER"], label="Kind of SQL", value=step.get("statement_kind"), on_change=lambda _e: dest_box.refresh()).bind_value(step, "statement_kind").props("outlined dense").classes("w-40")
            help_icon("Detected automatically. INSERT/UPDATE/DELETE = DML; several statements or DECLARE = SCRIPT.")
        dest_box()
    problems_box(state, index)


# ====================================================================== export
def export_form(step: dict, index: int, state: WizardState, svc: Any, refresh: Callable[[], None]) -> None:
    with section("What to export", icon="table_view"):
        @ui.refreshable
        def source_box() -> None:
            if step.get("source_kind") == "query":
                hint("Query results are written with EXPORT DATA, which always writes one or more files: name-*.ext.")
                SqlPanel(step, index, state, svc, mode="export", on_applied=refresh).render()
            else:
                earlier = state.tables_written_before(index)
                template_input(step, "source_table", "Table (project.dataset.table)", state, optional=True, tokens=False,
                               hint_text="e.g. {write_project}.{membership_dataset}.ms_acq_dm_active_members")
                if earlier:
                    with ui.row().classes("gap-1 items-center"):
                        ui.label("Built earlier in this process:").classes("sf-hint")
                        for t in earlier:
                            ui.button(t, on_click=lambda t=t: (step.update(source_table=t), source_box.refresh())).props("flat dense no-caps size=sm").classes("sf-mono")

        ui.toggle({"table": "A table (extract job)", "query": "The result of a query"}, value=step.get("source_kind"), on_change=lambda _e: source_box.refresh()).bind_value(step, "source_kind").props("no-caps dense")
        source_box()

    with section("Where to write the file", "Like the team's extract functions: bucket, path and file name come from the "
                                           "YAML; the script adds today's date and the extension.", "cloud_upload"):
        with ui.row().classes("w-full gap-4"):
            template_input(step, "bucket", "Bucket", state, classes="flex-1", hint_text="e.g. {ftp_bucket}")
            template_input(step, "path", "Folder", state, classes="flex-1", hint_text="e.g. {ftp_path} or outbound/rpm ('' = bucket root)")
        with ui.row().classes("w-full gap-4 items-start"):
            template_input(step, "file_name", "File name (before the date)", state, tokens=False, classes="flex-1",
                           hint_text="e.g. ms_acq_dm_active_members_")
            ui.select(DATE_SUFFIXES, label="Date suffix").bind_value(step, "date_suffix").props("outlined dense").classes("w-64")
            config_key_input(step)
        name_preview = ui.label().classes("sf-preview")

        def preview() -> None:
            ext = FILE_EXTENSIONS.get(step.get("file_format") or "CSV", "") + (".gz" if step.get("compression") == "GZIP" else "")
            suffix = dt.date.today().strftime(step["date_suffix"]) if step.get("date_suffix") else ""
            star = "-*" if (step.get("sharded") or step.get("source_kind") == "query") and step.get("file_format") != "XLSX" else ""
            lab = state.variable_values("lab")
            folder = fill_variables(step.get("path") or "", lab).strip("/")
            name_preview.text = (f"today in lab → gs://{fill_variables(step.get('bucket') or '', lab)}/"
                                 f"{folder + '/' if folder else ''}{fill_variables(step.get('file_name') or '', lab)}{suffix}{star}{ext}")

        ui.timer(1.0, preview)

        @ui.refreshable
        def format_box() -> None:
            fmt = step.get("file_format") or "CSV"
            allowed = sorted(EXPORT_COMPRESSION[fmt])
            if step.get("compression") not in allowed:
                step["compression"] = "NONE"
            with ui.row().classes("gap-4 items-center"):
                ui.select(EXPORT_FORMATS, label="Format", value=fmt, on_change=lambda e, fmt=fmt: on_export_format(e.value, fmt)).bind_value(step, "file_format").props("outlined dense").classes("w-56").mark("export-format")
                if fmt == "XLSX":
                    ui.input("Sheet", placeholder="Sheet1").bind_value(step, "sheet_name").props("outlined dense").classes(
                        "w-48").mark("export-sheet")
                    ui.switch("Header row").bind_value(step, "print_header")
                    help_icon("One .xlsx file, written with pandas and openpyxl by the script (BigQuery cannot extract "
                              "Excel): at most 1,048,575 rows, read into memory.")
                    return
                ui.select(allowed, label="Compression").bind_value(step, "compression").props("outlined dense").classes("w-36")
                if is_delimited(fmt):
                    ui.switch("Header row").bind_value(step, "print_header")
                    delimiter_input(step, "field_delimiter")
                ui.switch("Sharded files (name-*.ext)").bind_value(step, "sharded")
                help_icon("Tables over 1 GB must be exported to several files.")

        def on_export_format(value: str, previous: str) -> None:
            if value == previous:
                return
            if value == "TXT" and step.get("field_delimiter") in (None, ","):
                step["field_delimiter"] = "|"  # .txt extracts are usually pipe-delimited
            if value == "XLSX":
                step["sharded"] = False  # one workbook
            format_box.refresh()

        format_box()
        with ui.row().classes("items-center gap-2"):
            ui.switch("Attach this file to the success email").bind_value(step, "attach_to_email")
            if not state.spec.get("email", {}).get("enabled"):
                hint("(turn the email on under Email)")
    problems_box(state, index)


# ====================================================================== copy files
def copy_form(step: dict, index: int, state: WizardState, svc: Any, refresh: Callable[[], None]) -> None:
    rows = [{"v": s} for s in (step.get("sources") or [""])]  # inputs bind to dicts, not list items

    def sync() -> None:
        step["sources"] = [r["v"] or "" for r in rows]

    sync()

    def direction() -> str:
        return step.get("direction") or copy_direction(step.get("sources") or [], step.get("destination") or "") \
            or "bucket_to_bucket"

    def side(kind: str) -> tuple[str, str]:
        d = direction()
        bucket = d.startswith("bucket") if kind == "source" else d.endswith("bucket")
        return COPY_SIDES[f"{'bucket' if bucket else 'local'}_{kind}"]

    def on_direction(value: str) -> None:
        step["direction"] = value
        sources_box.refresh()
        destination_box.refresh()
        local_note.refresh()

    with section("What to copy", "The script runs the Google Cloud CLI (gcloud storage, the successor of gsutil). * matches "
                                 "several files and {date:%Y%m%d} is today's date.", "drive_file_move"):
        ui.toggle(COPY_DIRECTIONS, value=direction(), on_change=lambda e: on_direction(e.value)).props(
            "no-caps dense").mark(f"copy-direction-{index}")

        @ui.refreshable
        def local_note() -> None:
            if "local" in direction():
                hint("“Local” is the machine that runs the script (the pipeline server or Composer worker), not your "
                     "computer. Write full paths such as /data/exports/. A lab test run uses the hub's machine.")

        local_note()
        with ui.row().classes("gap-4 items-center"):
            ui.toggle(COPY_OPS, value=step.get("operation"), on_change=lambda _e: flags_box.refresh()).bind_value(step, "operation").props("no-caps dense")
            config_key_input(step)

        @ui.refreshable
        def sources_box() -> None:
            label, example = side("source")
            for i, row in enumerate(rows):
                with ui.row().classes("w-full gap-2 items-start no-wrap"):
                    inp = template_input(row, "v", label.format(n=i + 1), state, allow_date=True, classes="flex-1",
                                         hint_text=example)
                    inp.on_value_change(lambda _e: sync())
                    if len(rows) > 1:
                        ui.button(icon="delete_outline", on_click=lambda i=i: (rows.pop(i), sync(), sources_box.refresh())).props("flat dense round color=negative")
            if step.get("operation") != "rsync":
                ui.button("Add a source", icon="add", on_click=lambda: (rows.append({"v": ""}), sync(), sources_box.refresh())).props("flat dense no-caps").mark("copy-add-source")

        sources_box()

        @ui.refreshable
        def destination_box() -> None:
            label, example = side("destination")
            template_input(step, "destination", label, state, allow_date=True, hint_text=example)

        destination_box()

        @ui.refreshable
        def flags_box() -> None:
            with ui.row().classes("gap-4 items-center"):
                ui.switch("Recursive (folders)").bind_value(step, "recursive")
                ui.switch("Skip files that already exist (--no-clobber)").bind_value(step, "no_clobber")
                if step.get("operation") == "rsync":
                    ui.switch("Delete destination files that are not in the source").bind_value(step, "delete_unmatched")
                elif step.get("delete_unmatched"):
                    step["delete_unmatched"] = False

        flags_box()
        cmd = ui.code("", language="bash").classes("w-full sf-code")

        def preview() -> None:
            lab = state.variable_values("lab")
            flags = (" --recursive" if step.get("recursive") else "") + (" --no-clobber" if step.get("no_clobber") else "") + \
                (" --delete-unmatched-destination-objects" if step.get("delete_unmatched") else "")
            parts = [fill_variables(src, lab) for src in step.get("sources") or [] if src] + [fill_variables(step.get("destination") or "", lab)]
            cmd.set_content(f"# lab\ngcloud storage {step.get('operation') or 'cp'}{flags} " + " ".join(f'"{p}"' if " " in p else p for p in parts))

        ui.timer(1.0, preview)
        hint("The machine that runs the script needs gcloud installed and logged in as the same identity "
             "(Composer workers have it). A lab test run uses the hub's machine.")
    problems_box(state, index)


# ====================================================================== data quality
def dq_form(step: dict, index: int, state: WizardState, svc: Any, refresh: Callable[[], None]) -> None:
    checks = step.setdefault("checks", [])
    with section("Checks", "Each check runs one small query. Severity error stops the run; warn only logs.", "fact_check"):
        with ui.row().classes("gap-2 items-center"):
            with ui.button("Add a check", icon="add").props("unelevated no-caps"):
                with ui.menu():
                    for kind, label in DQ_KINDS.items():
                        ui.menu_item(label, on_click=lambda k=kind: (checks.append(new_check(k)), body.refresh()))

        @ui.refreshable
        def body() -> None:
            if not checks:
                hint("No checks yet. A good start: “At least N rows” and “Column combination is unique” on the table you built.")
            earlier = state.tables_written_before(index)
            for i, c in enumerate(checks):
                with ui.card().classes("sf-card w-full"):
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(f"{i + 1}. {DQ_KINDS[c['kind']]}").classes("font-medium")
                        ui.space()
                        ui.toggle({"error": "error", "warn": "warn"}).bind_value(c, "severity").props("dense no-caps")
                        ui.button(icon="delete_outline", on_click=lambda i=i: (checks.pop(i), body.refresh())).props("flat dense round color=negative")
                    if c["kind"] != "custom_sql":
                        with ui.row().classes("w-full gap-2 items-start"):
                            template_input(c, "table", "Table", state, optional=True, tokens=False, classes="flex-1",
                                           hint_text="variables only, e.g. {write_project}.{mart_dataset}.sales_daily")
                            if earlier:
                                with ui.button("pick", icon="list").props("flat dense no-caps"):
                                    with ui.menu():
                                        for t in earlier:
                                            ui.menu_item(t, on_click=lambda t=t, c=c: (c.update(table=t), body.refresh()))
                    if c["kind"] in ("not_null", "unique"):
                        ui.input("Columns (comma-separated)").bind_value(
                            c, "columns", forward=lambda v: [x.strip() for x in (v or "").split(",") if x.strip()],
                            backward=lambda v: ", ".join(v or [])).props("outlined dense").classes("w-full sf-mono")
                    if c["kind"] in ("accepted_values", "freshness"):
                        ui.input("Column").bind_value(c, "columns", forward=lambda v: [v.strip()] if v and v.strip() else [],
                                                      backward=lambda v: (v or [""])[0]).props("outlined dense").classes("w-72 sf-mono")
                    if c["kind"] == "row_count_min":
                        ui.number("Minimum rows", min=0, precision=0).bind_value(c, "min_rows", forward=lambda v: int(v or 0)).props("outlined dense").classes("w-40")
                    if c["kind"] == "accepted_values":
                        ui.input("Allowed values (comma-separated)").bind_value(
                            c, "accepted", forward=lambda v: [x.strip() for x in (v or "").split(",") if x.strip()],
                            backward=lambda v: ", ".join(v or [])).props("outlined dense").classes("w-full")
                    if c["kind"] == "freshness":
                        ui.number("Max age (hours)", min=1, precision=0).bind_value(c, "max_age_hours", forward=lambda v: int(v or 24)).props("outlined dense").classes("w-40")
                    if c["kind"] == "custom_sql":
                        hint("Write a query that returns the bad rows; the check passes when it returns none. ${var} is allowed.")
                        ui.codemirror(language="SQL").bind_value(c, "sql", forward=lambda v: v or "", backward=opt).classes("w-full h-32")
                        TablesCard(lambda c=c: c.get("sql") or "", state, step_name=step.get("function_name") or "",
                                   apply=lambda scan, c=c: apply_project_scan(c, "sql", scan, state, refresh)).render()
                    else:
                        ui.input("Only rows where… (optional SQL filter, e.g. sale_date = CURRENT_DATE())").bind_value(
                            c, "where", forward=lambda v: v or None, backward=opt).props("outlined dense").classes("w-full sf-mono")

        body()
    problems_box(state, index)


# ====================================================================== custom process (Gemini)
def _lines(value: Any) -> str:
    return "\n".join(value or [])


def _split_lines(value: str | None) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def custom_form(step: dict, index: int, state: WizardState, svc: Any, refresh: Callable[[], None]) -> None:
    step.setdefault("settings", [])
    applied = {"code": step.get("code") or ""}  # the code as Gemini (or the saved process) left it

    with section("What should this step do?", "Describe it in plain words: what it reads, what it connects to, what it "
                 "writes. Gemini writes the code inside the team's step function; you review and edit it.", "auto_awesome"):
        ui.textarea(placeholder="e.g. Take the API key from the secret geocode_api_key (the 'api' value of the first "
                    "'api_credentials' entry), geocode every address of the setting address_table with the maps API and "
                    "write member_id, latitude and longitude into the setting result_table").bind_value(step, "request").props(
            "outlined autogrow").classes("w-full").mark("custom-request")
        secrets = [x for x in state.spec.get("secrets") or [] if x.get("name")]
        with ui.row().classes("items-center gap-1"):
            ui.label("Secrets it can use:").classes("sf-hint")
            for sec in secrets:
                chip = ui.chip(f"SECRETS['{sec['name']}']", icon="key").props("outline dense").classes("sf-mono")
                if sec.get("description"):
                    chip.tooltip(sec["description"])
            if not secrets:
                hint("none yet: add them under Variables & secrets (Google Secret Manager), then ask Gemini")
        if secrets:
            hint("Each holds the secret's whole payload (a dict for JSON, e.g. {\"api_credentials\": [{\"api\": …}]}). Gemini "
                 "reads the payload keys from this step's settings (secret_list_key, secret_value_key), so a new layout "
                 "of the secret is a YAML change.")

    with section("Settings", "Values the code reads, written into this step's block of the YAML for each environment. "
                 "Use {variables} where lab and prod differ. Passwords and API keys belong in Secret Manager, not here.", "tune"):
        with ui.row().classes("items-center gap-2"):
            config_key_input(step)

        @ui.refreshable
        def settings_box() -> None:
            for i, row in enumerate(step["settings"]):
                with ui.row().classes("w-full gap-2 items-start no-wrap"):
                    ui.input("Key").bind_value(row, "key").props("outlined dense").classes("w-48 sf-mono")
                    template_input(row, "value", "Value", state, tokens=False, classes="flex-1",
                                   hint_text=row.get("description") or "a literal, or {variables} that differ per environment")
                    ui.button(icon="delete_outline", on_click=lambda i=i: (step["settings"].pop(i), settings_box.refresh())).props(
                        "flat dense round color=negative")
            ui.button("Add a setting", icon="add", on_click=lambda: (
                step["settings"].append({"key": "", "value": "", "description": ""}), settings_box.refresh())).props(
                "flat dense no-caps").mark("custom-add-setting")

        settings_box()

    with section("Code", "Only the statements inside try: — the hub adds the def, the begin/end logging, the timing and the "
                 "error handling, exactly like every other step.", "code"):
        with ui.row().classes("items-center gap-2"):
            write_btn = ui.button("Write the code with Gemini", icon="auto_awesome", on_click=lambda: write()).props(
                "unelevated no-caps color=purple").mark("custom-write")
            ui.button("Check the code", icon="rule", on_click=lambda: check()).props("outline no-caps").mark("custom-check")
            author = ui.label().classes("sf-hint")
        if svc.gemini is None:
            write_btn.disable()
            hint("Gemini is off on this hub: write the code yourself.")
        status = ui.column().classes("w-full gap-1")

        def on_code(e) -> None:
            by = step.get("generated_by") or ""
            if (e.value or "") != applied["code"] and by and not by.endswith("edited by hand"):
                step["generated_by"] = f"{by}, edited by hand"

        @ui.refreshable
        def code_box() -> None:
            by = step.get("generated_by")
            author.text = f"Written with Gemini ({by})" if by else "Written by hand"
            ui.codemirror(step.get("code") or "", language="Python", on_change=on_code).bind_value(
                step, "code").classes("w-full h-80").mark("custom-code")
            with ui.row().classes("w-full gap-4 items-start no-wrap"):
                ui.textarea("Imports (one per line, added to # Base imports)").bind_value(
                    step, "imports", forward=_split_lines, backward=_lines).props("outlined autogrow").classes(
                    "flex-1 sf-mono").mark("custom-imports")
                ui.textarea("Packages (requirements.txt, one per line)").bind_value(
                    step, "requirements", forward=_split_lines, backward=_lines).props("outlined autogrow").classes(
                    "flex-1 sf-mono")

        code_box()
        with ui.expansion("The function as the script will hold it", icon="preview").classes("w-full"):
            preview = ui.code("", language="python").classes("w-full sf-code")
        last = {"key": None}

        def update_preview() -> None:
            key = (step.get("function_name"), step.get("config_key"), step.get("description"), step.get("code"))
            if key != last["key"]:
                last["key"] = key
                preview.set_content(render_custom_preview(step))

        ui.timer(1.5, update_preview)

        def apply(new: dict) -> None:
            for k in ("code", "imports", "requirements", "generated_by", "settings"):
                step[k] = new[k]
            applied["code"] = new["code"]
            code_box.refresh()
            settings_box.refresh()

        def show(problems: list[str], notes: list[str]) -> None:
            for p in problems:
                with ui.row().classes("items-start gap-1 no-wrap"):
                    level_icon("error")
                    ui.label(p).classes("text-sm whitespace-pre-wrap")
            for n in notes:
                with ui.row().classes("items-start gap-1 no-wrap"):
                    level_icon("info")
                    ui.label(n).classes("text-sm")

        async def write() -> None:
            status.clear()
            with status:
                ui.spinner()
                ui.label("Gemini is writing the code and the hub is checking it…").classes("sf-hint")
            r = await run.io_bound(write_custom_code, state.spec, index, svc.gemini, svc.settings)
            status.clear()
            with status:
                if r.ok and r.step is not None:
                    apply(r.step)
                    with ui.row().classes("items-center gap-1"):
                        level_icon("ok")
                        ui.label(f"Gemini wrote the code ({r.model}) and it passes the hub's checks. Review it, then fill "
                                 "in any empty setting.").classes("text-sm")
                elif r.step is not None:
                    ui.label(f"Gemini's code still fails the hub's checks after {r.attempts} attempt(s):").classes(
                        "text-sm font-semibold text-red-800")
                    candidate = r.step
                    ui.button("Put it in the editor anyway (fix it by hand)", icon="edit",
                              on_click=lambda: (apply(candidate), status.clear())).props("flat dense no-caps")
                show(r.problems, [f"Check: {n}" for n in r.notes])

        async def check() -> None:
            status.clear()
            problems = await run.io_bound(check_custom_step, state.spec, index, svc.settings)
            with status:
                if not problems:
                    with ui.row().classes("items-center gap-1"):
                        level_icon("ok")
                        ui.label("The code compiles, passes ruff and follows the custom-code rules (the lab test run "
                                 "proves it works).").classes("text-sm")
                show(problems, [])
    problems_box(state, index)


FORMS = {"ingest": ingest_form, "sql": sql_form, "export": export_form, "copy": copy_form, "dq": dq_form,
         "custom": custom_form}
