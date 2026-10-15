"""The offline smoke run must use the fake google package even when the hub's Python pre-imports the real one."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.services.smoke_run import SANDBOX_DIR

# What a legacy *-nspkg.pth file does at start-up: put the real google namespace (here: a stand-in that breaks
# when used) into sys.modules before the script's imports run.
PRELOAD = """
import sys, types
real = {real!r}
google = types.ModuleType("google"); google.__path__ = [real + "/google"]
cloud = types.ModuleType("google.cloud"); cloud.__path__ = [real + "/google/cloud"]
google.cloud = cloud
sys.modules["google"], sys.modules["google.cloud"] = google, cloud
sys.path.insert(0, {sandbox!r})
import sitecustomize
import google.auth
from google.cloud import bigquery
print(bigquery.__file__)
print(google.auth.default()[1])
"""


def test_fake_google_wins_over_a_preloaded_real_namespace(tmp_path: Path) -> None:
    real = tmp_path / "site-packages"
    (real / "google" / "cloud").mkdir(parents=True)
    (real / "google" / "auth").mkdir()
    (real / "google" / "cloud" / "bigquery.py").write_text("raise ImportError('the real bigquery was imported')\n",
                                                           encoding="utf-8")
    (real / "google" / "auth" / "__init__.py").write_text("def default(**_):\n    return None, None\n", encoding="utf-8")
    log = tmp_path / "smoke.jsonl"
    code = PRELOAD.format(real=real.as_posix(), sandbox=str(SANDBOX_DIR))
    proc = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, encoding="utf-8",
                          env={"DAHUB_SMOKE_LOG": str(log), "SYSTEMROOT": "C:\\Windows"}, timeout=60)
    assert proc.returncode == 0, proc.stderr
    bigquery_file, project = proc.stdout.split()
    assert Path(bigquery_file).resolve().is_relative_to(SANDBOX_DIR)
    assert project == "smoke-project"
    boot = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert boot["kind"] == "boot" and boot["purged"] == 2
    assert Path(boot["google"]).resolve().is_relative_to(SANDBOX_DIR)
