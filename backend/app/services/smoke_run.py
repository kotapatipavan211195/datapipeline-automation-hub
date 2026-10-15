"""Offline smoke run: execute a freshly generated script end to end against fake Google clients.

The script runs exactly as an orchestrator would call it (``python <script>.py lab -cf <script>.yaml [-ts …]``)
next to the team modules (vendored copies), with ``app/sandbox/fake_google`` first on PYTHONPATH. That proves the
imports, the YAML keys the script reads, argument parsing, the call order and the pandas typing of the ingestion
— without credentials and without touching GCP. The lab test run (test_runner.py) is the real thing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..models.rules import fill_variables
from ..models.schema import Column
from ..models.spec import FILE_EXTENSIONS, IngestStep, ProcessSpec
from ..settings import Settings, get_settings

SANDBOX_DIR = Path(__file__).resolve().parents[1] / "sandbox" / "fake_google"
END_RE = r"Function {fn} execution end"

SAMPLE_VALUES = {
    "STRING": ["alpha", "beta"],
    "INTEGER": [1, 2],
    "FLOAT": [1.5, 2.5],
    "NUMERIC": ["1.25", "2.50"],
    "BIGNUMERIC": ["1.25", "2.50"],
    "BOOLEAN": [True, False],
    "DATE": ["2026-01-15", "2026-01-16"],
    "DATETIME": ["2026-01-15 10:30:00", "2026-01-16 11:00:00"],
    "TIMESTAMP": ["2026-01-15 10:30:00", "2026-01-16 11:00:00"],
    "TIME": ["10:30:00", "11:00:00"],
}


def _check(name: str, ok: bool, detail: str = ""):
    from .renderer import Check

    return Check(name, ok, detail)


def sample_rows(columns: list[Column]) -> list[dict[str, object]]:
    return [{c.name: SAMPLE_VALUES.get(c.type, ["x", "y"])[i] for c in columns} for i in range(2)]


def write_sample_file(step: IngestStep, folder: Path, name: str) -> Path:
    """A small file in the step's format that matches its schema, header and skiprows settings."""
    rows = sample_rows(step.columns)
    path = folder / name

    def text(v: object) -> str:
        return "true" if v is True else "false" if v is False else str(v)

    if step.file_format == "XLSX":
        write_sample_xlsx(step, rows, path)
    elif step.file_format in ("CSV", "TXT", "FIXED"):
        if step.file_format == "FIXED":
            def line(values: list[str]) -> str:  # each value padded to its column; one that does not fit stays blank
                return "".join((v if len(v) <= (c.width or 0) else "").ljust(c.width or 0)
                               for c, v in zip(step.columns, values))
        else:
            def line(values: list[str]) -> str:
                return step.separator.join(values)
        data = [line([c.name for c in step.columns])] if step.header.strip().lower() != "none" else []
        try:
            header_row = int(step.header) if step.header.strip().lower() != "none" else 0
        except ValueError:
            header_row = 0
        lines = ["junk header line"] * header_row + data + [line([text(r[c.name]) for c in step.columns]) for r in rows]
        skip = step.skiprows.strip()
        if skip.lower() != "none":
            positions = json.loads(skip) if skip.startswith("[") else list(range(int(skip)))
            for pos in sorted(positions):
                lines.insert(pos, "junk line skipped by skiprows")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif step.file_format == "NEWLINE_DELIMITED_JSON":
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    else:  # PARQUET
        import pandas as pd

        pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _read_log(path: Path) -> list[dict]:
    """The calls the fake clients recorded (one JSON object per line)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def write_sample_xlsx(step: IngestStep, rows: list[dict[str, object]], path: Path) -> None:
    """An Excel sample with typed cells, laid out for the step's header and skiprows settings."""
    from openpyxl import Workbook

    cells: list[list[object]] = [[c.name for c in step.columns]] if step.header.strip().lower() != "none" else []
    try:
        header_row = int(step.header) if step.header.strip().lower() != "none" else 0
    except ValueError:
        header_row = 0
    cells = [["junk header row"]] * header_row + cells + [[r[c.name] for c in step.columns] for r in rows]
    skip = step.skiprows.strip()
    if skip.lower() != "none":
        positions = json.loads(skip) if skip.startswith("[") else list(range(int(skip)))
        for pos in sorted(positions):
            cells.insert(pos, ["junk row skipped by skiprows"])
    book = Workbook()
    sheet = book.active
    sheet.title = step.sheet_name or "Sheet1"
    for row in cells:
        sheet.append(row)
    book.save(path)


