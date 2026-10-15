"""ADC identity lookup: never raises, never blocks pages, finds the email for each credential type."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import adc as adc_mod
from app.services.adc import AdcIdentity, AdcLookup, lookup_adc_identity


class _ServiceAccountCreds:
    service_account_email = "runner@proj.iam.gserviceaccount.com"


class _UserCreds:  # like google.oauth2.credentials.Credentials: no email until tokeninfo
    token = "ya29.token"

    def refresh(self, request):
        pass


_UserCreds.__module__ = "google.oauth2.credentials"


def test_service_account_email_needs_no_network(monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_ServiceAccountCreds(), "proj"))
    identity = lookup_adc_identity()
    assert identity == AdcIdentity("runner@proj.iam.gserviceaccount.com", "_ServiceAccountCreds", "proj")


def test_user_credentials_use_tokeninfo(monkeypatch):
    import google.auth
    import requests

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_UserCreds(), "proj"))
    calls = []

    def post(url, data=None, timeout=None):
        calls.append((url, data))
        return SimpleNamespace(ok=True, json=lambda: {"email": "dev@acme.com"})

    monkeypatch.setattr(requests, "post", post)
    identity = lookup_adc_identity()
    assert identity.email == "dev@acme.com" and identity.kind == "user"
    assert calls == [(adc_mod.TOKENINFO_URL, {"access_token": "ya29.token"})]


def test_missing_credentials_explain_the_fix(monkeypatch):
    import google.auth

    def no_adc(scopes=None):
        raise google.auth.exceptions.DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(google.auth, "default", no_adc)
    identity = lookup_adc_identity()
    assert identity.email is None and "gcloud auth application-default login" in identity.error
    assert identity.label == "not configured"


def test_lookup_runs_in_the_background_and_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(adc_mod, "lookup_adc_identity", lambda: calls.append(1) or AdcIdentity("a@b.com", "user"))
    lookup = AdcLookup(ttl=600)
    assert lookup.get(wait=True).email == "a@b.com"
    assert lookup.get().email == "a@b.com" and lookup.email() == "a@b.com"
    assert calls == [1]


@pytest.mark.parametrize("lookup,label", [(AdcLookup(enabled=False), "not looked up"),
                                          (AdcLookup(fixed=AdcIdentity("x@y.com", "service account")), "x@y.com")])
def test_disabled_and_fixed_lookups(lookup, label):
    assert lookup.get().label == label
