"""Fake google.api_core.exceptions: the errors generated scripts catch."""


class GoogleAPICallError(Exception):
    pass


class NotFound(GoogleAPICallError):
    pass
