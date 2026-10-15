"""Lab test runs: execute a generated script for real in the **lab** environment, from the hub.

The hub never runs (or even reads) the value/prod environment. A test run:

* renders the bundle and writes it to a private temp folder with a **lab-only** YAML (``gcp_projects`` holds just
  the lab block) whose email receivers are replaced by the tester's own address;
* puts the team modules next to it (the real ones from SF_MODULES_DIR, else the hub's copies — on a pipeline server
  ``/data/...`` comes first on sys.path anyway);
* runs ``python ./<script>.py -cf ./<script>.yaml [-ts …] lab`` with the hub's ADC credentials, streams its log and
  records each step's ``execution end`` time, the exit code and the duration;
* refuses to start when a lab write location equals the value/qa one (it would write where prod writes).
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..models.spec import CopyStep, CustomStep, ProcessSpec
from ..settings import TEST_RUN_ENVIRONMENT, Settings
from .lineage import process_tables
from .renderer import RenderError, build_config_dict, render_config_yaml, render_process
from .validators import prod_collisions

Status = Literal["running", "passed", "failed", "timeout", "cancelled", "error"]
END_RE = re.compile(r"Function (\w+) execution end:\s+([\d.]+) seconds")
ACTIVE: tuple[str, ...] = ("running",)


class TestRunRefused(Exception):
    __test__ = False  # not a pytest test class

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class TestRun:
    __test__ = False  # not a pytest test class

    id: str
    owner: str
    name: str
    started_by: str
    adc_email: str
    spec_hash: str
    script: str
    command: str
    status: Status = "running"
    exit_code: int | None = None
    started_at: str = ""
    ended_at: str | None = None
    duration: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    message: str = ""
    lines: deque = field(default_factory=lambda: deque(maxlen=5000))
    line_count: int = 0
    _proc: Any = None
    _cancel: bool = False
    _timed_out: bool = False
    _started: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    def record(self) -> dict[str, Any]:
        """JSON summary stored with a saved version (without the log)."""
        return {
            "id": self.id, "environment": TEST_RUN_ENVIRONMENT, "status": self.status, "exit_code": self.exit_code,
            "started_by": self.started_by, "adc_email": self.adc_email, "spec_hash": self.spec_hash,
            "started_at": self.started_at, "ended_at": self.ended_at, "duration": round(self.duration, 2),
            "timings": self.timings, "steps": self.steps, "command": self.command, "message": self.message,
        }

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        lines = list(self.lines)
        first = self.line_count - len(lines)  # index of lines[0] in the whole log
        start = max(since - first, 0)
        out = self.record()
        out.update(lines=lines[start:], next=self.line_count, side_effects=self.side_effects,
                   elapsed=round(time.monotonic() - self._started, 1) if self.active else round(self.duration, 1))
        return out

    def log_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def log_name(self) -> str:
        """File name of the downloadable log; it sits next to the script in a ZIP."""
        return f"{self.script}_lab_test_run.log"

    def log_file(self) -> str:
        """The log as a file: a summary of the run, then every line the script printed (the newest ones are kept)."""
        head = [
            f"# Lab test run of {self.script}.py ({self.id})",
            f"# status:      {self.status}" + (f" (exit code {self.exit_code})" if self.exit_code is not None else ""),
            f"# started:     {self.started_at} UTC by {self.started_by}",
            f"# ended:       {self.ended_at or 'still running'}" + (f" UTC, {self.duration:.1f} s" if self.ended_at else ""),
            f"# command:     {self.command}",
            f"# identity:    {self.adc_email or 'ADC'} (lab values only)",
            f"# spec:        {self.spec_hash}",
        ]
        if self.message:
            head.append(f"# result:      {self.message}")
        for fn in self.steps:
            seconds = self.timings.get(fn)
            head.append(f"# step:        {fn}: " + (f"{seconds:.2f} s" if seconds is not None else "did not finish"))
        dropped = self.line_count - len(self.lines)
        if dropped > 0:
            head.append(f"# note:        the first {dropped} line(s) are not kept (the hub keeps the last {len(self.lines)})")
        return "\n".join(head) + "\n#\n" + self.log_text()


def side_effects(spec: ProcessSpec, adc_email: str) -> list[str]:
    """What a lab test run will change, in words (for the confirm dialog)."""
    inv = process_tables(spec, TEST_RUN_ENVIRONMENT)
    out = [f"BigQuery: {', '.join(t.how) or 'writes'} {t.resolved}" for t in inv.targets]
    for f in inv.gcs_outputs:
        out.append(f"Files: {f.role} → {f.uri}")
    for step in spec.enabled_steps():
        if isinstance(step, CopyStep) and step.operation == "mv":
            out.append(f"Files: {step.function_name} moves (deletes) the source files")
        if isinstance(step, CustomStep):
            out.append(f"Custom code: {step.function_name} runs for real in lab (the hub cannot list what it changes)")
    if spec.email.enabled:
        out.append(f"Email: sent only to {adc_email} (the lab receivers are replaced for test runs)")
    return out


_IMPORTABLE: dict[tuple[str, str], bool] = {}  # (another interpreter, module) → importable


def custom_modules(step: CustomStep) -> list[str]:
    """Top-level modules a custom step imports that are not in the standard library."""
    tops = [line.split()[1].split(".")[0] for line in step.imports if len(line.split()) > 1]
    return [m for m in dict.fromkeys(tops) if m not in sys.stdlib_module_names]


def importable(python: str, module: str) -> bool:
    """Can that Python import the module? The hub's own interpreter is asked directly (so a fresh pip install counts);
    another one (SF_TEST_RUN_PYTHON) once per module."""
    if python == sys.executable:
        importlib.invalidate_caches()
        return importlib.util.find_spec(module) is not None
    key = (python, module)
    if key not in _IMPORTABLE:
        check = f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({module!r}) else 1)"
        try:
            _IMPORTABLE[key] = subprocess.run([python, "-c", check], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _IMPORTABLE[key] = False
    return _IMPORTABLE[key]


class TestRunManager:
    __test__ = False  # not a pytest test class

    def __init__(self, settings: Settings):
        self.settings = settings
        self._runs: dict[str, TestRun] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ queries
    def get(self, run_id: str) -> TestRun | None:
        return self._runs.get(run_id)

    def latest_for(self, owner: str, name: str) -> TestRun | None:
        runs = [r for r in self._runs.values() if (r.owner, r.name) == (owner, name)]
        return max(runs, key=lambda r: r.started_at) if runs else None

    def unavailable_reasons(self, spec: ProcessSpec, *, adc_email: str | None) -> list[str]:
        """Why this process cannot be test-run at all (empty = it can). Unlike a busy hub, these do not go away by
        waiting, so the wizard lets such a process generate without a lab test (marked "not tested in lab").
        adc_email None = not looked up yet (the email check is skipped)."""
        problems = []
        if not self.settings.test_run_enabled:
            problems.append("lab test runs are turned off on this hub (SF_TEST_RUN_ENABLED)")
        problems += [f"lab and prod share a write location: {p}" for p in prod_collisions(spec)]
        python = self.settings.test_run_python or sys.executable
        for step in spec.enabled_steps():
            if isinstance(step, CustomStep):
                missing = [m for m in custom_modules(step) if not importable(python, m)]
                if missing:
                    need = " ".join(step.requirements) or " ".join(missing)
                    problems.append(f"the custom step {step.function_name} needs package(s) the hub's Python does not have: "
                                    f"{', '.join(missing)} (pip install {need}, or test it on the pipeline server)")
        if spec.email.enabled and adc_email is not None and "@" not in adc_email:
            problems.append("the hub's ADC identity has no email address, so the test email has nowhere to go: "
                            "run the hub with your own credentials or turn the email off")
        return problems

    def passed_for(self, owner: str, name: str, spec_hash: str) -> TestRun | None:
        """The latest test run of this process, if it passed with exactly this spec."""
        latest = self.latest_for(owner, name)
        if latest is not None and latest.status == "passed" and latest.spec_hash == spec_hash:
            return latest
        return None

    def check(self, spec: ProcessSpec, *, owner: str, name: str, adc_email: str) -> list[str]:
        """Reasons a test run cannot start (empty = it can)."""
        problems = self.unavailable_reasons(spec, adc_email=adc_email)
        with self._lock:
            mine = [r for r in self._runs.values() if r.active and (r.owner, r.name) == (owner, name)]
            running = [r for r in self._runs.values() if r.active]
        if mine:
            problems.append("a test run of this process is already running")
        elif len(running) >= self.settings.test_run_max_concurrent:
            problems.append(f"{len(running)} test runs are already running on this hub; try again in a few minutes")
        return problems

    # ------------------------------------------------------------------ start / cancel
    def start(self, spec: ProcessSpec, *, owner: str, name: str, user: str, adc_email: str) -> TestRun:
        problems = self.check(spec, owner=owner, name=name, adc_email=adc_email)
        if problems:
            raise TestRunRefused(problems)
        try:
            result = render_process(spec, smoke_run=False, adc_email=adc_email, settings=self.settings)
        except RenderError as exc:
            raise TestRunRefused([f"the code does not generate: {p}" for p in exc.problems]) from exc
        stem = result.script
        workdir = Path(tempfile.mkdtemp(prefix="dahub-testrun-"))
        for rel, content in result.files.items():
            if rel.endswith(".py") or rel == result.schema_file:
                (workdir / rel).write_text(content, encoding="utf-8")
        receivers = [adc_email] if spec.email.enabled else None
        lab_config = build_config_dict(spec, sender=adc_email, envs=[TEST_RUN_ENVIRONMENT], receivers=receivers)
        (workdir / f"{stem}.yaml").write_text(render_config_yaml(lab_config, [
            f"LAB TEST RUN of {stem}.py started from the Datapipeline Automation Hub by {user}.",
            "Only the lab block is present; email receivers are replaced by the tester's address.",
        ]), encoding="utf-8")
        shutil.copytree(self.settings.resolved_modules_dir(), workdir / "modules",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        args = [f"./{stem}.py", "-cf", f"./{stem}.yaml"]  # the environment comes last, as the team runs it
        if result.schema_file:
            args += ["-ts", f"./{result.schema_file}"]
        args.append(TEST_RUN_ENVIRONMENT)
        run = TestRun(
            id=uuid.uuid4().hex[:12], owner=owner, name=name, started_by=user, adc_email=adc_email,
            spec_hash=spec.spec_hash(), script=stem, command="python " + " ".join(f'"{a}"' if " " in a else a for a in args),
            started_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            steps=[s.function_name for s in spec.enabled_steps()], side_effects=side_effects(spec, adc_email),
        )
        run.lines = deque(maxlen=self.settings.test_run_max_log_lines)
        env = dict(os.environ)
        env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1",
                   DAG_ID="datapipeline-hub-test-run", TASK_ID=run.id)
        python = self.settings.test_run_python or sys.executable
        try:
            run._proc = _spawn(
                [python, "-u", *args], cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            raise TestRunRefused([f"could not start Python ({python}): {exc}"]) from exc
        run._started = time.monotonic()
        with self._lock:
            self._runs[run.id] = run
        threading.Thread(target=self._follow, args=(run, workdir), name=f"testrun-{run.id}", daemon=True).start()
        return run

    def cancel(self, run_id: str) -> TestRun | None:
        run = self._runs.get(run_id)
        if run and run.active and run._proc is not None:
            run._cancel = True
            _stop(run._proc)
        return run

    # ------------------------------------------------------------------ internals
    def _follow(self, run: TestRun, workdir: Path) -> None:
        proc = run._proc

        def on_timeout() -> None:
            run._timed_out = True
            _stop(proc)

        timer = threading.Timer(self.settings.test_run_timeout_seconds, on_timeout)
        timer.daemon = True
        timer.start()
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                run.lines.append(line)
                run.line_count += 1
                m = END_RE.search(line)
                if m and m.group(1) != "main":
                    run.timings[m.group(1)] = float(m.group(2))
            proc.wait()
        finally:
            timer.cancel()
            run.duration = time.monotonic() - run._started
            run.ended_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            run.exit_code = proc.returncode
            if run._cancel:
                run.status, run.message = "cancelled", "Cancelled by the user"
            elif run._timed_out:
                run.status, run.message = "timeout", f"Stopped after {self.settings.test_run_timeout_seconds} s"
            elif proc.returncode == 0:
                missing = [s for s in run.steps if s not in run.timings]
                run.status = "passed" if not missing else "failed"
                run.message = "Every step finished in lab" if not missing else f"Exited 0 but did not finish: {', '.join(missing)}"
            else:
                last = next((ln for ln in reversed(run.lines) if ln.strip()), "")
                run.status, run.message = "failed", f"Exit code {proc.returncode}: {last[:300]}"
            shutil.rmtree(workdir, ignore_errors=True)


def _spawn(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """Start the script (a seam for tests)."""
    return subprocess.Popen(cmd, **kwargs)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
