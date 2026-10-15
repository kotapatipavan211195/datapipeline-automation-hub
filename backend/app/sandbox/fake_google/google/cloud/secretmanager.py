"""Fake google.cloud.secretmanager: every secret holds a small JSON payload."""


class _Payload:
    data = b'{"smoke": "secret"}'


class _Response:
    def __init__(self, name):
        self.name = name
        self.payload = _Payload()


class SecretManagerServiceClient:
    def __init__(self, credentials=None, **kwargs):
        pass

    def access_secret_version(self, request=None, name=None, **kwargs):
        return _Response((request or {}).get("name", name))

    def list_secrets(self, request=None, **kwargs):
        return []
