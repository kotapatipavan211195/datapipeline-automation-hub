"""The process wizard: Basics → Variables → Pipeline → Step details → Email → Review & test (validate, test in lab, generate).

A copy-files process (process_type "copy") swaps Pipeline + Step details for a single Copy jobs page."""

from __future__ import annotations

import datetime as dt
import json
import re
from types import SimpleNamespace
from typing import Any

from nicegui import run, ui

from ..models.rules import function_name_problem, parse_receivers, script_name_problem, suggest_function_name
from ..models.spec import BUILTIN_PROJECTS, ENV_LABELS, GSM_PROJECT, ProcessSpec
from ..services.docs_writer import ai_overview
from ..services.packager import build_zip, bundle_stem
from ..services.renderer import RenderError, render_main_preview, render_process, run_command
from ..services.test_runner import TestRunRefused, side_effects
from ..services.validators import order_checks
from ..services.workspace import WorkspaceError
from .components import list_input, render_files, render_report
from .state import STEP_KINDS, TEMPLATES, WizardState, empty_values, new_step
from .step_forms import FORMS, copy_form
from .theme import help_icon, hint, level_icon, section

GROUPS = {"project": "project", "dataset": "dataset", "bucket": "bucket", "other": "other"}
STATUS_COLORS = {"running": "primary", "passed": "positive", "failed": "negative", "timeout": "negative",
                 "cancelled": "grey", "error": "negative", "skipped": "warning"}


