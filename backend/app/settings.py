"""Service settings (environment variables prefixed SF_, or a .env file next to the backend)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORED_MODULES_DIR = Path(__file__).resolve().parent / "vendor" / "modules"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SF_", env_file=".env", extra="ignore")

    # GCP / Gemini (Vertex AI through google-genai, Application Default Credentials = the service account)
    gcp_project: str | None = None
    vertex_location: str = "us-central1"
    gemini_enabled: bool = True
    gemini_model: str = "gemini-2.5-pro"  # set to the newest Gemini model available in your Vertex region
    gemini_fast_model: str = "gemini-2.5-flash"
    gemini_rpm: int = 30
    gemini_max_retries: int = 3
    prompt_version: str = "2026-10-13.3"

    # Auth: "iap" in production (Cloud Run behind IAP), "local" for laptops/tests
    auth_mode: Literal["iap", "local"] = "local"
    iap_audience: str | None = None  # /projects/<number>/locations/<region>/services/<service> or the backend-service audience
    allowed_group: str | None = None  # optional second check via Cloud Identity Groups API
    dev_user_email: str = "developer@localhost"

    # Workspace: GCS bucket in production, local folder otherwise
    workspace_bucket: str | None = None
    local_workspace_dir: Path = REPO_ROOT / "output" / "workspace"

    # Validation / generation
    smoke_run_enabled: bool = True  # Generate runs the script offline (fake Google clients) before handing it out
    gcp_checks_enabled: bool = True  # BigQuery dry-runs, GCS path checks (lab values only)
    adc_lookup_enabled: bool = True  # find the ADC identity's email (sender, headers); off in tests
    max_upload_mb: int = 50
    max_sql_file_mb: int = 5  # attached .sql/.txt files on SQL steps
    enhance_max_script_kb: int = 1024  # "Enhance a script": largest script accepted
    sample_read_bytes: int = 2 * 1024 * 1024

    # ---- Generated-code standards (the same for every process; not shown in the UI)
    environments: list[str] = Field(default_factory=lambda: ["lab", "value", "qa"])  # argparse choices
    # read_project / write_project prefilled per environment in new processes ('' = none). Pasted SQL that names one
    # of these projects fills that environment's value; any other project in SQL counts as lab.
    default_project_lab: str = ""
    default_project_value: str = "value-gcp"
    default_project_qa: str = ""
    module_paths: list[str] = Field(default_factory=lambda: ["/data/py_scripts", "/data/data_eng/py_scripts"])
    bq_location: str = "US"  # extract jobs (location="US")

    # ---- Audit table (SF_SQL_LOGGER_*): every SQL a generated script runs is logged by the team's sql_logger module
    # (sql_logger.CaptureSQL) into <the credentials' project>.<sql_logger_table>: the SQL, success/failure, the BigQuery
    # job id, the environment and the Composer DAG_ID / TASK_ID. The module itself names the table; keep this value the
    # same, so "Validate all" checks the right table (in lab: your gcloud project).
    sql_logger_enabled: bool = True  # SF_SQL_LOGGER_ENABLED: false = generated scripts do not use sql_logger
    sql_logger_table: str = "pipeline_audit.pipeline_sql_log"  # SF_SQL_LOGGER_TABLE: dataset.table of the audit log

    # ---- Notification email (SF_SMTP_*, SF_EMAIL_SIGNATURE): written into every generated script that sends email;
    # the wizard does not show the relay
    smtp_host: str = "smtp.example.com"  # SF_SMTP_HOST: internal mail relay behind the VPN (no login)
    smtp_port: int = 25  # SF_SMTP_PORT
    smtp_starttls: bool = True  # SF_SMTP_STARTTLS
    email_signature: str = "Regards,\nThe Data Engineering Team\n\nThis is an automated email message."  # SF_EMAIL_SIGNATURE

    # ---- Lab test runs (the hub never reads, dry-runs or runs the value/prod environment)
    test_run_enabled: bool = True
    test_run_timeout_seconds: int = 1800
    test_run_max_concurrent: int = 2
    test_run_max_log_lines: int = 5000
    test_run_python: str | None = None  # interpreter for test runs; None = the hub's own Python
    modules_dir: Path | None = None  # the team's real modules/ folder; None = the vendored copies in app/vendor/modules

    # ---- Copy files tool (home page): copy / move files between buckets from the browser, as the hub's ADC identity
    copy_tool_enabled: bool = True
    # auto: a hub on a laptop (auth_mode local) may use any bucket your own credentials reach; a deployed hub (iap) only
    # the buckets in copy_tool_buckets. "any" / "allowlist" force one or the other.
    copy_tool_scope: Literal["auto", "any", "allowlist"] = "auto"
    copy_tool_buckets: str = ""  # comma-separated lab bucket names or patterns, e.g. "acme-*-dev,demo_file_test_bucket"
    copy_tool_max_files: int = 5000  # largest number of files one copy job takes
    copy_tool_workers: int = 8  # files copied at the same time
    copy_tool_max_upload_mb: int = 30  # per file from your computer (Cloud Run caps a request at 32 MiB; raise it locally)
    copy_tool_max_download_mb: int = 500  # per download to your computer (one file or one ZIP; Cloud Run's disk is memory)

    # ---- Find files (home page): search buckets for a file name or pattern at any depth (same buckets as Copy files)
    file_search_enabled: bool = True
    file_search_max_results: int = 1000  # matches shown; the search stops there
    file_search_max_scan: int = 500_000  # files looked at per bucket (or folder) before it stops

    # Web UI (NiceGUI, served from the same app)
    ui_enabled: bool = True
    ui_storage_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    # Logging
    log_format: Literal["json", "text"] = "json"
    log_level: str = "INFO"

    def copy_scope(self) -> str:
        """'any' (IAM decides) or 'allowlist' (only copy_tool_buckets) for the Copy files tool."""
        if self.copy_tool_scope != "auto":
            return self.copy_tool_scope
        return "any" if self.auth_mode == "local" else "allowlist"

    def default_projects(self) -> dict[str, str]:
        """{environment: project} of the read/write project defaults that are set."""
        pairs = (("lab", self.default_project_lab), ("value", self.default_project_value), ("qa", self.default_project_qa))
        return {env: project.strip() for env, project in pairs if project.strip()}

    def copy_buckets(self) -> list[str]:
        return [b.strip() for b in self.copy_tool_buckets.split(",") if b.strip()]

    def resolved_modules_dir(self) -> Path:
        """The modules/ folder test runs and smoke runs put next to the script."""
        return self.modules_dir if self.modules_dir else VENDORED_MODULES_DIR


# The only environment the hub ever executes (test runs) or checks (dry runs).
TEST_RUN_ENVIRONMENT = "lab"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
