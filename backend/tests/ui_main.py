"""NiceGUI entry point for UI tests: the real pages wired to fake GCP clients and a temp workspace."""

import os
import sys
import tempfile
from pathlib import Path

from nicegui import ui

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deps import Services  # noqa: E402
from app.services.adc import AdcIdentity, AdcLookup  # noqa: E402
from app.services.sql_analyzer import SqlAnalyzer  # noqa: E402
from app.services.validators import ClientFactory  # noqa: E402
from app.services.workspace import LocalBackend, WorkspaceStore  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.ui.pages import register_pages  # noqa: E402
from fakes import FakeBq, FakeGcs  # noqa: E402

workspace = Path(os.environ.get("SF_UI_TEST_WS") or tempfile.mkdtemp())
settings = Settings(
    auth_mode="local", dev_user_email="alice@example.com", local_workspace_dir=workspace, gcp_project="acme-dev",
    smoke_run_enabled=False, gemini_enabled=False, log_format="text",
)
services = Services(
    settings=settings,
    store=WorkspaceStore(LocalBackend(workspace)),
    gemini=None,
    analyzer=SqlAnalyzer(None),
    clients=ClientFactory(bq=FakeBq(datasets={"acme-dev.raw"}), gcs=FakeGcs({
        "landing": {"in/a_20260101.csv": b"id,name\n1,x\n"},
        "drop": {"out/Daily File_1.csv": b"a\n", "out/Daily File_2.csv": b"b\n", "out/readme.txt": b"x"},
    })),
    adc=AdcLookup(fixed=AdcIdentity("hub-sa@acme-dev.iam.gserviceaccount.com", "service account")),
)
register_pages(services)
ui.run(storage_secret="ui-tests")
