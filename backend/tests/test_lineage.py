"""Table inventory for the README / test-run summary: dependencies vs targets, files read and written (lab values)."""

from __future__ import annotations

from app.models.rules import fill_variables
from app.models.spec import ProcessSpec
from app.services.lineage import process_tables, step_io

from conftest import load_sample, minimal_spec


def _spec(*steps):
    data = minimal_spec(*steps)
    data["variables"].append({"name": "ref_project", "group": "project", "values": {"lab": "acme-ref-dev", "value": "acme-ref-prod"}})
    data["variables"].append({"name": "landing", "group": "bucket", "values": {"lab": "acme-landing-dev", "value": "acme-landing"}})
    return ProcessSpec.model_validate(data)


def test_dependencies_targets_and_files():
    spec = _spec(
        {"type": "ingest", "function_name": "load_raw", "read_bucket": "{landing}", "read_path": "in", "filename": "orders_",
         "columns": [{"name": "id", "type": "INTEGER"}], "table_name": "raw_orders_stg", "target_table": "raw_orders",
         "staging_dataset": "{ds}", "target_dataset": "{ds}", "merge_keys": ["id"], "archive_path": "in/archive"},
        {"type": "sql", "function_name": "build_mart", "statement_kind": "MERGE",
         "sql": "MERGE `${write_project}.${ds}.orders` T USING (SELECT o.id FROM `${write_project}.${ds}.raw_orders` o "
                "JOIN `${ref_project}.reference.stores` s ON s.id = o.id JOIN ${ds}.calendar c ON c.d = o.id) S "
                "ON T.id = S.id WHEN NOT MATCHED THEN INSERT ROW"},
        {"type": "export", "function_name": "export_mart", "source_table": "{write_project}.{ds}.orders",
         "bucket": "{landing}", "path": "out", "file_name": "orders_", "file_format": "TXT"},
        {"type": "copy", "function_name": "copy_out", "sources": ["gs://{landing}/out/orders_*.txt"], "destination": "/data/share/"},
        {"type": "dq", "function_name": "check_mart", "checks": [{"kind": "row_count_min", "table": "{write_project}.{ds}.orders"}]},
    )
    inv = process_tables(spec)
    deps = {d.resolved: d for d in inv.dependencies}
    assert set(deps) == {"acme-ref-dev.reference.stores", "dataset_a.calendar"}
    assert deps["dataset_a.calendar"].job_project  # dataset.table runs in the credentials' project
    assert deps["acme-ref-dev.reference.stores"].template == "{ref_project}.reference.stores"
    targets = {t.resolved: t for t in inv.targets}
    assert list(targets) == ["proj-dev.dataset_a.raw_orders_stg", "proj-dev.dataset_a.raw_orders_stg_history",
                             "proj-dev.dataset_a.raw_orders", "proj-dev.dataset_a.orders"]  # step order, then as written
    assert targets["proj-dev.dataset_a.raw_orders"].how == ["MERGE from the staging table (newest row per key)"]
    assert targets["proj-dev.dataset_a.raw_orders"].read_after_write == ["build_mart"]
    assert targets["proj-dev.dataset_a.orders"].read_after_write == ["export_mart", "check_mart"]
    assert inv.gcs_inputs[0].uri == "gs://acme-landing-dev/in/*orders_*"
    assert inv.gcs_outputs[0].uri == "gs://acme-landing-dev/in/archive/"
    assert inv.gcs_outputs[1].uri == "gs://acme-landing-dev/out/orders_<%Y_%m_%d>.txt" and "TXT" in inv.gcs_outputs[1].role
    assert inv.gcs_outputs[-1].uri == "/data/share/" and "(local)" in inv.gcs_outputs[-1].role


def test_prod_values_are_only_used_when_asked():
    spec = _spec({"type": "sql", "function_name": "q", "statement_kind": "DDL",
                  "sql": "CREATE TABLE `${write_project}.${ds}.t` AS SELECT 1 AS x"})
    assert [t.resolved for t in process_tables(spec).targets] == ["proj-dev.dataset_a.t"]
    assert [t.resolved for t in process_tables(spec, "value").targets] == ["proj-prod.dataset_a.t"]
    assert step_io(spec)[0].writes == {"proj-dev.dataset_a.t"}


def test_a_table_read_before_it_is_rebuilt_is_a_dependency():
    spec = _spec(
        {"type": "sql", "function_name": "append_history", "statement_kind": "DML",
         "sql": "INSERT INTO `${write_project}.${ds}.history` SELECT * FROM `${write_project}.${ds}.history_stage`"},
        {"type": "sql", "function_name": "rebuild_stage", "statement_kind": "DDL",
         "sql": "CREATE OR REPLACE TABLE `${write_project}.${ds}.history_stage` AS SELECT 1 AS x"},
    )
    inv = process_tables(spec)
    assert [d.resolved for d in inv.dependencies] == ["proj-dev.dataset_a.history_stage"]
    assert {t.resolved: t.how for t in inv.targets}["proj-dev.dataset_a.history_stage"] == ["CREATE OR REPLACE TABLE"]


def test_sample_readme_lists_targets_and_files():
    from app.services.renderer import render_process

    readme = render_process(ProcessSpec.model_validate(load_sample("sales_daily")), smoke_run=False).files["README.md"]
    assert "## Dependencies (source tables)" in readme and "## Target tables" in readme
    assert "| `acme-analytics-dev.sales_mart.sales_daily` | `build_sales_daily_agg` | MERGE |" in readme


def test_fill_variables_keeps_date_tokens():
    assert fill_variables("gs://{b}/x/{date:%Y%m%d}/{other}", {"b": "bkt"}) == "gs://bkt/x/{date:%Y%m%d}/{other}"
