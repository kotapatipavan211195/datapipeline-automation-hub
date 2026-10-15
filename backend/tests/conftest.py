from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


def load_sample(name: str) -> dict:
    return json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture()
def sales_spec_dict() -> dict:
    return copy.deepcopy(load_sample("sales_daily"))


@pytest.fixture()
def customer_spec_dict() -> dict:
    return copy.deepcopy(load_sample("customer_snapshot"))


@pytest.fixture()
def strategic_spec_dict() -> dict:
    return copy.deepcopy(load_sample("strategic_template"))


@pytest.fixture()
def ingest_spec_dict() -> dict:
    return copy.deepcopy(load_sample("engagement_file_ingest"))


def minimal_spec(*steps: dict, **overrides) -> dict:
    """Smallest valid v3 spec: lab/value projects, one dataset variable and (by default) one SQL step."""
    spec = {
        "spec_version": 3,
        "metadata": {"name": "mini_process", "description": "test", "authors": ["P00001"]},
        "variables": [
            {"name": "read_project", "group": "project", "values": {"lab": "proj-dev", "value": "proj-prod"}},
            {"name": "write_project", "group": "project", "values": {"lab": "proj-dev", "value": "proj-prod"}},
            {"name": "ds", "group": "dataset", "values": {"lab": "dataset_a", "value": "dataset_a"}},
        ],
        "steps": list(steps)
        or [{"type": "sql", "function_name": "run_report", "sql": "SELECT 1 AS x", "statement_kind": "SELECT"}],
    }
    spec.update(overrides)
    return spec


pytest_plugins = ["nicegui.testing.user_plugin"]
