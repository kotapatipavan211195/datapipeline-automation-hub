"""Datapipeline Automation Hub web UI (NiceGUI, pure Python) mounted on the same FastAPI app as the REST API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from nicegui import ui

from .. import APP_NAME

from .pages import register_pages


def setup_ui(fastapi_app: FastAPI, svc: Any, *, storage_secret: str) -> None:
    register_pages(svc)
    ui.run_with(
        fastapi_app,
        title=APP_NAME,
        favicon="🛠️",
        storage_secret=storage_secret,
        show_welcome_message=False,
        reconnect_timeout=30.0,
    )
