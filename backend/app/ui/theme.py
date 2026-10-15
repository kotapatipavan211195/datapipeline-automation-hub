"""Page frame, colours and small presentational helpers shared by all pages."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import quote

from nicegui import ui

from .. import APP_NAME, VERSION

CSS = """
:root { --sf-ink: #1f2937; --sf-muted: #6b7280; --sf-line: #e5e7eb; --sf-bg: #f8fafc; }
body { background: var(--sf-bg); color: var(--sf-ink); }
.sf-card { border: 1px solid var(--sf-line); border-radius: 10px; box-shadow: none; }
.sf-muted { color: var(--sf-muted); }
.sf-hint { color: var(--sf-muted); font-size: 0.8rem; line-height: 1.2rem; }
.sf-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.85rem; }
.sf-preview { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem; color: #0f766e; word-break: break-all; }
.sf-problem { color: #b91c1c; font-size: 0.8rem; }
.sf-step-chip { min-width: 2rem; justify-content: center; }
.q-stepper__tab { padding: 8px 12px !important; }
.sf-code pre { max-height: 560px; overflow: auto; }
.cm-editor { border: 1px solid var(--sf-line); border-radius: 6px; min-height: 140px; }
"""


@contextmanager
def frame(user_email: str, adc: Any = None, title: str = "", on_search: Callable[[str], Any] | None = None,
          search_value: str = ""):
    """Page chrome. ``adc`` is the AdcLookup: its identity is shown top right (whose Google permissions apply).
    The header's process search filters the home page as you type (``on_search``); on other pages Enter opens the
    home page with the results."""
    ui.add_css(CSS)
    ui.colors(primary="#1d4ed8", secondary="#0f766e", accent="#7c3aed", positive="#15803d", negative="#b91c1c", warning="#b45309")
    with ui.header(elevated=False).classes("bg-white text-gray-800 border-b items-center px-4 py-2"):
        with ui.link(target="/").classes("no-underline text-gray-900 flex items-center gap-2"):
            ui.icon("construction", size="sm").classes("text-primary")
            ui.label(APP_NAME).classes("text-lg font-semibold")
        ui.label("BigQuery & GCS pipeline builder").classes("sf-muted text-sm hidden sm:block")
        ui.space()
        search = ui.input(placeholder="Search processes: name, owner, team, tag…", value=search_value).props(
            "outlined dense clearable").classes("w-80").mark("process-search")
        with search.add_slot("prepend"):
            ui.icon("search")
        if on_search is not None:
            search.on_value_change(lambda e: on_search(e.value or ""))
        else:
            search.on("keydown.enter", lambda: ui.navigate.to(f"/?q={quote(search.value or '')}"))
        ui.button("Processes", icon="list", on_click=lambda: ui.navigate.to("/")).props("flat no-caps")
        adc_chip(adc)
        with ui.chip(user_email, icon="person").props("outline"):
            ui.tooltip(f"Signed in as {user_email} · {APP_NAME} {VERSION}")
    with ui.column().classes("w-full max-w-7xl mx-auto p-4 gap-4"):
        if title:
            ui.label(title).classes("text-2xl font-semibold")
        yield


def adc_chip(adc: Any) -> None:
    """'ADC: <email>' — the identity every Google call of the hub uses; amber when there are no usable credentials."""
    if adc is None:
        return

    def paint() -> None:
        identity = adc.get()
        chip.text = f"ADC: {identity.label}"
        chip.props(f"color={'primary' if identity.email else 'warning'}")
        tip.text = (
            f"Application Default Credentials: {identity.email} ({identity.kind}). Gemini, BigQuery dry-runs, GCS and "
            "Secret Manager checks run with this identity's permissions; generated scripts record it."
            if identity.email else
            f"No ADC identity: {identity.error or 'looking it up'}. Run: gcloud auth application-default login"
        )
        if identity.kind != "checking":
            timer.deactivate()

    with ui.chip("ADC: checking…", icon="key").props("outline").classes("sf-mono").mark("adc-chip") as chip:
        tip = ui.tooltip("").classes("text-sm max-w-sm")
    timer = ui.timer(1.0, paint)
    paint()


def hint(text: str) -> None:
    ui.label(text).classes("sf-hint")


def help_icon(text: str) -> None:
    with ui.icon("help_outline", size="xs").classes("text-gray-400 cursor-help"):
        ui.tooltip(text).classes("text-sm max-w-xs")


def section(title: str, subtitle: str = "", icon: str | None = None):
    card = ui.card().classes("sf-card w-full")
    with card:
        with ui.row().classes("items-center gap-2"):
            if icon:
                ui.icon(icon).classes("text-primary")
            ui.label(title).classes("text-base font-semibold")
        if subtitle:
            hint(subtitle)
    return card


LEVEL_STYLE = {
    "ok": ("check_circle", "text-green-700"),
    "info": ("info", "text-blue-700"),
    "warn": ("warning", "text-amber-700"),
    "error": ("error", "text-red-700"),
}


def level_icon(level: str) -> None:
    icon, cls = LEVEL_STYLE.get(level, ("help", "text-gray-500"))
    ui.icon(icon).classes(cls)


def human_bytes(n: int | None) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
