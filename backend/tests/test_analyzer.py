from __future__ import annotations

from app.services.gemini import GeminiError
from app.services.sql_analyzer import SqlAnalyzer, check_equivalence, scan_projects

from fakes import FakeGemini

SQL = (
    "INSERT INTO `acme-dev.sales_mart.daily` (store_id, total)\n"
    "SELECT store_id, SUM(amount) -- daily total\n"
    "FROM `acme-dev.raw_sales.store_sales`\n"
    "WHERE sale_date = DATE '2026-01-14'\n"
    "GROUP BY store_id"
)
EMPTY = {"read_project": "", "write_project": ""}  # a new process: built-in projects not set yet

GOOD = {
    "summary": "Aggregates store sales for the day into the daily mart.",
    "suggested_function_name": "load_daily_store_totals",
    "statement_kind": "DML",
    "parameterized_sql": SQL.replace("acme-dev.sales_mart", "${write_project}.${mart_dataset}")
    .replace("acme-dev.raw_sales", "${write_project}.${raw_dataset}")
    .replace("DATE '2026-01-14'", "@report_date"),
    "variables": [
        {"name": "write_project", "group": "project", "value": "acme-dev", "description": "target project"},
        {"name": "mart_dataset", "group": "dataset", "value": "sales_mart", "description": "mart"},
        {"name": "raw_dataset", "group": "dataset", "value": "raw_sales", "description": "raw"},
    ],
    "parameters": [
        {"name": "report_date", "bq_type": "DATE", "source": "today_offset", "offset_days": -1,
         "original_literal": "DATE '2026-01-14'", "default": "", "description": "yesterday"},
    ],
    "source_tables": ["acme-dev.raw_sales.store_sales"],
    "target_tables": ["acme-dev.sales_mart.daily"],
    "suggested_destination_table": "",
    "findings": [{"severity": "info", "message": "Consider MERGE for idempotent reruns."}],
}


def test_good_gemini_proposal_is_accepted():
    analysis = SqlAnalyzer(FakeGemini(GOOD)).analyze(SQL, existing_variables=EMPTY)
    assert analysis.equivalence.ok and analysis.ai_used
    assert "${mart_dataset}" in analysis.sql and "@report_date" in analysis.sql
    assert "-- daily total" in analysis.sql  # comments/formatting preserved
    assert [(p.name, p.source, p.offset_days) for p in analysis.parameters] == [("report_date", "today_offset", -1)]
    assert {(v.name, v.group, v.values.lab) for v in analysis.variables} == {
        ("write_project", "project", "acme-dev"), ("mart_dataset", "dataset", "sales_mart"), ("raw_dataset", "dataset", "raw_sales")}
    assert all(v.values.value == "" for v in analysis.variables)  # prod values are never guessed
    assert analysis.suggested_function_name == "load_daily_store_totals"
    assert analysis.targets == ["{write_project}.{mart_dataset}.daily"]
    assert any(f.source == "gemini" for f in analysis.findings)


def test_logic_change_is_rejected_and_rule_based_used():
    bad = dict(GOOD, parameterized_sql=GOOD["parameterized_sql"].replace("SUM(amount)", "SUM(amount) * 2"))
    analysis = SqlAnalyzer(FakeGemini(bad)).analyze(SQL, existing_variables=EMPTY)
    assert analysis.equivalence.ok  # the rule-based fallback is itself verified
    assert analysis.rejected_proposal and "* 2" in analysis.rejected_proposal
    assert "* 2" not in analysis.sql
    assert analysis.findings[0].severity == "warning" and "rejected" in analysis.findings[0].message
    # rule-based: the written project becomes write_project, datasets variables, the date a YAML value
    assert "`${write_project}.${sales_mart_dataset}.daily`" in analysis.sql and "@cutoff_date" in analysis.sql
    cutoff = analysis.parameters[0]
    assert (cutoff.source, cutoff.default, cutoff.original_literal) == ("config", "2026-01-14", "DATE '2026-01-14'")


def test_wrong_original_literal_is_rejected():
    bad = dict(GOOD)
    bad["parameters"] = [dict(GOOD["parameters"][0], original_literal="DATE '2026-01-13'")]
    analysis = SqlAnalyzer(FakeGemini(bad)).analyze(SQL, existing_variables=EMPTY)
    assert analysis.rejected_proposal is not None


def test_existing_variable_is_reused_by_lab_value():
    existing = {"read_project": "", "write_project": "acme-dev", "mart": "sales_mart"}
    analysis = SqlAnalyzer(FakeGemini(GOOD)).analyze(SQL, existing_variables=existing)
    assert "${mart}" in analysis.sql and "mart_dataset" not in analysis.sql
    assert {v.name for v in analysis.variables} == {"raw_dataset"}


