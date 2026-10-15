"""Fake google package for offline smoke runs of generated scripts (Datapipeline Automation Hub)."""

from . import auth, cloud  # noqa: F401  (``import google`` must make google.auth available, like the real package)
