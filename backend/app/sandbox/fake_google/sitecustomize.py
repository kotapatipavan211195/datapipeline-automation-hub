"""Loaded automatically (PYTHONPATH) in offline smoke runs: puts the fake google package in charge and stubs the mail
relay, the gcloud CLI and local attachments that only exist on the pipeline server. Never used in lab test runs."""

import builtins
import io
import json
import os
import shutil
import smtplib
import subprocess
import sys

_LOG = os.environ.get("DAHUB_SMOKE_LOG")


def _record(kind, **data):
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **data}, default=str) + "\n")


def _use_fake_google():
    """Older google-cloud / protobuf packages install ``*-nspkg.pth`` files that import the real ``google`` namespace
    while Python starts, before PYTHONPATH matters: ``import google.auth`` would then reach the real client libraries
    (and fail without a project). Drop whatever is already loaded so ``google`` is the fake package next to this file."""
    preloaded = [name for name in sys.modules if name == "google" or name.startswith("google.")]
    for name in preloaded:
        del sys.modules[name]
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import google

    _record("boot", google=getattr(google, "__file__", None), purged=len(preloaded))


_use_fake_google()


class _FakeSMTP:
    def __init__(self, host="", port=0, *args, **kwargs):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, *args, **kwargs):
        return (220, b"ok")

    def send_message(self, message, *args, **kwargs):
        attachments = [part.get_filename() for part in message.iter_attachments()] if message.is_multipart() else []
        body = message.get_body(("plain",))
        _record("email", host=self.host, port=self.port, to=message["To"], subject=message["Subject"],
                sender=message["From"], attachments=attachments, body=body.get_content() if body else "")
        return {}

    def quit(self):
        pass


smtplib.SMTP = _FakeSMTP

_real_which = shutil.which
_real_run = subprocess.run


def _which(cmd, *args, **kwargs):
    if os.path.basename(str(cmd)).lower().startswith("gcloud"):
        return _real_which(cmd, *args, **kwargs) or "gcloud"
    return _real_which(cmd, *args, **kwargs)


def _run(cmd, *args, **kwargs):
    if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])).lower().startswith("gcloud"):
        _record("gcloud", args=[str(c) for c in cmd[1:]])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="Copying files (smoke run: nothing copied)\n")
    return _real_run(cmd, *args, **kwargs)


shutil.which = _which
subprocess.run = _run

_real_open = builtins.open


def _open(file, mode="r", *args, **kwargs):
    if "rb" in str(mode) and isinstance(file, (str, os.PathLike)) and not os.path.exists(file):
        _record("local_read", path=str(file))
        return io.BytesIO(b"smoke-run placeholder\n")
    return _real_open(file, mode, *args, **kwargs)


builtins.open = _open
