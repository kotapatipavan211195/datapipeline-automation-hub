"""Service wiring shared by the routers (kept on app.state so tests can swap any piece)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from .auth import CurrentUser, resolve_user
from .models.spec import ProcessSpec, format_validation_errors
from .services.adc import AdcLookup
from .services.copy_tool import CopyJobManager
from .services.gemini import GeminiService
from .services.sql_analyzer import SqlAnalyzer
from .services.test_runner import TestRunManager
from .services.validators import ClientFactory, SpecValidator
from .services.workspace import WorkspaceStore, make_store
from .settings import Settings


@dataclass
class Services:
    settings: Settings
    store: WorkspaceStore
    gemini: GeminiService | None
    analyzer: SqlAnalyzer
    clients: ClientFactory
    adc: AdcLookup = field(default_factory=lambda: AdcLookup(enabled=False))  # whose Google permissions the hub uses
    test_runs: TestRunManager | None = None  # lab test runs (created in __post_init__)
    copy_jobs: CopyJobManager | None = None  # the Copy files tool (created in __post_init__)

    def __post_init__(self) -> None:
        if self.test_runs is None:
            self.test_runs = TestRunManager(self.settings)
        if self.copy_jobs is None:
            self.copy_jobs = CopyJobManager(self.settings, self.clients, self.store)

    def copy_project(self) -> str:
        """The project the Copy files tool lists buckets in (and bills requests to)."""
        return self.settings.gcp_project or getattr(self.adc.get(), "project", None) or ""

    def adc_email(self) -> str:
        """The ADC identity's email: the email sender and the only test-run recipient ('' when unknown)."""
        email = self.adc.email()
        return email if email and "@" in email else ""

    def validator(self) -> SpecValidator:
        ident = self.adc.get()
        return SpecValidator(self.clients, sample_bytes=self.settings.sample_read_bytes, settings=self.settings,
                             adc_project=getattr(ident, "project", None), adc_email=self.adc_email())


def build_services(settings: Settings) -> Services:
    gemini = GeminiService(settings) if settings.gemini_enabled and settings.gcp_project else None
    svc = Services(
        settings=settings,
        store=make_store(settings.workspace_bucket, settings.local_workspace_dir),
        gemini=gemini,
        analyzer=SqlAnalyzer(gemini),
        clients=ClientFactory(),
        adc=AdcLookup(enabled=settings.adc_lookup_enabled),
    )
    svc.adc.get()  # start the lookup now, so the first page already shows it
    return svc


def services(request: Request) -> Services:
    return request.app.state.services


def current_user(request: Request) -> CurrentUser:
    return resolve_user(request, request.app.state.services.settings)


def parse_spec(data: Any) -> ProcessSpec:
    """Validate a spec dict, returning field-level errors the wizard can show next to inputs."""
    try:
        return ProcessSpec.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "The process has problems to fix first", "errors": format_validation_errors(exc)},
        ) from exc
