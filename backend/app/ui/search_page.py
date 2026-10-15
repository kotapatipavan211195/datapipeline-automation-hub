"""The "Find files" page: search one or more buckets for a file name or pattern, in every folder (read-only).

The buckets follow the Copy files rule (services/copy_tool.py): your own credentials on a laptop, the lab-allowlisted
buckets on a deployed hub. Matching rules are in services/file_search.py. A file found can start a new pipeline with
an ingestion of the files like it (same folder, same name prefix, same format).
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Callable

from nicegui import run, ui

from ..models.rules import split_gs_uri
from ..models.spec import INGEST_EXTENSIONS
from ..services.file_search import search_files
from ..services.gcp_tools import infer_from_gcs
from ..services.schema_infer import InferResult
from ..services.validators import gcp_error_text
from .state import blank_spec, new_step
from .theme import hint, human_bytes, level_icon, section

PATTERN_HELP = ("Plain text: the file name contains it. With * ? [ ]: the whole file name matches (*.csv, "
                "MBR_EXTRACT_2026??.txt). With a /: the path matches from any folder down (inbound/*.csv). "
                "Every folder of each bucket is searched; give a folder (gs://bucket/folder/) to search less.")


# file extension → ingestion format (.txt is delimited text; .dat is fixed width)
INGEST_FORMATS = {ext: fmt for fmt, exts in reversed(list(INGEST_EXTENSIONS.items())) for ext in exts}


def file_name_prefix(name: str) -> str:
    """The part of a file name the files of a feed share: 'Daily File_20260115.csv' → 'Daily File_'."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    m = re.match(r"^(.*?[A-Za-z][^\d]*?)\d[\d_\-. ]*$", stem)
    return m.group(1) if m else stem


def ingestion_spec(uri: str, owner: str, default_projects: dict[str, str] | None, inferred: InferResult | None,
                   problem: str | None) -> tuple[dict[str, Any], list[str]]:
    """A new pipeline with one ingestion step for the files like ``uri`` (lab values only; prod is left to fill)."""
    bucket, path = split_gs_uri(uri)
    folder, _, name = path.rpartition("/")
    prefix = file_name_prefix(name)
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    file_format = INGEST_FORMATS[ext]
    table = re.sub(r"[^a-z0-9]+", "_", prefix.lower()).strip("_")[:50] or "file"
    if not table[0].isalpha():
        table = f"file_{table}"[:50]
    spec = blank_spec(owner, "pipeline", default_projects)
    spec["metadata"]["name"] = f"{table}_ingest"
    spec["metadata"]["description"] = f"Loads the '{prefix}' files from gs://{bucket}/{folder}".rstrip("/")
    spec["variables"].append({"name": "landing_bucket", "group": "bucket", "description": "where the files arrive",
                              "values": {"lab": bucket, "value": "", "qa": ""}})
    if folder:
        spec["variables"].append({"name": "landing_path", "group": "other", "description": "folder of the files",
                                  "values": {"lab": folder, "value": "", "qa": ""}})
    step = new_step("ingest", [])
    step.update(function_name=f"ingest_{table}", description=f"Load the '{prefix}' files and archive them",
                read_bucket="{landing_bucket}", read_path="{landing_path}" if folder else "", filename=prefix,
                file_format=file_format, table_name=table,
                archive_path="{landing_path}/Archive" if folder else "Archive")
    if file_format == "FIXED":
        step["header"] = "None"
    notes = [f"Started from {uri} (Find files): the step loads every file in gs://{bucket}/{folder} whose name "
             f"contains '{prefix}' and ends with {', '.join(INGEST_EXTENSIONS[file_format])}.",
             f"landing_bucket{' and landing_path' if folder else ''} hold the lab values found; enter their value (prod) "
             f"under Variables & secrets (and check that {bucket} is a lab bucket)."]
    if inferred is not None:
        step["columns"] = [c.model_dump() for c in inferred.columns]
        if inferred.detected.get("delimiter"):
            step["separator"] = inferred.detected["delimiter"]
        notes.append(f"{len(inferred.columns)} column(s) read from the file: check their names and types under Table schema.")
    elif file_format == "FIXED":
        notes.append("Fixed-width file: paste the layout (name and length per column) under Table schema → Import columns.")
    else:
        notes.append(f"The columns could not be read ({problem}): import them under Table schema.")
    notes.append("Choose the staging dataset and the history table's dataset under the step's Tables. The raw rows "
                 "stay in staging for the next steps unless you turn on a target table.")
    spec["steps"] = [step]
    return spec, notes