def prepare_sandbox(root: Path, files: dict[str, str], modules_dir: Path, overrides: dict[str, str] | None = None) -> None:
    """The bundle's .py/.yaml files (``overrides`` replace some: the script without its custom code) + team modules."""
    for rel, content in {**files, **(overrides or {})}.items():
        if rel.endswith((".py", ".yaml")):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    shutil.copytree(modules_dir, root / "modules", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def run_smoke(result, spec: ProcessSpec, *, settings: Settings | None = None, timeout: int = 180):
    """Run the generated script offline; returns a renderer Check."""
    settings = settings or get_settings()
    stem = result.script
    with tempfile.TemporaryDirectory(prefix="dahub-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        prepare_sandbox(root, result.files, settings.resolved_modules_dir(), result.smoke_files)
        manifest: dict[str, dict[str, str]] = {}
        ingest = spec.ingest_step()
        if ingest is not None:
            values = spec.variable_values("lab")
            bucket = fill_variables(ingest.read_bucket, values)
            folder = fill_variables(ingest.read_path, values).strip("/")
            samples = root / "__smoke_files__"
            samples.mkdir()
            local = write_sample_file(ingest, samples, "sample" + FILE_EXTENSIONS[ingest.file_format])
            # two matching files: the script must load every file of a run, one after the other
            manifest[bucket] = {(f"{folder}/" if folder else "") + f"{fill_variables(ingest.filename, values)}{day}"
                                f"{FILE_EXTENSIONS[ingest.file_format]}": str(local) for day in ("20260115", "20260116")}
        (root / "__smoke_manifest__.json").write_text(json.dumps(manifest), encoding="utf-8")
        log_path = root / "__smoke_log__.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_APPLICATION_CREDENTIALS", "PYTHON"))}
        env.update(
            PYTHONPATH=str(SANDBOX_DIR),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8",
            PYTHONUTF8="1",
            DAHUB_SMOKE_LOG=str(log_path),
            DAHUB_SMOKE_MANIFEST=str(root / "__smoke_manifest__.json"),
        )
        cmd = [sys.executable, "-u", f"./{stem}.py", "-cf", f"./{stem}.yaml"]  # the environment comes last, as the team runs it
        if result.schema_file:
            cmd += ["-ts", f"./{result.schema_file}"]
        cmd.append("lab")
        started = time.perf_counter()
        try:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return _check("offline smoke run", False, f"timed out after {timeout}s")
        seconds = time.perf_counter() - started
        output = (proc.stdout or "") + (proc.stderr or "")
        records = _read_log(log_path)
        boot = next((r for r in records if r.get("kind") == "boot"), None)
        fake = (boot or {}).get("google") or ""
        if not fake or not Path(fake).resolve().is_relative_to(SANDBOX_DIR):
            where = f"google came from {fake}" if fake else "its start-up hook (sitecustomize.py) did not run"
            tail = "\n".join(output.strip().splitlines()[-10:])
            return _check("offline smoke run", False,
                          f"could not load the fake Google clients ({where}): this is a problem of the hub's Python "
                          f"environment ({sys.executable}), not of the script\n{tail}")
        missing = [s.function_name for s in spec.enabled_steps()
                   if not re.search(END_RE.format(fn=re.escape(s.function_name)), output)]
        if proc.returncode != 0 or missing:
            tail = "\n".join(output.strip().splitlines()[-25:])
            why = f"exit code {proc.returncode}" if proc.returncode else f"step(s) did not finish: {', '.join(missing)}"
            return _check("offline smoke run", False, f"{why}\n{tail}")
        calls = sum(1 for r in records if r.get("kind") != "boot")
        n = len(spec.enabled_steps())
        custom = [s.function_name for s in spec.enabled_steps() if s.type == "custom"]
        note = f"; the custom code of {', '.join(custom)} is not run offline (the lab test run runs it)" if custom else ""
        return _check("offline smoke run", True,
                      f"{n} step(s) ran end to end against fake Google clients ({calls} calls) in {seconds:.1f}s{note}")