def test_gemini_failure_falls_back_to_rules():
    analysis = SqlAnalyzer(FakeGemini(error=GeminiError("quota"))).analyze(SQL, existing_variables=EMPTY)
    assert not analysis.ai_used and analysis.equivalence.ok
    assert any("AI analysis unavailable" in f.message for f in analysis.findings)
    assert analysis.suggested_function_name == "load_daily"


def test_no_gemini_configured():
    analysis = SqlAnalyzer(None).analyze("SELECT 1 AS x")
    assert analysis.sql == "SELECT 1 AS x" and analysis.statement_kind == "SELECT"
    assert analysis.suggested_function_name == "build_sql"


def test_taken_function_names_are_avoided():
    analysis = SqlAnalyzer(FakeGemini(GOOD)).analyze(SQL, existing_variables=EMPTY, taken_function_names=["load_daily_store_totals"])
    assert analysis.suggested_function_name != "load_daily_store_totals"


def test_parse_error_is_reported():
    analysis = SqlAnalyzer(None).analyze("SELEC broken FROM")
    assert analysis.parse_error or analysis.findings


def test_results_are_cached():
    gemini = FakeGemini(GOOD)
    analyzer = SqlAnalyzer(gemini)
    analyzer.analyze(SQL, existing_variables=EMPTY)
    analyzer.analyze(SQL, existing_variables=EMPTY)
    assert gemini.calls == 1


def test_equivalence_requires_original_literals():
    result = check_equivalence("SELECT 1 WHERE d = '2026-01-01'", "SELECT 1 WHERE d = @report_date", {}, {})
    assert not result.ok and "no original value" in result.reason


# ---------------------------------------------------------------- automatic project scan (no AI)
def test_scan_maps_written_and_read_projects_to_the_builtins():
    sql = ("INSERT INTO `demo-analytics-dev.mart.orders` SELECT o.id FROM `value-confidential.raw.orders` o "
           "JOIN acme-ref-prod.reference.stores s ON s.id = o.id JOIN staging.extra x ON x.id = o.id")
    scan = scan_projects(sql, {}, step="load_orders")
    assert scan.changed and scan.equivalence.ok
    assert scan.sql == ("INSERT INTO `${write_project}.mart.orders` SELECT o.id FROM `${read_project}.raw.orders` o "
                        "JOIN ${acme_ref_project}.reference.stores s ON s.id = o.id JOIN staging.extra x ON x.id = o.id")
    assert [(v.name, v.values.lab) for v in scan.variables] == [
        ("write_project", "demo-analytics-dev"), ("read_project", "value-confidential"), ("acme_ref_project", "acme-ref-prod")]
    assert {(t.role, t.name, t.variable) for t in scan.tables} == {
        ("reads", "value-confidential.raw.orders", "read_project"),
        ("reads", "acme-ref-prod.reference.stores", "acme_ref_project"),
        ("reads", "staging.extra", None),
        ("writes", "demo-analytics-dev.mart.orders", "write_project"),
    }
    assert any("no project" in f.message for f in scan.findings)
    assert scan.targets == ["{write_project}.mart.orders"]


def test_scan_reuses_existing_variables_and_is_stable():
    existing = {"read_project": "", "write_project": "acme-dev", "ref": "acme-ref-prod"}
    first = scan_projects("SELECT * FROM `acme-ref-prod.reference.stores`", existing)
    assert first.sql == "SELECT * FROM `${ref}.reference.stores`" and first.variables == []
    again = scan_projects(first.sql, existing)  # already parameterized: nothing to do, tables still listed
    assert not again.changed and again.tables[0].variable == "ref"


def test_project_variable_names_drop_environment_words():
    from app.services.sql_analyzer import project_variable_name

    assert project_variable_name("acme-raw-prod") == "acme_raw_project"
    assert project_variable_name("acme-analytics-dev") == "acme_analytics_project"
    assert project_variable_name("prod") == "prod_project"
    assert project_variable_name("123-data") == "p_123_data_project"


def test_project_scan_keeps_variables_already_in_the_sql():
    """SQL that already uses ${variables} (imported scripts, export queries) still gets its hard-coded projects replaced."""
    from app.services.sql_analyzer import scan_projects

    sql = ("create or replace table `${write_project}.${mart_dataset}.t` as\n"
           "select id from `acme-raw-dev.membership.members` where d >= @cutoff")
    scan = scan_projects(sql, {"read_project": "acme-raw-dev", "write_project": "acme-dev", "mart_dataset": "mart"})
    assert scan.changed and "`${read_project}.membership.members`" in scan.sql and "${mart_dataset}" in scan.sql