class SearchPage:
    def __init__(self, svc: Any, user: str, stash: Callable[..., str] | None = None):
        self.svc, self.user, self.stash = svc, user, stash
        self.form = {"buckets": "", "pattern": "", "case": False}
        self.result: dict[str, Any] | None = None
        self.searching = False

    async def build(self) -> None:
        policy = self.svc.copy_jobs.policy()
        identity = await run.io_bound(self.svc.adc_email)
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("manage_search", size="md").classes("text-primary")
            ui.label("Find files").classes("text-2xl font-semibold")
            ui.chip("your credentials" if policy.scope == "any" else "lab buckets only", icon="key",
                    color="primary" if policy.scope == "any" else "warning").props("outline").mark("search-scope")
        hint("Search one or more buckets for a file name or a name pattern, at any folder depth. Nothing is changed. "
             + policy.describe(identity))
        if not self.svc.settings.file_search_enabled:
            with ui.card().classes("sf-card w-full bg-amber-50"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    level_icon("warn")
                    ui.label("Find files is turned off on this hub (SF_FILE_SEARCH_ENABLED).").classes("text-sm")
            return
        with section("Search", icon="search"):
            with ui.row().classes("w-full gap-3 items-center no-wrap"):
                ui.input("Buckets", placeholder="bucket-a, gs://bucket-b/inbound/").bind_value(self.form, "buckets").props(
                    "outlined dense clearable").classes("flex-grow sf-mono").mark("search-buckets").on(
                    "keydown.enter", self.search)
                ui.input("File name or pattern", placeholder="Daily_Engagement   or   *.csv").bind_value(self.form, "pattern").props(
                    "outlined dense clearable").classes("w-80 sf-mono").mark("search-pattern").on("keydown.enter", self.search)
                ui.switch("Match case").bind_value(self.form, "case").mark("search-case")
                ui.button("Search", icon="search", on_click=self.search).props("unelevated no-caps").mark("search-run")
            hint("Buckets: names or gs:// paths, separated by commas. " + PATTERN_HELP)
            if policy.scope == "allowlist":
                hint("This hub may search: " + (", ".join(policy.patterns) or "no bucket yet"))
        with section("Results", icon="list"):
            self.results_view()

    async def search(self) -> None:
        if self.searching:
            return
        self.searching = True
        self.results_view.refresh()
        try:
            result = await run.io_bound(
                search_files, self.svc.clients, self.svc.settings, project=self.svc.copy_project(),
                buckets=self.form["buckets"] or "", pattern=self.form["pattern"] or "", case_sensitive=bool(self.form["case"]))
            self.result = result.as_dict()
        finally:
            self.searching = False
        self.results_view.refresh()

    @ui.refreshable_method
    def results_view(self) -> None:
        if self.searching:
            with ui.row().classes("items-center gap-2"):
                ui.spinner(size="md")
                ui.label("Searching… large buckets take a while (every folder is listed).").classes("sf-hint")
            return
        r = self.result
        if r is None:
            hint("The files found appear here, with their gs:// path, size and last update.")
            return
        for message in r["problems"]:
            with ui.row().classes("items-center gap-1 no-wrap"):
                level_icon("error")
                ui.label(message).classes("text-sm text-red-800")
        for message in r["notes"]:
            with ui.row().classes("items-center gap-1 no-wrap"):
                level_icon("warn")
                ui.label(message).classes("text-sm")
        files = r["files"]
        if not r["targets"] or (r["problems"] and not r["scanned"]):
            return
        where = f"{len(r['targets'])} bucket{'s' if len(r['targets']) != 1 else ''}"
        ui.label(f"{len(files):,} file{'s' if len(files) != 1 else ''} found in {where} · looked at {r['scanned']:,} "
                 f"files in {r['seconds']:.1f} s").classes("font-medium").mark("search-summary")
        if not files:
            hint("Nothing matches. Check the spelling, turn Match case off, or use * as a wildcard.")
            return
        rows = [{"uri": f["uri"], "bucket": f["bucket"], "size": human_bytes(f["size"]), "bytes": f["size"],
                 "updated": (f["updated"] or "")[:19].replace("T", " ")} for f in files]
        with ui.row().classes("w-full items-center gap-2"):
            table_filter = ui.input(placeholder="Filter the results…").props("outlined dense clearable").classes("w-72")
            ingest_btn = ui.button("Start an ingestion with the selected file", icon="upload_file",
                                   on_click=lambda: self.start_ingestion(table.selected)).props(
                "unelevated dense no-caps").mark("search-ingest")
            ui.space()
            ui.button("Copy all paths", icon="content_copy",
                      on_click=lambda: (ui.clipboard.write("\n".join(f["uri"] for f in files)), ui.notify("Copied"))).props(
                "flat dense no-caps").mark("search-copy-all")
            ui.button("Download CSV", icon="download", on_click=lambda: self.download_csv(files)).props(
                "flat dense no-caps").mark("search-csv")
        columns = [
            {"name": "uri", "label": "gs:// path", "field": "uri", "sortable": True, "align": "left"},
            {"name": "size", "label": "Size", "field": "size", "sortable": True, "align": "right",
             ":sort": "(a, b, rowA, rowB) => rowA.bytes - rowB.bytes"},
            {"name": "updated", "label": "Updated (UTC)", "field": "updated", "sortable": True, "align": "left"},
        ]
        table = ui.table(columns=columns, rows=rows, row_key="uri", pagination=50, selection="single").classes(
            "w-full sf-mono text-xs").mark("search-results")
        ingest_btn.bind_enabled_from(table, "selected", backward=bool)
        hint("Select a file to start a new pipeline that ingests the files like it (same folder, same name prefix, "
             "same format): CSV, TXT, DAT (fixed width), JSON lines, Parquet or Excel.")
        table.add_slot("body-cell-uri", r'''
            <q-td :props="props">
                <q-btn flat dense round size="xs" icon="content_copy" @click="() => $parent.$emit('copy', props.row.uri)" />
                {{ props.row.uri }}
            </q-td>
        ''')
        table.on("copy", lambda e: (ui.clipboard.write(e.args), ui.notify("Copied")))
        table.bind_filter_from(table_filter, "value")

    async def start_ingestion(self, selected: list[dict[str, Any]]) -> None:
        """New pipeline (/new) with an ingestion step for the files like the selected one, its columns read in GCS."""
        if not selected:
            return
        uri = selected[0]["uri"]
        name = uri.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in INGEST_FORMATS:
            ui.notify(f"{name}: the ingestion reads CSV, TXT, DAT (fixed width), JSON lines, Parquet and Excel files",
                      type="warning")
            return
        inferred, problem = None, None
        if INGEST_FORMATS[ext] != "FIXED":
            try:
                inferred = await run.io_bound(
                    infer_from_gcs, self.svc.clients, project_id=self.svc.copy_project(), uri=uri,
                    file_format=INGEST_FORMATS[ext], delimiter=None, has_header=True,
                    sample_bytes=self.svc.settings.sample_read_bytes)
            except Exception as exc:  # noqa: BLE001
                problem = gcp_error_text(exc)
        spec, notes = ingestion_spec(uri, self.user, self.svc.settings.default_projects(), inferred, problem)
        if self.stash is None:
            return
        token = self.stash(spec, notes, "New ingestion from a file found with Find files. Please review:")
        ui.navigate.to(f"/new?import={token}")

    @staticmethod
    def download_csv(files: list[dict[str, Any]]) -> None:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["uri", "bucket", "name", "size_bytes", "updated_utc"])
        for f in files:
            writer.writerow([f["uri"], f["bucket"], f["name"], f["size"], f["updated"]])
        ui.download.content(out.getvalue().encode("utf-8"), "found_files.csv", "text/csv")
