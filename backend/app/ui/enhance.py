"""The "Enhance a script" page: upload an existing script (and its YAML files), see how far it is from the team
standard, then open it as a hub process (when every step maps to a hub step) or have Gemini rewrite it in place."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import PurePath
from typing import Any, Callable

from nicegui import run, ui
from pydantic import ValidationError

from ..models.enhance import EnhanceHeader
from ..models.rules import PROCESS_NAME_RE, to_snake_case
from ..services.packager import build_zip
from ..services.script_convert import ConvertError, to_spec
from ..services.script_rewrite import rewrite_script
from ..services.script_scan import classify_yaml, scan_script
from .components import list_input, render_files
from .theme import hint, level_icon, section

KIND_LABELS = {"sql": "SQL step", "export": "Export step", "copy": "Copy step", "ingest": "File ingestion step",
               "unmapped": "custom code", "email": "email", "helper": "helper", "auth": "credentials (replaced by ADC)"}
FILE_ROLES = {"script": "Script", "config": "YAML", "schema": "Table schema YAML"}


def _text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding).replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def suggest_process_name(stem: str) -> str:
    name = re.sub(r"^[^a-z]+", "", to_snake_case(stem).lower())[:63]
    return name if PROCESS_NAME_RE.match(name) else "enhanced_process"


class EnhancePage:
    def __init__(self, svc: Any, user: str, stash: Callable[..., str]):
        self.svc, self.user, self.stash = svc, user, stash
        self.files: dict[str, tuple[str, str]] = {}  # role -> (file name, text)
        self.analysis = None
        self.header: dict[str, Any] = {}  # the header block to attach (EnhanceHeader fields), filled from the old header
        self.rewrite = None

    def build(self) -> None:
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("auto_fix_high", size="md").classes("text-primary")
            ui.label("Enhance a script").classes("text-2xl font-semibold")
        hint("Bring an existing pipeline script up to the team standard. The hub reads it without running anything. When "
             "every step maps to a hub step (SQL, export, copy, file ingestion) it becomes a process you validate, test in "
             "lab and generate; otherwise Gemini rewrites it in place and the hub checks the result (same SQL, same "
             "functions, clean code) before you see the diff. Either way the script keeps its author header block "
             "(you check it under Script header) and reaches Google with ADC/IAM instead of service-account keys.")
        with section("1 · Upload", "The .py script, and if it has them its YAML (gcp_projects) and table schema YAML "
                     "(-ts). Nothing is stored until you open the result as a process.", "upload_file"):
            ui.upload(on_upload=self.on_upload, auto_upload=True, multiple=True, label="Drop the .py and .yaml files here",
                      max_file_size=self.svc.settings.enhance_max_script_kb * 1024).props(
                "accept=.py,.yaml,.yml flat").classes("w-96").mark("enhance-upload")
            self.files_view()
            with ui.row().classes("gap-2 items-center"):
                ui.button("Analyze", icon="manage_search", on_click=self.analyze).props("unelevated no-caps").mark("enhance-analyze")
        self.report_view()
        self.rewrite_view()

    # ------------------------------------------------------------------ files
    async def on_upload(self, e: Any) -> None:
        name, text = e.file.name, _text(await e.file.read())
        if name.lower().endswith(".py"):
            role = "script"
        elif name.lower().endswith((".yaml", ".yml")):
            role = classify_yaml(text)
            if not role:
                ui.notify(f"{name}: neither a gcp_projects config nor a table_definition schema", type="warning")
                return
        else:
            ui.notify(f"{name}: upload .py and .yaml files", type="warning")
            return
        self.files[role] = (name, text)
        self.analysis = self.rewrite = None
        self.files_view.refresh()
        self.report_view.refresh()
        self.rewrite_view.refresh()

    @ui.refreshable_method
    def files_view(self) -> None:
        if not self.files:
            hint("No files yet.")
            return
        with ui.row().classes("gap-2"):
            for role, (name, text) in self.files.items():
                ui.chip(f"{FILE_ROLES[role]}: {name} ({text.count(chr(10)) + 1:,} lines)", icon="description",
                        removable=True, on_value_change=lambda _e, r=role: self.remove(r)).props("outline").mark(f"enhance-file-{role}")

    def remove(self, role: str) -> None:
        self.files.pop(role, None)
        self.analysis = self.rewrite = None
        self.files_view.refresh()
        self.report_view.refresh()
        self.rewrite_view.refresh()

    # ------------------------------------------------------------------ analysis
    async def analyze(self) -> None:
        if "script" not in self.files:
            ui.notify("Upload the .py script first", type="warning")
            return
        name, source = self.files["script"]
        self.analysis = await run.io_bound(
            scan_script, source, name, config_yaml=self.files.get("config", ("", None))[1],
            schema_yaml=self.files.get("schema", ("", None))[1], settings=self.svc.settings)
        self.header = EnhanceHeader.from_analysis(self.analysis).model_dump()
        self.rewrite = None
        self.report_view.refresh()
        self.rewrite_view.refresh()

    @ui.refreshable_method
    def report_view(self) -> None:
        a = self.analysis
        if a is None:
            return
        met = sum(1 for s in a.standards if s.ok)
        with section("2 · What the hub found", f"{a.file_name}: {met} of {len(a.standards)} rules of the team standard met.",
                     "fact_check"):
            with ui.column().classes("w-full gap-1").mark("enhance-standards"):
                for s in a.standards:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        level_icon("ok" if s.ok else "warn")
                        ui.label(s.title + (f": {s.detail}" if s.detail else "")).classes("text-sm")
            if a.steps():
                ui.label("Steps, in the order main() calls them").classes("font-semibold mt-2")
                for f in a.steps():
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        level_icon("error" if f.kind == "unmapped" else "ok")
                        ui.label(f"{f.name}()").classes("sf-mono text-sm w-72")
                        ui.label(KIND_LABELS[f.kind] + (": " + "; ".join(f.reasons) if f.reasons else "")).classes("text-sm")
            for note in a.notes:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    level_icon("info")
                    ui.label(note).classes("text-sm")
        if not a.parse_error:
            self.header_section()
        with ui.card().classes("sf-card w-full bg-slate-50 gap-2").mark("enhance-verdict"):
            if a.convertible:
                ui.label("Every step maps to a hub step: it becomes a process you validate, test in lab and generate "
                         "with today's templates.").classes("font-semibold")
                with ui.row().classes("items-center gap-2"):
                    self.name_input = ui.input("Process name", value=suggest_process_name(a.stem), validation=lambda v: None if
                                               PROCESS_NAME_RE.match(v or "") else "lower_snake_case, 3-63 characters").props(
                        "outlined dense").classes("w-72 sf-mono").mark("enhance-process-name")
                    ui.button("Open in the wizard", icon="open_in_new", on_click=self.convert).props(
                        "unelevated no-caps").mark("enhance-convert")
            else:
                ui.label("It cannot become a hub process as it is:").classes("font-semibold")
                for b in a.blockers:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        level_icon("error")
                        ui.label(b).classes("text-sm")
                with ui.row().classes("items-center gap-2"):
                    button = ui.button("Rewrite with AI", icon="auto_awesome", on_click=self.do_rewrite).props(
                        "unelevated no-caps color=accent").mark("enhance-rewrite")
                    if a.parse_error:
                        button.disable()
                        hint("Fix the syntax error first.")
                    elif self.svc.gemini is None:
                        button.disable()
                        hint("Rewriting needs Gemini, which is turned off on this hub.")
                    else:
                        hint("Gemini restructures the script to the team standard (Google access becomes ADC); the hub "
                             "keeps it only if every SQL, every function and the YAML layout survive, the code lints "
                             "clean and no service-account key is left. The hub writes the header block above.")

    def header_section(self) -> None:
        """The header block the enhanced script (and the process) gets: the old header's values, editable, plus an
        Updates line for this enhancement."""
        h = self.header
        with section("3 · Script header", "The ''' block on top of the enhanced script (and the process's header): "
                     "read from the old script, check and complete it. An Updates line records this enhancement.",
                     "assignment_ind"):
            with ui.row().classes("w-full gap-4 items-center"):
                list_input("Author(s): PIDs, comma separated", h, "authors", placeholder="P10000").mark("enhance-authors")
                list_input("Reviewer(s): PIDs, comma separated", h, "reviewers", placeholder="P12345").mark("enhance-reviewers")
                ui.input("JIRA# or Service Now#").bind_value(h, "jira").props("outlined dense").classes(
                    "w-56 sf-mono").mark("enhance-jira")
                ui.input("Created on (first deployment)").bind_value(
                    h, "created_on", forward=lambda v: v or None, backward=lambda v: v or "").props(
                    "outlined dense type=date stack-label").classes("w-56")
            ui.textarea("Description — the program's purpose").bind_value(h, "description").props(
                "outlined autogrow").classes("w-full").mark("enhance-description")
            if h.get("updates"):
                ui.label("Updates lines kept from the old header").classes("sf-hint")
                for u in h["updates"]:
                    ui.label(f"[{u.get('date')}, {u.get('pid')}]: {u.get('note')}").classes("sf-mono text-sm")
            with ui.row().classes("w-full gap-2 items-center no-wrap"):
                ui.label(f"New Updates line [{dt.date.today().isoformat()}, PID]:").classes("sf-hint")
                ui.input("PID", placeholder="your PID").bind_value(h, "update_pid").props("outlined dense").classes(
                    "w-32 sf-mono").mark("enhance-update-pid")
                ui.input("What changed").bind_value(h, "update_note").props("outlined dense").classes("flex-grow")
            if not h.get("authors"):
                hint("The old script names no author: enter the PID(s) before you open or rewrite it.")

    def checked_header(self) -> EnhanceHeader | None:
        """The header to attach; None (with a warning) when it is incomplete."""
        try:
            header = EnhanceHeader.model_validate(self.header)
        except ValidationError as exc:
            ui.notify(f"Script header: {exc.errors()[0]['msg']}", type="warning")
            return None
        if not header.authors:
            ui.notify("Enter the author PID(s) under 3 · Script header: they go into the header block", type="warning")
            return None
        return header

    def convert(self) -> None:
        name = (self.name_input.value or "").strip()
        if not PROCESS_NAME_RE.match(name):
            ui.notify("Give the process a lower_snake_case name, 3-63 characters", type="warning")
            return
        header = self.checked_header()
        if header is None:
            return
        try:
            spec, notes = to_spec(self.analysis, process_name=name, owner=self.user, header=header)
        except ConvertError as exc:
            ui.notify("It cannot be converted: " + "; ".join(exc.problems), type="negative", multi_line=True)
            return
        token = self.stash(spec, notes, f"Imported from {self.analysis.file_name}: please review before you validate")
        ui.navigate.to(f"/new?import={token}")

    # ------------------------------------------------------------------ rewrite
    async def do_rewrite(self) -> None:
        header = self.checked_header()
        if header is None:
            return
        name, source = self.files["script"]
        self.rewrite = "running"
        self.rewrite_view.refresh()
        self.rewrite = await run.io_bound(rewrite_script, self.analysis, source, self.svc.gemini,
                                          config_yaml=self.files.get("config", ("", None))[1], settings=self.svc.settings,
                                          header=header)
        self.rewrite_view.refresh()

    @ui.refreshable_method
    def rewrite_view(self) -> None:
        r = self.rewrite
        if r is None:
            return
        with section("4 · Rewrite", "Checked by the hub before you see it. Review the diff before you deploy.", "auto_awesome"):
            if r == "running":
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="lg")
                    ui.label("Gemini is rewriting the script…").classes("sf-hint")
                return
            with ui.row().classes("gap-3 items-center").mark("enhance-checks"):
                for c in r.checks:
                    with ui.row().classes("items-center gap-1"):
                        level_icon("ok" if c.ok else "error")
                        ui.label(c.name).classes("text-sm").tooltip(c.detail or "")
            if not r.ok:
                ui.label("Not accepted:").classes("font-semibold text-red-800")
                for p in r.problems:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        level_icon("error")
                        ui.label(p).classes("text-sm whitespace-pre-wrap")
                if r.checks:
                    ui.button("Try again", icon="refresh", on_click=self.do_rewrite).props("outline no-caps")
            for title, items in (("What changed", r.changes), ("Still to do", r.todos)):
                if items:
                    ui.label(title).classes("font-semibold")
                    for item in items:
                        ui.label(f"• {item}").classes("text-sm")
            if r.diff:
                with ui.expansion("Diff", icon="difference", value=r.ok).classes("w-full sf-card"):
                    ui.code(r.diff, language="diff").classes("w-full sf-code")
            if r.ok:
                ui.button("Download ZIP", icon="download", on_click=lambda: ui.download.content(
                    build_zip(r.stem, r.files), f"{r.stem}_enhanced.zip", "application/zip")).props("unelevated no-caps").mark("enhance-download")
                render_files([{"path": p, "content": c, "language": {".py": "python", ".yaml": "yaml", ".md": "markdown"}.get(
                    PurePath(p).suffix, "text")} for p, c in sorted(r.files.items())])
