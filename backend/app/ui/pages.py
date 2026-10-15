"""NiceGUI pages: the process list (dashboard) and the wizard for new / existing processes."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import Request
from nicegui import run, ui

from .. import APP_NAME
from ..auth import resolve_user
from ..models.migrate import upgrade_spec
from ..models.spec import PROCESS_TYPES, SPEC_VERSION, ensure_builtin_variables
from ..services.workspace import WorkspaceError
from .copy_page import CopyPage
from .enhance import EnhancePage
from .search_page import SearchPage
from .state import WizardState, blank_spec, empty_values, fill_default_projects
from .theme import frame, hint
from .wizard import Wizard

_pending_imports: dict[str, dict[str, Any]] = {}  # token -> {"spec", "notes", "title"} for /new?import=<token>

ACTIONS = [  # the home page's ways to start
    ("New pipeline", "account_tree", "/new", "new-pipeline",
     "SQL, file ingestion (CSV, delimited or fixed-width TXT, JSON, Parquet, Excel), exports, copies and data-quality checks "
     "in one script."),
    ("Copy files", "drive_file_move", "/copy", "new-copy",
     "Copy or move files between buckets right now, or make a script that does it every day."),
    ("Enhance a script", "auto_fix_high", "/enhance", "enhance",
     "Bring an existing script (and its YAML) up to the team standard: imported as a process, or rewritten in place."),
    ("Find files", "manage_search", "/search", "find-files",
     "Search one or more buckets for a file name or pattern (*.csv), in every folder."),
]


def stash_import(spec: dict[str, Any], notes: list[str] | None = None, title: str | None = None) -> str:
    """Keep a spec for the next /new?import=<token> page (imported files, enhanced scripts)."""
    token = secrets.token_urlsafe(8)
    _pending_imports[token] = {"spec": spec, "notes": list(notes or []), "title": title}
    return token


def register_pages(svc: Any) -> None:
    """Register every page. Called once at start-up (and per test with fake services)."""

    @ui.page("/", title=APP_NAME)
    async def dashboard(request: Request) -> None:
        user = resolve_user(request, svc.settings)
        query = {"text": request.query_params.get("q", "")}  # the header search (from another page: ?q=)

        async def on_search(text: str) -> None:
            query["text"] = text
            await load()

        with frame(user.email, svc.adc, on_search=on_search, search_value=query["text"]):
            with ui.grid(columns=len(ACTIONS)).classes("w-full gap-3"):
                for title, icon, target, mark, text in ACTIONS:
                    with ui.card().classes("sf-card w-full cursor-pointer").on("click", lambda t=target: ui.navigate.to(t)).mark(mark):
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.icon(icon, size="md").classes("text-primary")
                            ui.label(title).classes("text-lg font-semibold")
                        ui.label(text).classes("text-sm sf-muted")
            with ui.row().classes("w-full items-end gap-4"):
                with ui.column().classes("gap-0"):
                    ui.label("Processes").classes("text-2xl font-semibold")
                    hint("Everyone in the group can open and clone any process; only owners and co-owners can save versions.")
                ui.space()
                # a search started on another page looks at everyone's processes
                scope = ui.toggle({"mine": "Mine", "all": "Everyone's"}, value="all" if query["text"] else "mine").props(
                    "no-caps dense")
                ui.upload(on_upload=lambda e: import_spec(e), auto_upload=True, label="Import spec.json / ZIP").props(
                    "accept=.json,.zip flat dense").classes("w-56")
            listing = ui.column().classes("w-full gap-2")

            async def load() -> None:
                items = await run.io_bound(svc.store.list_processes, user=user.email, scope=scope.value, query=query["text"])
                listing.clear()
                with listing:
                    if not items:
                        with ui.card().classes("sf-card w-full items-center p-8"):
                            ui.icon("inventory_2", size="xl").classes("text-gray-400")
                            searching = bool(query["text"])
                            ui.label("Nothing matches" if searching or scope.value == "all" else "No processes yet").classes("text-lg")
                            hint(f"No process matches “{query['text']}”." if searching else "Start one with the cards above.")
                        return
                    with ui.grid(columns=3).classes("w-full gap-3"):
                        for p in items:
                            process_card(p, user.email)

            def process_card(p: Any, me: str) -> None:
                with ui.card().classes("sf-card w-full"):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(p.name).classes("text-base font-semibold sf-mono")
                        ui.space()
                        if p.process_type != "pipeline":
                            ui.chip(PROCESS_TYPES.get(p.process_type, p.process_type), icon="drive_file_move").props("dense outline")
                        ui.chip(f"v{p.latest_version}" if p.latest_version else "draft", color="primary" if p.latest_version else "grey").props("outline dense")
                    ui.label(p.description or "No description").classes("text-sm sf-muted line-clamp-2")
                    with ui.row().classes("gap-1"):
                        ui.chip(p.owner if p.owner != me else "you", icon="person").props("dense outline")
                        if p.team:
                            ui.chip(p.team, icon="groups").props("dense outline")
                        for t in p.tags[:3]:
                            ui.chip(t).props("dense outline color=secondary")
                    hint(f"{p.step_count} step(s) · updated {p.updated_at[:16].replace('T', ' ')} UTC by {p.updated_by or p.owner}")
                    with ui.row().classes("gap-1 w-full items-center"):
                        ui.button("Open", icon="open_in_new", on_click=lambda p=p: ui.navigate.to(f"/p/{p.owner}/{p.name}")).props("flat dense no-caps")
                        ui.button("Clone", icon="content_copy", on_click=lambda p=p: clone(p)).props("flat dense no-caps")
                        if p.owner == me:
                            ui.space()
                            ui.button(icon="delete_outline", on_click=lambda p=p: delete(p)).props(
                                "flat dense round size=sm color=negative").tooltip("Delete this process").mark("delete-process")

            async def delete(p: Any) -> None:
                versions = f"its {p.latest_version} saved version(s)" if p.latest_version else "its draft"
                with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
                    ui.label(f"Delete {p.name}?").classes("text-lg font-semibold")
                    ui.label(f"This removes the process and {versions} for everyone. It cannot be undone; "
                             "scripts already deployed are not touched.").classes("text-sm")
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat no-caps")
                        ui.button("Delete", icon="delete", on_click=lambda: dialog.submit(True)).props(
                            "unelevated no-caps color=negative").mark("delete-process-confirm")
                if not await dialog:
                    return
                try:
                    await run.io_bound(svc.store.delete_process, p.owner, p.name, user.email)
                except WorkspaceError as exc:
                    ui.notify(f"Not deleted: {exc}", type="negative")
                    return
                ui.notify(f"Deleted {p.name}", type="positive")
                await load()

            async def clone(p: Any) -> None:
                with ui.dialog() as dialog, ui.card():
                    ui.label(f"Clone {p.name}").classes("text-lg font-semibold")
                    name = ui.input("New name", value=f"{p.name}_copy").props("outlined dense").classes("w-72 sf-mono")
                    with ui.row():
                        ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat no-caps")
                        ui.button("Clone", on_click=lambda: dialog.submit(name.value)).props("unelevated no-caps")
                new_name = await dialog
                if not new_name:
                    return
                try:
                    meta = await run.io_bound(svc.store.clone, p.owner, p.name, None, user.email, new_name)
                except WorkspaceError as exc:
                    ui.notify(f"Clone failed: {exc}", type="negative")
                    return
                ui.navigate.to(f"/p/{meta.owner}/{meta.name}")

            async def import_spec(e: Any) -> None:
                raw = await e.file.read()
                try:
                    if raw[:2] == b"PK":
                        import io
                        import zipfile

                        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                            name = next((n for n in zf.namelist() if n.endswith("spec.json")), None)
                            if name is None:
                                ui.notify("This ZIP has no spec.json (bundles leave it out since 3.1). Download the spec from "
                                          "the process, or bring the script in with “Enhance a script”.", type="warning",
                                          multi_line=True)
                                return
                            raw = zf.read(name)
                    data = json.loads(raw)
                    assert isinstance(data, dict) and "metadata" in data
                except Exception:  # noqa: BLE001
                    ui.notify(f"That is not a {APP_NAME} spec.json or ZIP", type="negative")
                    return
                ui.navigate.to(f"/new?import={stash_import(data)}")

            scope.on_value_change(lambda _e: load())
            await load()

    @ui.page("/copy", title=f"Copy files · {APP_NAME}")
    async def copy_files(request: Request) -> None:
        user = resolve_user(request, svc.settings)
        with frame(user.email, svc.adc):
            await CopyPage(svc, user.email).build()

    @ui.page("/search", title=f"Find files · {APP_NAME}")
    async def find_files(request: Request) -> None:
        user = resolve_user(request, svc.settings)
        with frame(user.email, svc.adc):
            await SearchPage(svc, user.email, stash_import).build()

    @ui.page("/enhance", title=f"Enhance a script · {APP_NAME}")
    async def enhance(request: Request) -> None:
        user = resolve_user(request, svc.settings)
        with frame(user.email, svc.adc):
            EnhancePage(svc, user.email, stash_import).build()

    @ui.page("/new", title=f"New process · {APP_NAME}")
    async def new_process(request: Request) -> None:
        user = resolve_user(request, svc.settings)
        token = request.query_params.get("import")
        pending = _pending_imports.pop(token, None) if token else None
        notes: list[str] = []
        title = None
        if pending is None:
            kind = request.query_params.get("type")
            spec = blank_spec(user.email, kind if kind in PROCESS_TYPES else "pipeline", svc.settings.default_projects())
        else:
            spec, title = pending["spec"], pending.get("title")
            spec.setdefault("metadata", {})["owner"] = user.email
            spec["metadata"]["co_owners"] = []
            spec, notes = _fill_defaults(spec, svc.settings.default_projects())  # older specs are upgraded; hand-written ones may lack sections
            notes = list(pending.get("notes") or []) + notes
        state = WizardState(spec=spec, user=user.email, is_new=True, can_edit=True, migration_notes=notes,
                            default_projects=svc.settings.default_projects())
        if title:
            state.notes_title = title
        with frame(user.email, svc.adc):
            Wizard(state, svc).build()

    @ui.page("/p/{owner}/{name}", title=f"Process · {APP_NAME}")
    async def process_page(request: Request, owner: str, name: str) -> None:
        user = resolve_user(request, svc.settings)
        with frame(user.email, svc.adc):
            try:
                meta, _ = await run.io_bound(svc.store.get_meta, owner, name)
            except WorkspaceError as exc:
                ui.label(str(exc)).classes("text-lg")
                ui.button("Back to processes", on_click=lambda: ui.navigate.to("/")).props("flat no-caps")
                return
            can_edit = svc.store.can_edit(meta, user.email)
            version = request.query_params.get("version")
            spec = None
            if version and version.isdigit():
                spec = (await run.io_bound(svc.store.get_version, owner, name, int(version)))["spec"]
            if spec is None and can_edit:
                draft = await run.io_bound(svc.store.get_draft, owner, name, user.email)
                spec = draft["spec"] if draft else None
            if spec is None and meta.latest_version:
                spec = (await run.io_bound(svc.store.get_version, owner, name, meta.latest_version))["spec"]
            if spec is None:
                draft = await run.io_bound(svc.store.get_draft, owner, name, meta.owner)
                spec = draft["spec"] if draft else blank_spec(meta.owner)
            spec, notes = _fill_defaults(spec, svc.settings.default_projects())
            state = WizardState(spec=spec, user=user.email, owner=meta.owner, name=meta.name, can_edit=can_edit,
                                is_new=False, versions=[v.model_dump() for v in meta.versions], migration_notes=notes,
                                default_projects=svc.settings.default_projects())
            state.mark_saved(None)
            if version:
                ui.label(f"Loaded version {version}. Changes are saved as a new draft.").classes("sf-hint")
            Wizard(state, svc).build()


def _fill_defaults(spec: dict[str, Any], default_projects: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Upgrade older specs (v1/v2) and give every input something to bind to; read/write projects that are still
    empty in an environment get the hub's default project there (e.g. value-gcp). Returns (spec, migration notes)."""
    notes: list[str] = []
    if int(spec.get("spec_version") or 1) < SPEC_VERSION:
        spec, notes = upgrade_spec(spec)
    spec = ensure_builtin_variables(spec)
    spec.setdefault("process_type", "pipeline")
    base = blank_spec(spec.get("metadata", {}).get("owner", ""))
    for key in ("metadata", "environments", "email"):
        merged = dict(base[key])
        merged.update(spec.get(key) or {})
        spec[key] = merged
    for key in ("send_on", "receivers"):  # nested email settings
        merged = dict(base["email"][key])
        merged.update(spec["email"].get(key) or {})
        spec["email"][key] = merged
    variables = []
    for v in spec.get("variables") or []:
        v = {"group": "other", "description": "", **v}
        v["values"] = {**empty_values(), **{k: val or "" for k, val in (v.get("values") or {}).items()}}
        variables.append(v)
    fill_default_projects(variables, default_projects)
    spec["variables"] = variables
    spec.setdefault("secrets", [])
    from .state import new_step

    filled = []
    for st in spec.setdefault("steps", []):
        template = new_step(st.get("type", "sql"), [])
        if st.get("type") == "copy" and "direction" not in st:
            template["direction"] = None  # made before 3.1: opening it must not change it
        template.update(st)
        filled.append(template)
    spec["steps"] = filled
    return spec, notes