# ---------------------------------------------------------------- prod default project (value-gcp) and other projects
PROD_DEFAULTS = {"value": "value-gcp"}
NEW_PROCESS = {"value": {"read_project": "value-gcp", "write_project": "value-gcp"}}  # what a new process starts with


def test_prod_named_sql_maps_to_the_builtins_and_leaves_lab_to_type():
    sql = "INSERT INTO `value-gcp.mart.orders` SELECT id FROM `value-gcp.raw.orders`"
    scan = scan_projects(sql, {}, env_values=NEW_PROCESS, defaults=PROD_DEFAULTS)
    assert scan.changed and scan.equivalence.ok  # filled back with value-gcp, not with the (empty) lab value
    # tables in the project the SQL writes into use ${write_project}, as for lab SQL
    assert scan.sql == "INSERT INTO `${write_project}.mart.orders` SELECT id FROM `${write_project}.raw.orders`"
    assert scan.variables == []  # both built-ins already hold value-gcp in value; lab stays for the user


def test_the_default_project_fills_only_its_environment():
    """A process whose built-ins have no value yet (e.g. made before the default existed)."""
    scan = scan_projects("SELECT id FROM `value-gcp.raw.orders`", {}, defaults=PROD_DEFAULTS)
    assert scan.sql == "SELECT id FROM `${read_project}.raw.orders`"
    [v] = scan.variables
    assert (v.name, v.values.lab, v.values.value) == ("read_project", "", "value-gcp")


def test_lab_sql_claims_the_builtin_and_other_projects_keep_their_name_everywhere():
    sql = ("INSERT INTO `lab-gcp.mart.orders` SELECT o.id FROM `lab-gcp.raw.orders` o "
           "JOIN `shared-ref.reference.stores` s ON s.id = o.id")
    scan = scan_projects(sql, {}, env_values=NEW_PROCESS, defaults=PROD_DEFAULTS)
    assert scan.changed and scan.equivalence.ok
    assert scan.sql == ("INSERT INTO `${write_project}.mart.orders` SELECT o.id FROM `${write_project}.raw.orders` o "
                        "JOIN `${shared_ref_project}.reference.stores` s ON s.id = o.id")
    by_name = {v.name: v.values for v in scan.variables}
    # one project in prod (value-gcp) → one project in lab: both built-ins get lab-gcp; value stays value-gcp
    assert {n: (by_name[n].lab, by_name[n].value) for n in ("write_project", "read_project")} == {
        "write_project": ("lab-gcp", ""), "read_project": ("lab-gcp", "")}
    shared = by_name["shared_ref_project"]
    assert shared.lab == shared.value == shared.qa == "shared-ref"


def test_project_names_drop_lab_and_value():
    from app.services.sql_analyzer import project_variable_name

    assert project_variable_name("value-mkt") == "mkt_project"
    assert project_variable_name("lab-mkt-data") == "mkt_data_project"


def test_one_variable_never_stands_for_two_projects():
    """value-gcp (the prod default) and lab-gcp in the same SQL: each keeps its own placeholder, so the SQL stays provable."""
    sql = "INSERT INTO `lab-gcp.mart.t` SELECT id FROM `value-gcp.raw.s`"
    scan = scan_projects(sql, {"write_project": "lab-gcp"}, env_values=NEW_PROCESS, defaults=PROD_DEFAULTS)
    assert scan.changed and scan.equivalence.ok
    assert scan.sql == "INSERT INTO `${write_project}.mart.t` SELECT id FROM `${read_project}.raw.s`"


def test_analysis_places_projects_like_the_scan():
    analysis = SqlAnalyzer(FakeGemini(GOOD)).analyze(SQL.replace("acme-dev.raw_sales", "shared-ref.raw_sales"),
                                                     existing_variables=EMPTY, use_ai=False, env_values=NEW_PROCESS,
                                                     defaults=PROD_DEFAULTS)
    assert analysis.equivalence.ok
    shared = next(v for v in analysis.variables if v.name == "shared_ref_project")
    assert shared.values.lab == shared.values.value == "shared-ref"


def test_another_project_never_takes_over_a_builtin_that_is_value_ent_in_prod():
    """write_project is already lab-gcp (and both built-ins are value-gcp in prod): a read from another project must not
    become ${read_project}, or prod would read value-gcp instead."""
    sql = "INSERT INTO `lab-gcp.mart.t` SELECT id FROM `acme-ref-prod.reference.stores`"
    scan = scan_projects(sql, {"write_project": "lab-gcp"}, env_values=NEW_PROCESS, defaults=PROD_DEFAULTS)
    assert scan.sql == "INSERT INTO `${write_project}.mart.t` SELECT id FROM `${acme_ref_project}.reference.stores`"
    [v] = scan.variables
    assert v.values.lab == v.values.value == "acme-ref-prod"
