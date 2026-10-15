"""Fake google.auth: default() returns dummy credentials and a smoke project."""


class FakeCredentials:
    service_account_email = "smoke-run@example.iam.gserviceaccount.com"
    token = "smoke"
    valid = True


def default(scopes=None, **_kwargs):
    return FakeCredentials(), "smoke-project"