class Wizard:
    def __init__(self, state: WizardState, svc: Any):
        self.state, self.svc = state, svc
        self.stepper: ui.stepper | None = None
        self._log_next = 0
        self._poll: ui.timer | None = None
        self._adc: str | None = None  # the hub's ADC email, looked up once (test runs and their gates)

    # ================================================================== layout
    def build(self) -> None:
        s = self.state
        with ui.row().classes("w-full items-center gap-3"):
            ui.label().bind_text_from(s.spec["metadata"], "name", backward=lambda v: v or "New process").classes("text-2xl font-semibold sf-mono")
            if s.is_copy:
                ui.chip("Copy files process", icon="drive_file_move", color="brown").props("outline")
            if not s.can_edit:
                ui.chip("read-only — clone to edit", icon="lock", color="warning").props("outline")
            elif s.is_new:
                ui.chip("not created yet", icon="fiber_new").props("outline")
            ui.space()
            self.status_label = ui.label().classes("sf-hint")
            self.problem_chip = ui.chip("", icon="error_outline", color="negative").props("outline")
            self.problem_chip.visible = False
        if s.migration_notes:
            with ui.card().classes("sf-card w-full bg-amber-50").mark("migration-notes"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("upgrade").classes("text-amber-800")
                    ui.label(s.notes_title).classes("font-semibold")
                for note in s.migration_notes:
                    ui.label(f"• {note}").classes("text-sm")
        with ui.stepper().props("header-nav animated alternative-labels").classes("w-full sf-card") as stepper:
            self.stepper = stepper
            with ui.step("Basics", icon="badge"):
                self.stage_basics()
                self.nav("basics", first=True)
            with ui.step("Variables & secrets", icon="data_object"):
                self.stage_variables()
                self.nav("variables")
            if s.is_copy:
                with ui.step("Copy jobs", icon="drive_file_move"):
                    self.stage_copy_jobs()
                    self.nav("copy-jobs")
            else:
                with ui.step("Pipeline", icon="account_tree"):
                    self.stage_pipeline()
                    self.nav("pipeline")
                with ui.step("Step details", icon="tune"):
                    self.stage_details()
                    self.nav("details")
            with ui.step("Email", icon="mail"):
                self.stage_email()
                self.nav("email")
            with ui.step("Review & test", icon="rocket_launch"):
                self.stage_review()
                self.nav("review", last=True)
        ui.timer(2.0, self.tick)

    def nav(self, stage: str, first: bool = False, last: bool = False) -> None:
        with ui.stepper_navigation():
            if not last:
                ui.button("Next", icon="arrow_forward", on_click=self.next_stage).props("unelevated no-caps").mark(f"next-{stage}")
            if not first:
                ui.button("Back", on_click=self.stepper.previous).props("flat no-caps").mark(f"back-{stage}")

    async def next_stage(self) -> None:
        if self.stepper.value == "Basics" and self.state.is_new:
            if not await self.create_process():
                return
        self.stepper.next()

    # ================================================================== background: validation + autosave
    async def tick(self) -> None:
        s = self.state
        if self._adc is None:
            self._adc = await run.io_bound(self.svc.adc_email)
        s.validate()
        digest = s.digest()
        if digest != getattr(self, "_last_digest", None):
            self._last_digest = digest
            self.refresh_pipeline_side()
        self.refresh_gates()
        names = tuple(v.get("name") for v in s.variables)
        if names != getattr(self, "_last_names", names):  # e.g. a SQL scan added variables
            self.variables_view.refresh()
            self.environments_view.refresh()
        self._last_names = names
        n = len(s.errors)
        self.problem_chip.visible = n > 0
        self.problem_chip.text = f"{n} problem{'s' if n != 1 else ''} to fix"
        if s.can_edit and not s.is_new and s.dirty:
            try:
                saved_at = await run.io_bound(self.svc.store.save_draft, s.owner, s.name, s.user, s.snapshot())
                s.mark_saved(saved_at)
            except WorkspaceError as exc:
                self.status_label.text = f"Draft not saved: {exc}"
                return
        if s.saved_at:
            self.status_label.text = f"Draft saved {s.saved_at[11:19]} UTC"
        elif s.is_new:
            self.status_label.text = "Click Next to create the process; drafts then save automatically"

    async def create_process(self) -> bool:
        s = self.state
        name = (s.spec["metadata"].get("name") or "").strip()
        from ..models.rules import PROCESS_NAME_RE

        if not PROCESS_NAME_RE.match(name):
            ui.notify("Give the process a name first: lower_snake_case, 3-63 characters, e.g. sales_daily", type="warning")
            return False
        if not s.variable_values("lab").get("write_project"):
            ui.notify("Enter the lab (dev) write_project under Environments", type="warning")
            return False
        s.spec["metadata"]["owner"] = s.user
        try:
            await run.io_bound(self.svc.store.create_process, s.user, s.snapshot())
        except WorkspaceError as exc:
            msg = "you already have a process with this name" if exc.status == 409 else str(exc)
            ui.notify(f"Could not create the process: {msg}", type="negative")
            return False
        s.is_new, s.owner, s.name = False, s.user, name
        s.mark_saved(None)
        ui.navigate.history.replace(f"/p/{s.owner}/{s.name}")
        ui.notify(f"Process {name} created — your changes now save automatically", type="positive")
        self.name_input.disable()
        return True

    # ================================================================== stage 1: basics
    def stage_basics(self) -> None:
        s = self.state
        md = s.spec["metadata"]
        with section("About this process", icon="badge"):
            with ui.row().classes("w-full gap-4"):
                self.name_input = ui.input(
                    "Process name", placeholder="e.g. sales_daily",
                    validation=lambda v: None if v and re.match(r"^[a-z][a-z0-9_]{2,62}$", v) else "lower_snake_case, 3-63 characters",
                ).bind_value(md, "name").props("outlined dense").classes("w-72 sf-mono").mark("process-name")
                if not s.is_new:
                    self.name_input.disable()
                ui.input("Team").bind_value(md, "team").props("outlined dense").classes("w-56")
                list_input("Tags, comma separated", md, "tags", placeholder="sales, daily", classes="min-w-[16rem]").mark("tags")
            ui.textarea("Description — the program's purpose (goes into the script header and the README)").bind_value(md, "description").props("outlined autogrow").classes("w-full")
            hint("The process name cannot change later — clone instead.")
        with section("Script header", "The block at the top of the script, as in the team template.", icon="assignment_ind"):
            with ui.row().classes("w-full gap-4 items-center"):
                list_input("Author(s): PIDs, comma separated", md, "authors", placeholder="P10000, P12345").mark("authors")
                list_input("Reviewer(s): PIDs, comma separated", md, "reviewers", placeholder="P12345").mark("reviewers")
                ui.input("JIRA# or Service Now#").bind_value(md, "jira").props("outlined dense").classes("w-56 sf-mono").mark("jira")
                ui.label().bind_text_from(md, "created_on", backward=lambda v: f"Created on: {v}" if v else
                                          "Created on: set when the first version is saved").classes("sf-hint")
            self.updates_view()
        with section("Script file", "The script and its YAML share this name and sit side by side.", icon="description"):
            with ui.row().classes("w-full gap-4 items-center"):
                ui.input(
                    "Script file name (without .py)",
                    validation=lambda v: script_name_problem(v.strip().removesuffix(".py")) if v and v.strip() else None,
                ).bind_value(md, "script_name", forward=lambda v: (v or "").strip().removesuffix(".py") or None,
                             backward=lambda v: v or "").props("outlined dense").classes("w-72 sf-mono").mark("script-name")
                run_line = ui.label().classes("sf-preview").mark("run-command")
                for key in ("script_name", "name"):  # either name changes the command line
                    run_line.bind_text_from(md, key, backward=lambda _v: self._run_command())
            hint("Leave empty to use the process name. The orchestrator passes the environment (lab / value) and the YAML.")
        with section("Environments", "One YAML block per environment. The hub checks and test-runs lab only; it never "
                                     "reads or runs value (prod).", icon="layers"):
            self.environments_view()

    @ui.refreshable_method
    def environments_view(self) -> None:
        s = self.state
        envs = s.spec.setdefault("environments", {"qa_enabled": False})
        for env in s.active_envs():
            with ui.row().classes("w-full gap-4 items-center no-wrap"):
                ui.label(ENV_LABELS[env]).classes("w-32 font-medium")
                for name in BUILTIN_PROJECTS:
                    v = s.variable(name)
                    if v is None:
                        continue
                    v.setdefault("values", empty_values())
                    ui.input(name).bind_value(v["values"], env).props("outlined dense").classes("w-72 sf-mono").mark(f"{env}-{name}")
                if env == "value":
                    ui.icon("lock").classes("text-gray-500").tooltip("Written into the YAML only: the hub never uses prod values")
        with ui.row().classes("items-center gap-2"):
            ui.switch("Also a qa environment", value=bool(envs.get("qa_enabled")),
                      on_change=lambda _e: (self.environments_view.refresh(), self.variables_view.refresh())).bind_value(envs, "qa_enabled")
            hint("Projects, datasets and buckets per environment are on the next page.")

    @ui.refreshable_method
    def updates_view(self) -> None:
        md = self.state.spec["metadata"]
        updates = md.setdefault("updates", [])
        for i, u in enumerate(updates):
            with ui.row().classes("w-full gap-2 items-center no-wrap"):
                ui.input("Date").bind_value(u, "date").props("outlined dense type=date").classes("w-44")
                ui.input("PID").bind_value(u, "pid").props("outlined dense").classes("w-32 sf-mono")
                ui.input("What changed").bind_value(u, "note").props("outlined dense").classes("flex-grow")
                ui.button(icon="delete_outline", on_click=lambda i=i: (updates.pop(i), self.updates_view.refresh())).props("flat dense round color=negative")
        ui.button("Add an update line", icon="add", on_click=lambda: (updates.append(
            {"date": dt.date.today().isoformat(), "pid": (md.get("authors") or [""])[0], "note": ""}), self.updates_view.refresh())).props("flat dense no-caps")

    def _run_command(self) -> str:
        s = self.state
        md = s.spec["metadata"]
        stem = (md.get("script_name") or md.get("name") or "dl_script").strip()
        schema = f"{stem}_table_schema.yaml" if any(st.get("type") == "ingest" for st in s.steps) else None
        return f"Run: {run_command(stem, schema, 'lab')}   (or value: the environment comes last)"

    # ================================================================== stage 2: variables
    def stage_variables(self) -> None:
        with section("Variables per environment", "Projects, datasets, buckets and folders that differ between lab and prod. "
                     "SQL uses them as ${name}; step fields as {name}. Each value is written into its environment's YAML block.",
                     "data_object"):
            self.variables_view()
        with section("Secrets (Google Secret Manager)", "Read at the start of main() with the team's gsm_module, from each "
                     "environment's gsm_project, e.g. for source-system connections. Only the location goes into the YAML.", "key"):
            self.secrets_view()

    @ui.refreshable_method
    def variables_view(self) -> None:
        s = self.state
        envs = s.active_envs()
        variables = s.variables
        with ui.row().classes("w-full items-center gap-2 no-wrap sf-hint"):
            ui.label("Name").classes("w-52")
            ui.label("Group").classes("w-28")
            for env in envs:
                ui.label(ENV_LABELS[env]).classes("w-56")
            ui.label("Used by").classes("flex-grow")
        for i, v in enumerate(variables):
            builtin = v.get("name") in (*BUILTIN_PROJECTS, GSM_PROJECT)
            v.setdefault("values", empty_values())
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                name = ui.input(placeholder="raw_dataset").bind_value(v, "name").props("outlined dense").classes("w-52 sf-mono")
                group = ui.select(GROUPS).bind_value(v, "group").props("outlined dense").classes("w-28")
                if builtin:
                    name.props("readonly")
                    group.disable()
                for env in envs:
                    ui.input(placeholder=env).bind_value(v["values"], env).props("outlined dense").classes("w-56 sf-mono").mark(f"var-{i}-{env}")
                ui.label(self._usage(v.get("name", ""))).classes("flex-grow sf-hint")
                if builtin:
                    ui.icon("push_pin").classes("text-gray-400").tooltip("built in")
                else:
                    ui.button(icon="delete_outline", on_click=lambda i=i: (variables.pop(i), self.variables_view.refresh())).props("flat dense round color=negative")
        with ui.row().classes("gap-2"):
            ui.button("Add variable", icon="add", on_click=lambda: (variables.append(
                {"name": "", "group": "other", "values": empty_values(), "description": ""}), self.variables_view.refresh())).props("outline dense no-caps").mark("add-variable")
            ui.button("Refresh usage", icon="refresh", on_click=self.variables_view.refresh).props("flat dense no-caps")
        hint("Tip: pasting SQL on a SQL step adds its projects and datasets here with their lab values; fill in the value (prod) column.")

    def _usage(self, name: str) -> str:
        if not name:
            return ""
        users = []
        needle_sql, needle_path = "${" + name + "}", "{" + name + "}"
        for st in self.state.steps:
            blob = json.dumps(st)
            if needle_sql in blob or needle_path in blob:
                users.append(st.get("function_name", "?"))
        if any(needle_path in a for a in self.state.spec.get("email", {}).get("attachments") or []):
            users.append("email")
        if name in BUILTIN_PROJECTS:
            users.append("main()")
        if name == GSM_PROJECT and self.state.spec.get("secrets"):
            users.append("secrets")
        return ", ".join(users) if users else "not used yet"

    @ui.refreshable_method
    def secrets_view(self) -> None:
        s = self.state
        secrets = s.spec.setdefault("secrets", [])
        if secrets:
            with ui.row().classes("w-full items-center gap-2 no-wrap sf-hint"):
                ui.label("Name (SECRETS[...] in the script)").classes("w-56")
                ui.label("Secret id").classes("w-56")
                ui.label("Version").classes("w-36")
                ui.label("What it holds (Gemini reads this)").classes("flex-grow")
        for i, sec in enumerate(secrets):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.input(placeholder="pdw_credentials").bind_value(sec, "name").props("outlined dense").classes("w-56 sf-mono").mark(f"secret-name-{i}")
                ui.input(placeholder="pdw-source-connection").bind_value(sec, "secret_id").props("outlined dense").classes("w-56 sf-mono")
                ui.input(placeholder="latest").bind_value(
                    sec, "version", forward=lambda v: str(v or "latest").strip().lower(), backward=lambda v: v or "latest").props(
                    "outlined dense").classes("w-36 sf-mono").tooltip("latest = the newest version; a number pins one version")
                ui.input(placeholder="what it holds, e.g. api_credentials → api").bind_value(sec, "description").props("outlined dense").classes("flex-grow")
                result = ui.label().classes("sf-hint w-56")
                ui.button(icon="verified_user", on_click=lambda sec=sec, result=result: self.check_secret(sec, result)).props(
                    "flat dense round").tooltip("Check in lab that the secret version exists (the value is never read)")
                ui.button(icon="delete_outline", on_click=lambda i=i: (secrets.pop(i), self.secrets_view.refresh())).props("flat dense round color=negative")
        with ui.row().classes("gap-2 items-center"):
            ui.button("Add secret", icon="add", on_click=self.add_secret).props("outline dense no-caps").mark("add-secret")
            hint("Adding a secret adds the gsm_project variable (the project that holds secrets in each environment).")

    def add_secret(self) -> None:
        secrets = self.state.spec.setdefault("secrets", [])
        taken = {x.get("name") for x in secrets}
        final, n = "secret", 2
        while final in taken:
            final, n = f"secret_{n}", n + 1
        secrets.append({"name": final, "secret_id": "", "version": "latest", "description": ""})
        self.state.ensure_gsm_project()
        self.secrets_view.refresh()
        self.variables_view.refresh()

    async def check_secret(self, sec: dict, result: ui.label) -> None:
        from ..services.validators import secret_version_info

        if not sec.get("secret_id"):
            result.text = "enter the secret id first"
            return
        project = self.state.variable_values("lab").get(GSM_PROJECT) or ""
        if not project:
            result.text = "enter the lab gsm_project first"
            return
        result.text = "checking in lab…"
        info = await run.io_bound(secret_version_info, self.svc.clients, project=project, secret_id=sec["secret_id"],
                                  version=sec.get("version") or "latest")
        if info["ok"]:
            result.text = f"✓ version {info['version']} enabled (lab)"
            result.classes(replace="text-sm text-green-800 w-56")
        elif info.get("state"):
            result.text = f"⚠ version {info['version']} is {info['state']}"
            result.classes(replace="text-sm text-amber-700 w-56")
        else:
            result.text = f"✗ {info['error'][:120]}"
            result.classes(replace="text-sm text-red-700 w-56")

    # ================================================================== stage 3: pipeline
    def stage_pipeline(self) -> None:
        with ui.row().classes("w-full gap-4 no-wrap items-start"):
            with ui.column().classes("flex-grow gap-3"):
                with section("Steps, in the order main() runs them", "Each step becomes one Python function. Reorder with the arrows; "
                             "names must be valid, unique, lower_snake_case.", "account_tree"):
                    self.pipeline_view()
                    with ui.row().classes("gap-2"):
                        for kind, meta in STEP_KINDS.items():
                            with ui.button(f"+ {meta['label']}", icon=meta["icon"], on_click=lambda k=kind: self.add_step(k)).props(f"outline no-caps color={meta['color']}").mark(f"add-{kind}"):
                                ui.tooltip(meta["hint"])
            with ui.column().classes("w-[30rem] gap-3"):
                with section("Preview: def main(args)", "How the script will call your steps.", "visibility"):
                    self.main_preview = ui.code("", language="python").classes("w-full sf-code").mark("main-preview")
                with section("Order check", icon="rule"):
                    self.order_view()
        self.refresh_pipeline_side()

    @ui.refreshable_method
    def pipeline_view(self) -> None:
        s = self.state
        if not s.steps:
            hint("No steps yet. Start from a template or add steps below.")
            with ui.row().classes("gap-2"):
                for key, t in TEMPLATES.items():
                    if key == "blank":
                        continue
                    with ui.button(t["label"], icon="auto_fix_high", on_click=lambda k=key: self.apply_template(k)).props("unelevated no-caps color=secondary").mark(f"template-{key}"):
                        ui.tooltip(t["hint"])
            return
        for i, st in enumerate(s.steps):
            meta = STEP_KINDS[st["type"]]
            with ui.card().classes("sf-card w-full p-2"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.chip(str(i + 1), color=meta["color"], text_color="white").classes("sf-step-chip")
                    ui.icon(meta["icon"]).classes(f"text-{meta['color']}-700")
                    with ui.column().classes("gap-0 w-72"):
                        ui.input("Function name", validation=lambda v, i=i: self._name_problem(v, i)).bind_value(st, "function_name").props("outlined dense prefix=def").classes("w-full sf-mono")
                    ui.input("What it does").bind_value(st, "description").props("outlined dense").classes("flex-grow")
                    ui.select({"stop": "On failure: stop", "continue": "On failure: continue"}).bind_value(st, "on_failure").props("outlined dense").classes("w-48")
                    ui.switch().bind_value(st, "enabled").tooltip("Enabled")
                    ui.button(icon="arrow_upward", on_click=lambda i=i: self.move_step(i, -1)).props("flat dense round size=sm")
                    ui.button(icon="arrow_downward", on_click=lambda i=i: self.move_step(i, 1)).props("flat dense round size=sm")
                    ui.button(icon="edit", on_click=lambda i=i: self.edit_step(i)).props("flat dense round size=sm").tooltip("Edit details")
                    ui.button(icon="delete_outline", on_click=lambda i=i: self.delete_step(i)).props("flat dense round size=sm color=negative")

    def _name_problem(self, value: str, index: int) -> str | None:
        problem = function_name_problem(value or "")
        if problem:
            return problem
        if value in self.state.taken_names(index):
            return "another step already uses this name"
        return None

    @ui.refreshable_method
    def order_view(self) -> None:
        spec = self.state.validate()
        if spec is None:
            hint("Shown once the process has no errors.")
            return
        for item in order_checks(spec):
            with ui.row().classes("items-start gap-2 no-wrap"):
                level_icon(item.level)
                ui.label(item.title).classes("text-sm")
                if item.fix:
                    ui.button("Fix", on_click=lambda f=item.fix: self.apply_fix(f)).props("flat dense no-caps")

    def refresh_pipeline_side(self) -> None:
        s = self.state
        if s.is_copy:  # no pipeline page: the copy jobs refresh themselves
            return
        self.main_preview.set_content(render_main_preview(s.steps, s.variables, bool(s.spec.get("email", {}).get("enabled"))))
        self.order_view.refresh()

    def structure_changed(self) -> None:
        if self.state.is_copy:
            self.copy_jobs_view.refresh()
        else:
            self.pipeline_view.refresh()
            self.refresh_pipeline_side()
            self.details_view.refresh()
        self.attachments_view.refresh()

    def add_step(self, kind: str) -> None:
        if kind == "ingest" and any(st.get("type") == "ingest" for st in self.state.steps):
            ui.notify("One file-ingestion step per process (the script takes a single -ts table schema): "
                      "create another process for the next file", type="warning")
            return
        self.state.steps.append(new_step(kind, self.state.taken_names(), self.state.taken_keys()))
        self.structure_changed()

    def apply_template(self, key: str) -> None:
        for kind in TEMPLATES[key]["kinds"]:
            self.state.steps.append(new_step(kind, self.state.taken_names(), self.state.taken_keys()))
        self.structure_changed()
        ui.notify(f"Added {len(TEMPLATES[key]['kinds'])} steps — rename them and fill in the details", type="positive")

    def move_step(self, i: int, delta: int) -> None:
        steps, j = self.state.steps, i + delta
        if 0 <= j < len(steps):
            steps[i], steps[j] = steps[j], steps[i]
            self.state.analyses = {}
            self.structure_changed()

    async def delete_step(self, i: int) -> None:
        st = self.state.steps[i]
        with ui.dialog() as dialog, ui.card():
            ui.label(f"Delete step {i + 1} ({st.get('function_name')})?")
            with ui.row():
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat no-caps")
                ui.button("Delete", on_click=lambda: dialog.submit(True)).props("unelevated no-caps color=negative")
        if await dialog:
            del self.state.steps[i]
            self.state.analyses = {}
            self.structure_changed()

    def apply_fix(self, fix: dict) -> None:
        if fix.get("action") == "move_after":
            names = self.state.taken_names()
            if fix["step"] in names and fix["after"] in names:
                step = self.state.steps.pop(names.index(fix["step"]))
                after = self.state.taken_names().index(fix["after"])
                self.state.steps.insert(after + 1, step)
                self.state.analyses = {}
                self.structure_changed()
                ui.notify(f"Moved {fix['step']} after {fix['after']}", type="positive")

    def edit_step(self, i: int) -> None:
        if self.state.is_copy:
            self.stepper.value = "Copy jobs"
            return
        self.details_target = i
        self.details_view.refresh()
        self.stepper.value = "Step details"

    # ================================================================== copy-files process: copy jobs
    def stage_copy_jobs(self) -> None:
        with section("Copy jobs, in the order main() runs them", "Each job becomes one function that runs the Google Cloud "
                     "CLI (gcloud storage cp, mv or rsync). Paths are gs://bucket/path or local paths; {variables} differ "
                     "per environment and {date:%Y%m%d} is today's date.", "drive_file_move"):
            self.copy_jobs_view()
            ui.button("Add a copy job", icon="add", on_click=lambda: self.add_step("copy")).props(
                "outline no-caps color=brown").mark("add-copy-job")

    @ui.refreshable_method
    def copy_jobs_view(self) -> None:
        s = self.state
        if not s.steps:
            hint("No copy jobs yet: add one below.")
        for i, st in enumerate(s.steps):
            with ui.card().classes("sf-card w-full gap-2"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.chip(str(i + 1), color="brown", text_color="white").classes("sf-step-chip")
                    ui.input("Function name", validation=lambda v, i=i: self._name_problem(v, i)).bind_value(
                        st, "function_name").props("outlined dense prefix=def").classes("w-72 sf-mono").mark(f"copy-fn-{i}")
                    ui.input("What it does").bind_value(st, "description").props("outlined dense").classes("flex-grow")
                    ui.select({"stop": "On failure: stop", "continue": "On failure: continue"}).bind_value(
                        st, "on_failure").props("outlined dense").classes("w-48")
                    ui.switch().bind_value(st, "enabled").tooltip("Enabled")
                    ui.button(icon="arrow_upward", on_click=lambda i=i: self.move_step(i, -1)).props("flat dense round size=sm")
                    ui.button(icon="arrow_downward", on_click=lambda i=i: self.move_step(i, 1)).props("flat dense round size=sm")
                    ui.button(icon="delete_outline", on_click=lambda i=i: self.delete_step(i)).props(
                        "flat dense round size=sm color=negative")
                copy_form(st, i, s, self.svc, self.refresh_pipeline_side)

    # ================================================================== stage 4: step details
    def stage_details(self) -> None:
        self.details_target = 0
        self.details_view()

    @ui.refreshable_method
    def details_view(self) -> None:
        s = self.state
        if not s.steps:
            hint("Add steps on the Pipeline page first.")
            return
        target = min(getattr(self, "details_target", 0), len(s.steps) - 1)
        with ui.tabs().props("dense align=left no-caps inline-label").classes("w-full") as tabs:
            tab_objs = []
            for i, st in enumerate(s.steps):
                meta = STEP_KINDS[st["type"]]
                tab_objs.append(ui.tab(f"t{i}", label=f"{i + 1}. {st.get('function_name') or meta['label']}", icon=meta["icon"]))
        with ui.tab_panels(tabs, value=tab_objs[target]).classes("w-full"):
            for i, st in enumerate(s.steps):
                with ui.tab_panel(tab_objs[i]).classes("gap-3"):
                    from .step_forms import step_header

                    step_header(st, i, s)
                    FORMS[st["type"]](st, i, s, self.svc, self.refresh_pipeline_side)

    # ================================================================== stage 5: email
    def stage_email(self) -> None:
        s = self.state
        em = s.spec["email"]
        settings = self.svc.settings

        @ui.refreshable
        def body() -> None:
            if not em.get("enabled"):
                hint("Off: the generated script contains no email code.")
                return
            em.setdefault("receivers", empty_values())
            with ui.row().classes("items-center gap-2"):
                ui.label("Sender").classes("font-semibold")
                sender = self.svc.adc_email()
                ui.chip(sender or "unknown: the hub's ADC identity has no email", icon="account_circle",
                        color="primary" if sender else "warning").props("outline").mark("email-sender")
                help_icon("The email is sent from the Google identity the hub runs with (ADC), written into the YAML as "
                          "email.sender, through the internal mail relay (a hub setting; no login behind the VPN).")
            ui.label("To (comma-separated addresses)").classes("font-semibold")
            for env in s.active_envs():
                with ui.column().classes("w-full gap-0"):
                    inp = ui.input(f"{ENV_LABELS[env]}", placeholder="first.last@example.com, team-list@example.com").bind_value(
                        em["receivers"], env).props("outlined dense").classes("w-full sf-mono").mark(f"email-to-{env}")
                    check = ui.label().classes("sf-hint")

                    def show(_e=None, env=env, check=check) -> None:
                        addresses, problems = parse_receivers(em["receivers"].get(env) or "")
                        if problems:
                            check.text = "⚠ " + "; ".join(problems)
                            check.classes(replace="sf-problem")
                        else:
                            check.text = f"{len(addresses)} recipient(s)" if addresses else "add at least one address"
                            check.classes(replace="sf-hint")

                    inp.on_value_change(show)
                    show()
            ui.label("When").classes("font-semibold")
            with ui.row().classes("gap-6"):
                ui.checkbox("A step failed").bind_value(em["send_on"], "failure")
                ui.checkbox("The run succeeded").bind_value(em["send_on"], "success")
            with ui.row().classes("items-center gap-2"):
                ui.switch("List the files loaded and exported (file name, rows, table or bucket path)").bind_value(
                    em, "file_summary").mark("email-file-summary")
                if not any(st.get("type") in ("ingest", "export") for st in s.steps):
                    hint("(for ingestion and export steps; this process has none yet)")
            ui.input("Subject").bind_value(em, "subject").props("outlined dense").classes("w-full sf-mono").mark("email-subject")
            ui.textarea("Message").bind_value(em, "message").props("outlined autogrow").classes("w-full").mark("email-message")
            hint("Placeholders: {status} {script} {process} {environment} {date} (or {date:%Y-%m-%d}). The email reads "
                 "“Hello Team,”, your message, then the signature:")
            ui.label(settings.email_signature).classes("sf-hint whitespace-pre-line pl-4")
            ui.label("Attachments").classes("font-semibold")
            self.attachments_view()
            ui.select(list(em.get("attachments") or []), label="Other files (gs://bucket/path/file or a local path, press Enter after each)",
                      multiple=True, new_value_mode="add-unique").bind_value(em, "attachments").props(
                "outlined dense use-chips").classes("w-full").mark("email-attachments")
            hint("Paths may use {variables} and {date:%Y%m%d}; a * in a gs:// file name attaches every matching file.")
            problems()

        @ui.refreshable
        def problems() -> None:
            errors = [e["message"] for e in s.errors if e["path"].startswith("email") or e["message"].startswith("email")]
            for message in errors[:6]:
                with ui.row().classes("items-center gap-1 no-wrap"):
                    level_icon("error")
                    ui.label(message).classes("text-sm text-red-800")

        self.email_body = body
        with section("Email notification", "Sent at the end of a run (and when an ingestion finds no file, if you choose so), "
                     "the team way: smtplib through the internal relay.", "mail"):
            ui.switch("Send a notification email", value=bool(em.get("enabled")),
                      on_change=lambda _e: (body.refresh(), self.refresh_pipeline_side())).bind_value(em, "enabled").mark("email-enabled")
            body()
            ui.timer(2.0, problems.refresh)

    @ui.refreshable_method
    def attachments_view(self) -> None:
        exports = [st for st in self.state.steps if st.get("type") == "export"]
        if not exports:
            hint("Export steps can attach the file they write to the success email.")
            return
        for st in exports:
            ui.checkbox(f"The file written by {st.get('function_name')}").bind_value(st, "attach_to_email")

    # ================================================================== stage 6: review & test (1 validate → 2 lab test → 3 generate)
    def stage_review(self) -> None:
        s = self.state
        self.review_status()
        with section("1 · Validate all (lab)", "Dry-runs every query on BigQuery, checks the GCS paths and sample files with the "
                     "lab values, and renders the code. Nothing is written; prod (value) is never touched.", "fact_check"):
            with ui.row().classes("gap-2 items-center"):
                ui.button("Validate all", icon="checklist", on_click=self.validate_all).props("unelevated no-caps").mark("validate")
                self.gcp_switch = ui.switch("Include GCP checks", value=self.svc.settings.gcp_checks_enabled)
            self.report_box = ui.column().classes("w-full gap-2")
        with section("2 · Test run in lab", "Runs the script for real in lab (dev) with the hub's credentials: "
                     "python ./<script>.py -cf ./<script>.yaml lab. It writes lab tables and files; email goes only to you. "
                     "When it ends, download its log (the ZIP of a tested bundle also holds it).", "science"):
            with ui.row().classes("gap-2 items-center"):
                self.test_btn = ui.button("Test run in lab", icon="play_arrow", on_click=self.test_run).props(
                    "unelevated no-caps color=deep-purple").mark("test-run")
                self.cancel_btn = ui.button("Cancel", icon="stop", on_click=self.cancel_test_run).props("flat no-caps color=negative")
                self.cancel_btn.visible = False
                self.skip_btn = ui.button("Skip the lab test…", icon="redo", on_click=self.skip_test_dialog).props(
                    "flat no-caps color=warning").mark("skip-test")
                self.skip_btn.visible = False
                self.test_gate = ui.label().classes("sf-hint").mark("test-gate")
            self.test_box = ui.column().classes("w-full gap-2")
            self.restore_test_run()
        with section("3 · Generate", "Builds the script, its YAML, the README and requirements.txt (plus the table schema of a "
                     "file ingestion), then smoke-runs the script offline against fake Google clients.", "rocket_launch"):
            with ui.row().classes("gap-2 items-center"):
                self.generate_btn = ui.button("Generate", icon="build", on_click=self.generate).props("unelevated no-caps").mark("generate")
                self.download_btn = ui.button("Download ZIP", icon="download", on_click=self.download).props("outline no-caps").mark("download")
                ui.button("Download spec.json", icon="data_object", on_click=self.download_spec).props("flat no-caps").mark(
                    "download-spec").tooltip("The process definition: import it on the home page to recreate the process")
                self.generate_gate = ui.label().classes("sf-hint").mark("generate-gate")
            self.generate_box = ui.column().classes("w-full gap-2")
        with section("4 · Save version", icon="save"):
            with ui.row().classes("gap-2 items-center"):
                if s.can_edit:
                    self.note_input = ui.input("Version note (optional)").props("outlined dense").classes("w-72")
                    ui.button("Save version", icon="save", on_click=self.save_version).props("unelevated no-caps color=positive").mark("save-version")
                    hint("Saves the generated files with the spec, the validation report and the lab test run.")
                else:
                    ui.button("Clone to my workspace", icon="content_copy", on_click=self.clone_dialog).props("unelevated no-caps color=warning").mark("clone")
        with section("Saved versions", icon="history"):
            self.versions_view()
        self.refresh_gates()

    # ------------------------------------------------------------------ gates
    def gates(self) -> SimpleNamespace:
        """Where the review stands for the process as it is now. Validation and the lab test are tied to the spec hash,
        so any change that alters the generated code sends the user back to step 1."""
        s = self.state
        spec = s.validate()
        g = SimpleNamespace(spec=spec, hash=None, validated=False, tested=None, untestable=[], skipped=None, generated=False,
                            custom=False)
        if spec is None:
            return g
        g.hash = spec.spec_hash()
        g.validated = s.validated_hash == g.hash
        if s.owner and s.name:
            g.tested = self.svc.test_runs.passed_for(s.owner, s.name, g.hash)
        g.untestable = self.svc.test_runs.unavailable_reasons(spec, adc_email=self._adc)
        g.custom = any(st.type == "custom" for st in spec.enabled_steps())
        if g.custom and s.test_skip and s.test_skip.get("hash") == g.hash:
            g.skipped = s.test_skip
        g.generated = bool(s.generated and s.generated["digest"] == s.digest())
        return g

    @staticmethod
    def can_generate(g: SimpleNamespace) -> bool:
        return g.validated and bool(g.tested or g.untestable or g.skipped)

    @staticmethod
    def untested_reason(g: SimpleNamespace) -> str:
        """Why the bundle is not tested in lab ('' = it is): the skip reason or why a test is not possible here."""
        if g.tested:
            return ""
        if g.skipped:
            return f"skipped by {g.skipped['by']} on {g.skipped['at'][:10]}: {g.skipped['reason']}"
        return "; ".join(g.untestable)

    def gate_problem(self, g: SimpleNamespace, step: str) -> str | None:
        """Why step 'test' or 'generate' cannot run yet (None = it can)."""
        if g.spec is None:
            return f"fix the {len(self.state.errors)} problem(s) first"
        if not g.validated:
            return "run 1 · Validate all first (it must finish without errors)"
        if step == "generate" and not (g.tested or g.untestable or g.skipped):
            return "run 2 · Test run in lab first: Generate unlocks when the lab test of this exact process passes"
        return None

    def refresh_gates(self) -> None:
        if not hasattr(self, "generate_btn"):
            return
        g = self.gates()
        test_problem = self.gate_problem(g, "test")
        self.test_gate.text = f"Locked: {test_problem}" if test_problem else (
            "Lab test not possible here: " + "; ".join(g.untestable) if g.untestable else "")
        self.test_btn.set_enabled(test_problem is None and not g.untestable)
        self.skip_btn.visible = g.custom and not g.tested and not g.untestable and not g.skipped and test_problem is None
        gen_problem = self.gate_problem(g, "generate")
        self.generate_btn.set_enabled(gen_problem is None)
        self.download_btn.set_enabled(g.generated)
        if gen_problem:
            self.generate_gate.text = f"Locked: {gen_problem}"
        elif g.tested:
            self.generate_gate.text = f"Lab test passed ({g.tested.started_at[:16].replace('T', ' ')} UTC)"
        elif g.skipped:
            self.generate_gate.text = "Lab test skipped: the bundle is not tested in lab (the README says why)"
        else:
            self.generate_gate.text = "Allowed without a lab test, which is not possible here: the bundle is not tested in lab"
        state = (g.validated, bool(g.tested), bool(g.untestable), bool(g.skipped), g.generated)
        if state != getattr(self, "_gate_state", None):
            self._gate_state = state
            self.review_status.refresh(g)

    @ui.refreshable_method
    def review_status(self, g: SimpleNamespace | None = None) -> None:
        g = g or self.gates()
        with ui.row().classes("items-center gap-2").mark("review-status"):
            def chip(text: str, done: bool, color: str = "positive") -> None:
                ui.chip(text, icon="check_circle" if done else "radio_button_unchecked",
                        color=color if done else "grey").props("outline")

            chip("1 Validated", g.validated)
            ui.icon("arrow_forward").classes("text-gray-400")
            if g.tested:
                chip("2 Lab test passed", True)
            elif g.skipped:
                chip("2 Lab test skipped", True, "warning")
            elif g.untestable:
                chip("2 Lab test not possible", True, "warning")
            else:
                chip("2 Lab test", False)
            ui.icon("arrow_forward").classes("text-gray-400")
            chip("3 Generated", g.generated)

    # ------------------------------------------------------------------ 1 validate
    async def validate_all(self) -> None:
        spec = self.state.validate()
        self.report_box.clear()
        if spec is None:
            self.state.validated_hash = None
            with self.report_box:
                self._show_errors()
            return
        with self.report_box:
            spinner = ui.spinner(size="lg")
        validator = await run.io_bound(self.svc.validator)
        report = await run.io_bound(validator.validate, spec, gcp_checks=self.gcp_switch.value)
        self.state.validation = json.loads(report.model_dump_json())
        self.state.validated_hash = spec.spec_hash() if report.ok else None
        spinner.delete()
        with self.report_box:
            render_report(self.state.validation, on_fix=self.apply_fix, on_goto=self.goto_step)
            if not report.ok:
                hint("Fix the errors and validate again: the lab test run unlocks after a validation without errors.")
        self.refresh_gates()

    def goto_step(self, fn: str | None) -> None:
        names = self.state.taken_names()
        if fn in names:
            self.edit_step(names.index(fn))

    def _show_errors(self) -> None:
        ui.label("Fix these first:").classes("font-semibold text-red-800")
        for e in self.state.errors:
            with ui.row().classes("items-center gap-1 no-wrap"):
                level_icon("error")
                ui.label(f"{e['path'] or 'process'}: {e['message']}").classes("text-sm")

    # ------------------------------------------------------------------ 3 generate
    async def _render(self) -> Any:
        g = self.gates()
        problem = self.gate_problem(g, "generate")
        self.generate_box.clear()
        if problem:
            with self.generate_box:
                if g.spec is None:
                    self._show_errors()
                else:
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        level_icon("warn")
                        ui.label(f"Not generated: {problem}.").classes("text-sm")
            return None
        spec = g.spec
        with self.generate_box:  # replaced by the result (or the problems) below
            ui.spinner(size="lg")
            ui.label("Rendering and smoke-running the script…").classes("sf-hint")
        overview = await run.io_bound(ai_overview, spec, self.svc.gemini)
        adc_email = await run.io_bound(self.svc.adc_email)
        try:
            result = await run.io_bound(lambda: render_process(
                spec, generated_by=self.state.user, ai_overview=overview, adc_email=adc_email,
                smoke_run=self.svc.settings.smoke_run_enabled, settings=self.svc.settings,
                untested_reason=self.untested_reason(g)))
        except RenderError as exc:
            self.generate_box.clear()
            with self.generate_box:
                ui.label("Generation failed:").classes("font-semibold text-red-800")
                for p in exc.problems:
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        level_icon("error")
                        ui.label(p).classes("text-sm whitespace-pre-wrap")
            return None
        if not g.tested:
            result.warnings.insert(0, "Not tested in lab: " + self.untested_reason(g))
        files = [{"path": p, "content": c, "language": result.language_of(p)} for p, c in sorted(result.files.items())]
        self.state.generated = {"digest": self.state.digest(), "result": result, "files": files, "spec": spec}
        self.generate_box.clear()
        with self.generate_box:
            with ui.row().classes("gap-3 items-center"):
                for c in result.checks:
                    with ui.row().classes("items-center gap-1"):
                        level_icon("ok" if c.ok else "error")
                        ui.label(c.name).classes("text-sm").tooltip(c.detail or "")
            for w in result.warnings:
                with ui.row().classes("items-center gap-1"):
                    level_icon("warn")
                    ui.label(w).classes("text-sm")
            self.run_commands(spec, result)
            render_files(files)
        self.refresh_gates()
        return self.state.generated

    def run_commands(self, spec: ProcessSpec, result: Any) -> None:
        """How to run the generated script: the environment comes last and picks the YAML block."""
        with ui.card().classes("sf-card w-full bg-slate-50 gap-1").mark("run-commands"):
            ui.label("Run it").classes("font-semibold")
            for env in spec.active_envs():
                command = run_command(result.script, result.schema_file, env)
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(command).classes("sf-mono text-sm")
                    ui.label(ENV_LABELS[env]).classes("sf-hint")
                    ui.button(icon="content_copy", on_click=lambda c=command: (ui.clipboard.write(c), ui.notify("Copied"))).props(
                        "flat dense round size=sm").tooltip("Copy")

    async def generate(self) -> None:
        await self._render()

    async def _current_generation(self) -> dict | None:
        g = self.state.generated
        if g and g["digest"] == self.state.digest():
            return g
        return await self._render()  # enforces the gates: steps 1 and 2 first

    def _tested_run(self, spec: ProcessSpec) -> Any:
        """The finished lab test run of exactly this spec (any status), if there is one."""
        s = self.state
        test = self.svc.test_runs.latest_for(s.owner, s.name) if s.owner and s.name else None
        return test if test is not None and not test.active and test.spec_hash == spec.spec_hash() else None

    async def download(self) -> None:
        g = await self._current_generation()
        if g:
            files = dict(g["result"].files)
            test = self._tested_run(g["spec"])
            if test is not None:  # the lab test run's log sits next to the script
                files[test.log_name()] = test.log_file()
            ui.download.content(build_zip(g["result"].script, files), f"{g['result'].script}.zip", "application/zip")
        else:
            ui.notify("Do steps 1–3 first: validate, test in lab, generate", type="warning")

    def download_spec(self) -> None:
        s = self.state
        name = s.spec["metadata"].get("name") or "process"
        ui.download.content(json.dumps(s.snapshot(), indent=2) + "\n", f"{name}_spec.json", "application/json")

    # ------------------------------------------------------------------ 2 lab test run
    async def test_run(self) -> None:
        s = self.state
        if s.is_new and not await self.create_process():
            return
        g = self.gates()
        problem = self.gate_problem(g, "test")
        if problem:
            self.test_box.clear()
            with self.test_box:
                if g.spec is None:
                    self._show_errors()
                else:
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        level_icon("warn")
                        ui.label(f"Not started: {problem}.").classes("text-sm")
            return
        spec = g.spec
        adc = await run.io_bound(self.svc.adc_email)
        problems = self.svc.test_runs.check(spec, owner=s.owner, name=s.name, adc_email=adc)
        effects = side_effects(spec, adc)
        with ui.dialog() as dialog, ui.card().classes("w-[760px] max-w-full"):
            ui.label("Run this process in lab now?").classes("text-lg font-semibold")
            hint(f"Runs as {adc or 'the hub ADC identity'} with the lab values only. It really changes lab:")
            for e in effects or ["nothing is written (the steps only read)"]:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon("edit_note").classes("text-amber-700")
                    ui.label(e).classes("text-sm sf-mono")
            for p in problems:
                with ui.row().classes("items-start gap-2 no-wrap"):
                    level_icon("error")
                    ui.label(p).classes("text-sm text-red-800")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat no-caps")
                ok = ui.button("Run in lab", icon="play_arrow", on_click=lambda: dialog.submit(True)).props(
                    "unelevated no-caps color=deep-purple").mark("test-run-confirm")
                if problems:
                    ok.disable()
        if not await dialog:
            return
        try:
            test = await run.io_bound(lambda: self.svc.test_runs.start(spec, owner=s.owner, name=s.name, user=s.user, adc_email=adc))
        except TestRunRefused as exc:
            ui.notify("The test run cannot start: " + "; ".join(exc.problems), type="negative", multi_line=True)
            return
        s.test_run_id = test.id
        self._follow(test.id)

    def restore_test_run(self) -> None:
        s = self.state
        if s.owner and s.name and s.test_run_id is None:
            latest = self.svc.test_runs.latest_for(s.owner, s.name)
            if latest is not None:
                s.test_run_id = latest.id
        if s.test_run_id:
            self._follow(s.test_run_id)

    def _follow(self, run_id: str) -> None:
        self._log_next = 0
        self.test_box.clear()
        with self.test_box:
            with ui.row().classes("items-center gap-3"):
                self.test_status = ui.chip("starting", icon="science").props("outline").mark("test-run-status")
                self.test_message = ui.label().classes("text-sm")
                self.log_btn = ui.button("Download log", icon="download", on_click=lambda: self.download_test_log(run_id)).props(
                    "flat dense no-caps").mark("test-run-log-download").tooltip("The whole log of this lab test run, as a file")
                self.log_btn.visible = False
            self.test_command = ui.label().classes("sf-hint sf-mono")
            self.test_timings = ui.column().classes("gap-0")
            self.test_log = ui.log(max_lines=self.svc.settings.test_run_max_log_lines).classes("w-full h-80 sf-mono text-xs").mark("test-run-log")
        if self._poll is not None:
            self._poll.deactivate()
        self._poll = ui.timer(1.0, lambda: self._poll_test(run_id))

    def _poll_test(self, run_id: str) -> None:
        test = self.svc.test_runs.get(run_id)
        if test is None:
            self._poll.deactivate()
            return
        snap = test.snapshot(self._log_next)
        for line in snap["lines"]:
            self.test_log.push(line)
        self._log_next = snap["next"]
        status = snap["status"]
        self.test_status.text = f"lab test: {status}"
        self.test_status.props(f"color={STATUS_COLORS.get(status, 'grey')}")
        self.test_message.text = snap["message"] or f"running for {snap['elapsed']} s"
        self.test_command.text = f"{snap['command']}  ·  as {snap['adc_email'] or 'ADC'}  ·  spec {snap['spec_hash']}"
        self.test_timings.clear()
        with self.test_timings:
            for fn in snap["steps"]:
                seconds = snap["timings"].get(fn)
                with ui.row().classes("items-center gap-2"):
                    level_icon("ok" if seconds is not None else ("info" if test.active else "error"))
                    ui.label(f"{fn}: {seconds:.2f} s" if seconds is not None else f"{fn}: —").classes("text-sm sf-mono")
        self.cancel_btn.visible = test.active
        self.log_btn.visible = not test.active
        if not test.active:
            self._poll.deactivate()

    def download_test_log(self, run_id: str) -> None:
        test = self.svc.test_runs.get(run_id)
        if test is None:
            ui.notify("This test run is no longer on the hub (runs are kept until it restarts)", type="warning")
            return
        ui.download.content(test.log_file().encode("utf-8"), test.log_name(), "text/plain")

    def cancel_test_run(self) -> None:
        if self.state.test_run_id:
            self.svc.test_runs.cancel(self.state.test_run_id)
            ui.notify("Stopping the lab test run…", type="info")

    def skip_test_dialog(self) -> None:
        """Custom steps only: generate without a lab test, with a reason that goes into the README."""
        g = self.gates()
        if not g.custom or self.gate_problem(g, "test"):
            return
        with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-full"):
            ui.label("Skip the lab test?").classes("text-lg font-semibold")
            hint("For custom code the hub cannot run, e.g. a database it cannot reach. The bundle is then marked "
                 "\"not tested in lab\" and the README says why: test it on the pipeline server before deploying.")
            reason = ui.textarea("Why can it not be tested from the hub?").props("outlined autogrow").classes(
                "w-full").mark("skip-reason")

            def confirm() -> None:
                text = " ".join((reason.value or "").split())
                if len(text) < 10:
                    ui.notify("Write the reason (at least 10 characters)", type="warning")
                    return
                self.state.test_skip = {"hash": g.hash, "reason": text, "by": self.state.user,
                                        "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
                dialog.close()
                self.refresh_gates()
                ui.notify("Lab test skipped: Generate is unlocked (the bundle is marked not tested in lab)", type="warning")

            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                ui.button("Skip the lab test", icon="redo", on_click=confirm).props("unelevated no-caps color=warning").mark(
                    "skip-confirm")
        dialog.open()

    # ------------------------------------------------------------------ save
    async def save_version(self) -> None:
        s = self.state
        if s.is_new and not await self.create_process():
            return
        if not s.spec["metadata"].get("created_on"):  # header "Created on": the first saved version
            s.spec["metadata"]["created_on"] = dt.date.today().isoformat()
        g = await self._current_generation()
        if not g:
            ui.notify("Do steps 1–3 first: validate, test in lab, generate", type="warning")
            return
        spec: ProcessSpec = g["spec"]
        validation = s.validation
        tested = self._tested_run(spec)
        skip = s.test_skip if s.test_skip and s.test_skip.get("hash") == spec.spec_hash() else None
        skip_record = {"status": "skipped", "message": skip["reason"], "started_by": skip["by"], "started_at": skip["at"],
                       "spec_hash": skip["hash"], "environment": "lab"} if skip and tested is None else None
        try:
            info = await run.io_bound(
                lambda: self.svc.store.save_version(
                    s.owner, s.name, s.user, spec=json.loads(spec.model_dump_json()), spec_hash=spec.spec_hash(),
                    files=g["result"].files, validation=validation, note=self.note_input.value or "",
                    checks_ok=bool(validation.get("ok")) if validation else True,
                    test_run=tested.record() if tested else skip_record, test_run_log=tested.log_file() if tested else None,
                )
            )
        except WorkspaceError as exc:
            ui.notify(f"Not saved: {exc}", type="negative")
            return
        await run.io_bound(self.svc.store.discard_draft, s.owner, s.name, s.user)
        s.versions.append(info.model_dump())
        s.mark_saved(info.saved_at)
        self.versions_view.refresh()
        ui.notify(f"Saved version {info.version}" + (f" with its lab test run ({tested.status})" if tested else
                                                     " (lab test skipped)" if skip_record else ""), type="positive")

    @ui.refreshable_method
    def versions_view(self) -> None:
        s = self.state
        if not s.versions:
            hint("No saved versions yet." if s.can_edit else "The owner has not saved a version yet.")
            return
        for v in reversed(s.versions):
            with ui.row().classes("items-center gap-3 w-full"):
                ui.chip(f"v{v['version']}", icon="bookmark", color="primary").props("outline dense")
                ui.label(f"{v['saved_at'][:16].replace('T', ' ')} UTC by {v['saved_by']}").classes("text-sm")
                ui.label(v.get("note") or "").classes("sf-hint flex-grow")
                if v.get("lab_test"):
                    ui.chip(f"lab test {v['lab_test']}", icon="science",
                            color=STATUS_COLORS.get(v["lab_test"], "grey")).props("outline dense")
                level_icon("ok" if v.get("checks_ok") else "warn")
                ui.button("ZIP", icon="download", on_click=lambda v=v: self.download_version(v["version"])).props("flat dense no-caps")
                ui.button("spec", icon="data_object", on_click=lambda v=v: self.download_version_spec(v["version"])).props(
                    "flat dense no-caps").tooltip("Download this version's spec.json")
                ui.button("Open in editor", icon="restore", on_click=lambda v=v: self.load_version(v["version"])).props("flat dense no-caps")

    async def download_version(self, version: int) -> None:
        files = await run.io_bound(self.svc.store.get_version_files, self.state.owner, self.state.name, version)
        stem = bundle_stem(files, self.state.name)
        log = await run.io_bound(self.svc.store.get_version_log, self.state.owner, self.state.name, version)
        if log:  # the lab test run saved with this version
            files[f"{stem}_lab_test_run.log"] = log
        ui.download.content(build_zip(stem, files), f"{stem}_v{version}.zip", "application/zip")

    async def download_version_spec(self, version: int) -> None:
        spec = (await run.io_bound(self.svc.store.get_version, self.state.owner, self.state.name, version))["spec"]
        ui.download.content(json.dumps(spec, indent=2) + "\n", f"{self.state.name}_v{version}_spec.json", "application/json")

    async def load_version(self, version: int) -> None:
        ui.navigate.to(f"/p/{self.state.owner}/{self.state.name}?version={version}")

    async def clone_dialog(self) -> None:
        s = self.state
        with ui.dialog() as dialog, ui.card():
            ui.label("Clone into your workspace").classes("text-lg font-semibold")
            name = ui.input("New process name", value=f"{s.name}_copy").props("outlined dense").classes("w-72 sf-mono")
            with ui.row():
                ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat no-caps")
                ui.button("Clone", on_click=lambda: dialog.submit(name.value)).props("unelevated no-caps")
        new_name = await dialog
        if not new_name:
            return
        try:
            meta = await run.io_bound(self.svc.store.clone, s.owner, s.name, None, s.user, new_name)
        except WorkspaceError as exc:
            ui.notify(f"Clone failed: {exc}", type="negative")
            return
        ui.navigate.to(f"/p/{meta.owner}/{meta.name}")


def unique_name(base: str, taken: list[str]) -> str:
    return suggest_function_name(base, taken)
