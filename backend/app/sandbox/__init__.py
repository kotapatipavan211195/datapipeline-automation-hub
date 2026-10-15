"""Offline sandbox for smoke-running generated scripts: a fake ``google`` package (``fake_google/``) that the
smoke run puts first on PYTHONPATH, plus a ``sitecustomize`` that stubs SMTP and the gcloud CLI. Never imported by
the hub itself."""
