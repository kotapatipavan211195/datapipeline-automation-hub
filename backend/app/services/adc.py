"""Whose Google permissions the hub uses: the Application Default Credentials (ADC) identity.

Every Google call the hub makes (Gemini, BigQuery dry-runs, GCS checks, Secret Manager checks, the workspace bucket)
runs as ADC: on Cloud Run the service account, on a laptop whatever `gcloud auth application-default login` stored.
The UI header shows this identity, and generated scripts and READMEs record it, so a passing check can be traced to
the permissions it reflects.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("dahub.adc")
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


@dataclass(frozen=True)
class AdcIdentity:
    email: str | None
    kind: str  # service account | user | impersonated service account | workload identity | none | off | checking
    project: str | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        if self.email:
            return self.email
        return {"checking": "checking…", "off": "not looked up"}.get(self.kind, "not configured")


CHECKING = AdcIdentity(None, "checking")


def _kind(creds: object) -> str:
    module = type(creds).__module__
    if "impersonated" in module:
        return "impersonated service account"
    if "service_account" in module or "compute_engine" in module:
        return "service account"
    if "external_account" in module or "identity_pool" in module or "aws" in module:
        return "workload identity"
    if "oauth2.credentials" in module:
        return "user"
    return type(creds).__name__


def _email_attr(creds: object) -> str | None:
    for attr in ("service_account_email", "signer_email", "account"):
        value = getattr(creds, attr, None)
        if isinstance(value, str) and "@" in value:
            return value
    return None


def lookup_adc_identity(timeout: float = 5.0) -> AdcIdentity:
    """Find the ADC identity now (a token refresh and a tokeninfo call at most). Never raises."""
    try:
        import google.auth
        from google.auth.transport.requests import Request
    except ImportError as exc:  # pragma: no cover - google-auth is a hard dependency
        return AdcIdentity(None, "none", error=str(exc))
    try:
        creds, project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    except Exception as exc:  # noqa: BLE001 - DefaultCredentialsError and friends
        return AdcIdentity(None, "none", error=f"no Application Default Credentials ({str(exc).splitlines()[0][:200]}); "
                                              "run: gcloud auth application-default login")
    kind = _kind(creds)
    email = _email_attr(creds)
    if email:
        return AdcIdentity(email, kind, project)
    try:
        creds.refresh(Request())
        email = _email_attr(creds)  # a VM's / Cloud Run's account is only known after a refresh
        if not email:
            import requests

            resp = requests.post(TOKENINFO_URL, data={"access_token": creds.token}, timeout=timeout)
            email = resp.json().get("email") if resp.ok else None
    except Exception as exc:  # noqa: BLE001
        return AdcIdentity(None, kind, project, error=f"credentials found but not usable: {str(exc).splitlines()[0][:200]}")
    if not email:
        return AdcIdentity(None, kind, project, error="credentials work, but they do not reveal an email address")
    return AdcIdentity(email, kind, project)


class AdcLookup:
    """The ADC identity, looked up in a background thread and cached (pages never wait for Google)."""

    def __init__(self, *, enabled: bool = True, ttl: float = 600.0, fixed: AdcIdentity | None = None):
        self.enabled, self.ttl, self.fixed = enabled, ttl, fixed
        self._value: AdcIdentity | None = None
        self._at = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def get(self, *, wait: bool = False, timeout: float = 10.0) -> AdcIdentity:
        """The cached identity; starts a refresh when it is missing or older than ttl. wait=True blocks for it."""
        if self.fixed is not None:
            return self.fixed
        if not self.enabled:
            return AdcIdentity(None, "off", error="the ADC lookup is turned off (SF_ADC_LOOKUP_ENABLED=false)")
        with self._lock:
            stale = self._value is None or time.time() - self._at > self.ttl
            if stale and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._refresh, name="adc-lookup", daemon=True)
                self._thread.start()
            thread, value = self._thread, self._value
        if wait and value is None and thread is not None:
            thread.join(timeout)
            value = self._value
        return value or CHECKING

    def email(self) -> str:
        """For generated files: the identity's email, waiting briefly if it is still being looked up."""
        return self.get(wait=True).email or "unknown"

    def _refresh(self) -> None:
        value = lookup_adc_identity()
        if value.email:
            log.info("Google calls use ADC identity %s (%s)", value.email, value.kind)
        else:
            log.warning("ADC identity unknown: %s", value.error)
        with self._lock:
            self._value, self._at = value, time.time()
