"""Who is calling: IAP identity in production, a dev identity locally.

IAP (in front of Cloud Run) only lets members of the allowed Google group through. Every request still carries a
signed JWT (x-goog-iap-jwt-assertion) that we verify against Google's IAP keys and the configured audience, so the
backend never trusts a spoofable header. Adapted from search_impact/ui/auth.py, with key caching.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .app_logging import user_var
from .settings import Settings

log = logging.getLogger("dahub.auth")

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
HDR_JWT = "x-goog-iap-jwt-assertion"
HDR_DEV_USER = "x-dev-user"  # honoured only when SF_AUTH_MODE=local (tests, laptops)


@dataclass(frozen=True)
class CurrentUser:
    email: str
    source: str  # "iap" | "local"


class _CertCache:
    def __init__(self, ttl: int = 3600):
        self.ttl, self._certs, self._at = ttl, None, 0.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if self._certs is None or time.time() - self._at > self.ttl:
                import requests

                resp = requests.get(IAP_CERTS_URL, timeout=10)
                resp.raise_for_status()
                self._certs, self._at = resp.json(), time.time()
            return self._certs


_certs = _CertCache()


def verify_iap_jwt(token: str, audience: str) -> dict:
    from google.auth import jwt

    payload = jwt.decode(token, certs=_certs.get(), audience=audience, clock_skew_in_seconds=30)
    if payload.get("iss") != IAP_ISSUER:
        raise ValueError(f"unexpected issuer {payload.get('iss')!r}")
    return payload


class _GroupChecker:
    """Optional defence in depth: confirm membership of SF_ALLOWED_GROUP via the Cloud Identity Groups API."""

    def __init__(self, group_email: str):
        self.group_email = group_email
        self._group_name: str | None = None
        self._cache: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    def _session(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-identity.groups.readonly"])
        return AuthorizedSession(creds)

    def is_member(self, email: str) -> bool:
        now = time.time()
        with self._lock:
            hit = self._cache.get(email)
            if hit and now - hit[1] < 600:
                return hit[0]
        session = self._session()
        if self._group_name is None:
            r = session.get("https://cloudidentity.googleapis.com/v1/groups:lookup", params={"groupKey.id": self.group_email}, timeout=10)
            r.raise_for_status()
            self._group_name = r.json()["name"]
        r = session.get(
            f"https://cloudidentity.googleapis.com/v1/{self._group_name}/memberships:checkTransitiveMembership",
            params={"query": f"member_key_id == '{email}'"},
            timeout=10,
        )
        r.raise_for_status()
        member = bool(r.json().get("hasMembership"))
        with self._lock:
            self._cache[email] = (member, now)
        return member


_group_checkers: dict[str, _GroupChecker] = {}


def resolve_user(request: Request, settings: Settings) -> CurrentUser:
    if settings.auth_mode == "local":
        email = (request.headers.get(HDR_DEV_USER) or settings.dev_user_email).strip().lower()
        user = CurrentUser(email=email, source="local")
    else:
        if not settings.iap_audience:
            raise HTTPException(500, "SF_IAP_AUDIENCE is not configured")
        token = request.headers.get(HDR_JWT, "").strip()
        if not token:
            raise HTTPException(401, "No IAP identity on the request. Open the Datapipeline Automation Hub through its IAP-protected URL.")
        try:
            payload = verify_iap_jwt(token, settings.iap_audience)
        except Exception as exc:  # noqa: BLE001
            log.warning("IAP token rejected: %s", exc)
            raise HTTPException(401, "Your sign-in could not be verified. Reload the page.") from exc
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise HTTPException(401, "The IAP token has no email claim")
        user = CurrentUser(email=email, source="iap")
        if settings.allowed_group:
            checker = _group_checkers.setdefault(settings.allowed_group, _GroupChecker(settings.allowed_group))
            try:
                allowed = checker.is_member(email)
            except Exception as exc:  # noqa: BLE001
                log.error("group membership check failed: %s", exc)
                raise HTTPException(503, "Could not confirm your group membership; try again shortly") from exc
            if not allowed:
                raise HTTPException(403, f"{email} is not a member of {settings.allowed_group}")
    user_var.set(user.email)
    return user
