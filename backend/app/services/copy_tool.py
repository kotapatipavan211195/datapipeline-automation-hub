"""Copy files with the hub: the home page's "Copy files" tool, in three directions.

* **bucket → bucket**: copy or move. The copy happens inside Cloud Storage (an object rewrite), so no file passes
  through the hub. A move copies each file, then deletes exactly the source version it copied.
* **your computer → bucket** (upload): the browser sends the files to the hub, which writes them to the bucket.
* **bucket → your computer** (download): the hub reads the files and hands them to the browser; several files come as
  one ZIP that keeps their folders.

Everything runs as the hub's ADC identity, and which buckets it may touch depends on where the hub runs:

* on a laptop (auth_mode local) the identity is your own gcloud login: any bucket your account can reach;
* deployed (Cloud Run behind IAP, a service account) the hub stays lab-only: only the buckets listed in
  SF_COPY_TOOL_BUCKETS (names or patterns such as ``acme-*-dev``).

Every job is recorded in the workspace with the user who ran it.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from ..models.rules import BUCKET_RE
from ..settings import Settings
from .validators import ClientFactory, gcp_error_text

log = logging.getLogger("dahub.copy")
Operation = Literal["copy", "move", "upload", "download"]
ItemStatus = Literal["pending", "copied", "moved", "skipped", "failed"]  # copied = copied, uploaded or downloaded
JobStatus = Literal["running", "done", "failed", "cancelled"]
DONE_VERBS = {"copy": "copied", "move": "moved", "upload": "uploaded", "download": "downloaded"}
KEEP_SECONDS = 3600  # downloads and unsent uploads are kept this long on the hub, then removed


class CopyRefused(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def temp_area() -> Path:
    """The hub's own folder for uploads waiting to be sent and downloads waiting to be saved."""
    path = Path(tempfile.gettempdir()) / "dahub-copy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_temp_dir(kind: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=temp_area()))


def sweep_temp_area(max_age: float = KEEP_SECONDS) -> None:
    """Remove uploads and downloads older than max_age seconds (pages closed before sending, downloads not saved)."""
    cutoff = time.time() - max_age
    for path in temp_area().iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


# ====================================================================== which buckets
class CopyPolicy:
    def __init__(self, settings: Settings):
        self.enabled = settings.copy_tool_enabled
        self.scope = settings.copy_scope()
        self.patterns = settings.copy_buckets()

    def allows(self, bucket: str) -> bool:
        return self.scope == "any" or any(fnmatch.fnmatchcase(bucket, p) for p in self.patterns)

    def unavailable(self) -> str | None:
        if not self.enabled:
            return "the Copy files tool is turned off on this hub (SF_COPY_TOOL_ENABLED)"
        if self.scope == "allowlist" and not self.patterns:
            return ("no buckets are allowed yet: the hub admin lists the lab buckets in SF_COPY_TOOL_BUCKETS "
                    "(the deployed hub never copies outside them)")
        return None

    def describe(self, identity: str = "") -> str:
        if self.scope == "any":
            return f"Runs with your own Google credentials{f' ({identity})' if identity else ''}: any bucket your account can reach."
        return "Lab buckets only: " + (", ".join(self.patterns) or "none configured yet")


def folder(prefix: str) -> str:
    """'a/b' → 'a/b/', '' → '' (a folder inside a bucket)."""
    prefix = (prefix or "").strip().strip("/")
    return f"{prefix}/" if prefix else ""


# ====================================================================== the plan
@dataclass
class CopyItem:
    source: str  # object name in the source bucket, or the name of the file from your computer (upload)
    destination: str  # object name in the destination bucket, or the path inside the download (download)
    size: int = 0
    exists: bool = False  # already at the destination when the plan was made
    local_path: str = ""  # upload: where the hub keeps the file until it is sent
    status: ItemStatus = "pending"
    error: str = ""


@dataclass
class CopyPlan:
    operation: Operation
    source_bucket: str = ""
    source_prefix: str = ""
    destination_bucket: str = ""
    destination_prefix: str = ""
    pattern: str = "*"
    recursive: bool = False
    overwrite: bool = False
    items: list[CopyItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    staging_dir: str | None = None  # upload: the hub's copy of the files (removed when the job ends)

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def existing(self) -> int:
        return sum(1 for i in self.items if i.exists)

    def source_uri(self) -> str:
        return "your computer" if self.operation == "upload" else f"gs://{self.source_bucket}/{self.source_prefix}"

    def destination_uri(self) -> str:
        return "your computer" if self.operation == "download" else f"gs://{self.destination_bucket}/{self.destination_prefix}"

    def summary(self) -> dict[str, Any]:
        return {
            "operation": self.operation, "source": self.source_uri(), "destination": self.destination_uri(),
            "pattern": self.pattern, "recursive": self.recursive, "overwrite": self.overwrite, "files": len(self.items),
            "bytes": self.total_bytes, "existing": self.existing, "problems": self.problems,
        }


def _check_bucket(plan: CopyPlan, policy: CopyPolicy, bucket: str, role: str) -> None:
    if not bucket:
        plan.problems.append(f"choose the {role} bucket")
    elif not BUCKET_RE.match(bucket):
        plan.problems.append(f"{bucket!r} is not a valid bucket name")
    elif not policy.allows(bucket):
        plan.problems.append(f"gs://{bucket} is not one of the buckets this hub may copy {'from' if role == 'source' else 'to'} "
                             f"({', '.join(policy.patterns) or 'none configured'})")


def _source_items(client: Any, plan: CopyPlan, names: list[str] | None, max_files: int, destination_prefix: str) -> None:
    """The ticked files (names relative to the source folder), or every file matching the pattern."""
    wanted = {n.strip("/") for n in names or [] if n.strip("/")}
    try:
        for blob in client.list_blobs(plan.source_bucket, prefix=plan.source_prefix or None):
            rel = blob.name[len(plan.source_prefix):]
            if not rel or blob.name.endswith("/"):
                continue  # folder placeholders
            if wanted:
                if rel not in wanted:
                    continue
            elif ("/" in rel and not plan.recursive) or not fnmatch.fnmatchcase(PurePosixPath(rel).name, plan.pattern):
                continue
            if len(plan.items) >= max_files:
                plan.problems.append(f"more than {max_files:,} files match: narrow the selection or the pattern")
                return
            plan.items.append(CopyItem(source=blob.name, destination=destination_prefix + rel, size=blob.size or 0))
    except Exception as exc:  # noqa: BLE001
        plan.problems.append(f"could not list gs://{plan.source_bucket}/{plan.source_prefix}: {gcp_error_text(exc)}")
        return
    if not plan.items:
        plan.problems.append("no file matches: tick files, or change the pattern"
                             + ("" if plan.recursive else " (subfolders are not included)"))


def _mark_existing(client: Any, plan: CopyPlan) -> None:
    try:
        there = {b.name for b in client.list_blobs(plan.destination_bucket, prefix=plan.destination_prefix or None)}
    except Exception as exc:  # noqa: BLE001
        plan.problems.append(f"could not list gs://{plan.destination_bucket}/{plan.destination_prefix}: {gcp_error_text(exc)}")
        return
    for item in plan.items:
        item.exists = item.destination in there


def plan_copy(
    clients: ClientFactory,
    policy: CopyPolicy,
    *,
    project: str,
    operation: Literal["copy", "move"],
    source_bucket: str,
    source_prefix: str = "",
    names: list[str] | None = None,
    pattern: str = "*",
    recursive: bool = False,
    destination_bucket: str,
    destination_prefix: str = "",
    overwrite: bool = False,
    max_files: int = 5000,
) -> CopyPlan:
    """Bucket → bucket: every file to copy, where it lands, and whether it is already there. Nothing is changed."""
    plan = CopyPlan(operation=operation, source_bucket=(source_bucket or "").strip(), source_prefix=folder(source_prefix),
                    destination_bucket=(destination_bucket or "").strip(), destination_prefix=folder(destination_prefix),
                    pattern=(pattern or "*").strip() or "*", recursive=recursive, overwrite=overwrite)
    unavailable = policy.unavailable()
    if unavailable:
        plan.problems.append(unavailable)
        return plan
    _check_bucket(plan, policy, plan.source_bucket, "source")
    _check_bucket(plan, policy, plan.destination_bucket, "destination")
    if plan.source_bucket == plan.destination_bucket and plan.source_prefix == plan.destination_prefix:
        plan.problems.append("the destination folder is the source folder: pick another folder or bucket")
    elif (plan.source_bucket == plan.destination_bucket and recursive
          and plan.destination_prefix.startswith(plan.source_prefix)):
        plan.problems.append("the destination folder is inside the source folder, and subfolders are included")
    if plan.problems:
        return plan
    client = clients.storage(project)
    _source_items(client, plan, names, max_files, plan.destination_prefix)
    if not plan.problems:
        _mark_existing(client, plan)
    return plan


@dataclass
class LocalFile:
    """A file sent from the browser, kept by the hub until the upload job sends it."""

    name: str
    size: int
    path: str


def plan_upload(
    clients: ClientFactory,
    policy: CopyPolicy,
    *,
    project: str,
    files: list[LocalFile],
    destination_bucket: str,
    destination_prefix: str = "",
    overwrite: bool = False,
    staging_dir: str | None = None,
    max_files: int = 5000,
) -> CopyPlan:
    """Your computer → bucket: each file keeps its name under the destination folder."""
    plan = CopyPlan(operation="upload", destination_bucket=(destination_bucket or "").strip(),
                    destination_prefix=folder(destination_prefix), overwrite=overwrite, staging_dir=staging_dir)
    unavailable = policy.unavailable()
    if unavailable:
        plan.problems.append(unavailable)
        return plan
    _check_bucket(plan, policy, plan.destination_bucket, "destination")
    if not files:
        plan.problems.append("add the files to upload first")
    elif len(files) > max_files:
        plan.problems.append(f"more than {max_files:,} files: upload them in parts")
    names = [f.name for f in files]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        plan.problems.append(f"two files have the same name: {', '.join(dupes[:5])}")
    if plan.problems:
        return plan
    plan.items = [CopyItem(source=f.name, destination=plan.destination_prefix + f.name, size=f.size, local_path=f.path)
                  for f in files]
    _mark_existing(clients.storage(project), plan)
    return plan


def plan_download(
    clients: ClientFactory,
    policy: CopyPolicy,
    *,
    project: str,
    source_bucket: str,
    source_prefix: str = "",
    names: list[str] | None = None,
    pattern: str = "*",
    recursive: bool = False,
    max_files: int = 5000,
    max_bytes: int = 500 * 1024 * 1024,
) -> CopyPlan:
    """Bucket → your computer: the ticked files (or every file matching the pattern), with their path inside the ZIP."""
    plan = CopyPlan(operation="download", source_bucket=(source_bucket or "").strip(), source_prefix=folder(source_prefix),
                    pattern=(pattern or "*").strip() or "*", recursive=recursive)
    unavailable = policy.unavailable()
    if unavailable:
        plan.problems.append(unavailable)
        return plan
    _check_bucket(plan, policy, plan.source_bucket, "source")
    if plan.problems:
        return plan
    _source_items(clients.storage(project), plan, names, max_files, "")
    if not plan.problems and plan.total_bytes > max_bytes:
        plan.problems.append(f"{plan.total_bytes / 1024 / 1024:,.0f} MB is more than a download may hold "
                             f"({max_bytes / 1024 / 1024:,.0f} MB): narrow the selection")
    return plan


# ====================================================================== running a job
@dataclass
class CopyJob:
    id: str
    user: str
    identity: str  # the ADC identity that did the work
    plan: CopyPlan
    status: JobStatus = "running"
    started_at: str = ""
    ended_at: str | None = None
    message: str = ""
    download_path: str | None = None  # download: the file (or ZIP) waiting to be saved
    lines: deque = field(default_factory=lambda: deque(maxlen=2000))
    _cancel: bool = False
    _workdir: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def active(self) -> bool:
        return self.status == "running"

    @property
    def verb(self) -> str:
        return DONE_VERBS[self.plan.operation]

    def counts(self) -> dict[str, int]:
        out = {"pending": 0, "copied": 0, "moved": 0, "skipped": 0, "failed": 0}
        for item in self.plan.items:
            out[item.status] += 1
        return out

    def record(self) -> dict[str, Any]:
        """What the workspace keeps (the audit trail): who copied what, where, and how it went."""
        counts = self.counts()
        return {
            "id": self.id, "user": self.user, "identity": self.identity, "status": self.status, "message": self.message,
            "started_at": self.started_at, "ended_at": self.ended_at, **self.plan.summary(), "counts": counts,
            "bytes_done": sum(i.size for i in self.plan.items if i.status in ("copied", "moved")),
            "failures": [{"source": i.source, "error": i.error} for i in self.plan.items if i.status == "failed"][:50],
        }

    def snapshot(self) -> dict[str, Any]:
        out = self.record()
        out["lines"] = list(self.lines)
        out["download"] = Path(self.download_path).name if self.download_path else None
        return out

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)


class CopyJobManager:
    """Runs copy, upload and download jobs in background threads and records each finished job in the workspace."""

    def __init__(self, settings: Settings, clients: ClientFactory, store: Any = None):
        self.settings, self.clients, self.store = settings, clients, store
        self._jobs: dict[str, CopyJob] = {}
        self._lock = threading.Lock()

    def policy(self) -> CopyPolicy:
        return CopyPolicy(self.settings)

    def plan(self, *, project: str, **kwargs: Any) -> CopyPlan:
        return plan_copy(self.clients, self.policy(), project=project, max_files=self.settings.copy_tool_max_files, **kwargs)

    def plan_upload(self, *, project: str, **kwargs: Any) -> CopyPlan:
        return plan_upload(self.clients, self.policy(), project=project, max_files=self.settings.copy_tool_max_files, **kwargs)

    def plan_download(self, *, project: str, **kwargs: Any) -> CopyPlan:
        return plan_download(self.clients, self.policy(), project=project, max_files=self.settings.copy_tool_max_files,
                             max_bytes=self.settings.copy_tool_max_download_mb * 1024 * 1024, **kwargs)

    def get(self, job_id: str) -> CopyJob | None:
        return self._jobs.get(job_id)

    def recent(self, user: str, limit: int = 20) -> list[dict[str, Any]]:
        """The user's latest jobs: running ones from memory, finished ones from the workspace."""
        running = [j.record() for j in self._jobs.values() if j.user == user and j.active]
        stored = self.store.list_copy_jobs(user, limit=limit) if self.store is not None else []
        seen = {r["id"] for r in running}
        return (running + [r for r in stored if r["id"] not in seen])[:limit]

    def start(self, plan: CopyPlan, *, project: str, user: str, identity: str) -> CopyJob:
        if plan.problems:
            raise CopyRefused(plan.problems)
        with self._lock:
            mine = [j for j in self._jobs.values() if j.user == user and j.active]
        if mine:
            raise CopyRefused(["one of your copy jobs is still running: wait for it or cancel it"])
        sweep_temp_area()
        job = CopyJob(id=uuid.uuid4().hex[:12], user=user, identity=identity, plan=plan,
                      started_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
        with self._lock:
            self._jobs[job.id] = job
        log.info("copy job started", extra={"fields": {"job": job.id, "user": user, **plan.summary()}})
        threading.Thread(target=self._run, args=(job, project), name=f"copy-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str, user: str) -> CopyJob | None:
        job = self._jobs.get(job_id)
        if job and job.user == user and job.active:
            job._cancel = True
        return job

    # ------------------------------------------------------------------ internals
    def _run(self, job: CopyJob, project: str) -> None:
        plan = job.plan
        try:
            client = self.clients.storage(project)
            source = client.bucket(plan.source_bucket) if plan.source_bucket else None
            target = client.bucket(plan.destination_bucket) if plan.destination_bucket else None
            if plan.operation == "download":
                job._workdir = new_temp_dir("download")
            work = {"upload": self._upload_one, "download": self._download_one}.get(plan.operation, self._rewrite_one)
            with ThreadPoolExecutor(max_workers=max(1, self.settings.copy_tool_workers)) as pool:
                list(pool.map(lambda item: work(job, item, source, target), plan.items))
            if plan.operation == "download" and not job._cancel:
                self._package(job)
            counts = job.counts()
            done = counts["copied"] + counts["moved"]
            job.status = "cancelled" if job._cancel else ("failed" if counts["failed"] else "done")
            skipped = "" if plan.operation == "download" else f", {counts['skipped']} skipped (already there)"
            job.message = (f"{done} file(s) {job.verb}{skipped}, {counts['failed']} failed"
                           + (f", {counts['pending']} not started (cancelled)" if job._cancel else ""))
        except Exception as exc:  # noqa: BLE001
            job.status, job.message = "failed", gcp_error_text(exc)
            log.exception("copy job %s failed", job.id)
        finally:
            job.ended_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            job.log(job.message)
            if plan.staging_dir:
                shutil.rmtree(plan.staging_dir, ignore_errors=True)  # the uploaded files are in the bucket now
            if job._workdir is not None and job.download_path is None:
                shutil.rmtree(job._workdir, ignore_errors=True)
            log.info("copy job finished", extra={"fields": {"job": job.id, "user": job.user, "status": job.status,
                                                            "operation": plan.operation, **job.counts()}})
            if self.store is not None:
                try:
                    self.store.record_copy_job(job.record())
                except Exception:  # noqa: BLE001
                    log.exception("could not record copy job %s", job.id)

    def _rewrite_one(self, job: CopyJob, item: CopyItem, source: Any, target: Any) -> None:
        if job._cancel:
            return
        plan = job.plan
        src_uri = f"gs://{plan.source_bucket}/{item.source}"
        dst_uri = f"gs://{plan.destination_bucket}/{item.destination}"
        blob = source.get_blob(item.source)
        if blob is None:
            item.status, item.error = "failed", "the source file is gone"
            job.log(f"FAILED {src_uri}: the source file is gone")
            return
        destination = target.blob(item.destination)
        conditions = {"if_source_generation_match": blob.generation}
        if not plan.overwrite:
            conditions["if_generation_match"] = 0  # only if the destination does not exist yet
        try:
            token = None
            while True:  # large objects take several rewrite calls
                token, _done, _total = destination.rewrite(blob, token=token, **conditions)
                if token is None:
                    break
        except Exception as exc:  # noqa: BLE001
            if self._skip(job, item, exc, dst_uri):
                return
            item.status, item.error = "failed", gcp_error_text(exc)
            job.log(f"FAILED {src_uri} → {dst_uri}: {item.error}")
            return
        if plan.operation == "move":
            try:
                blob.delete(if_generation_match=blob.generation)  # exactly the version that was copied
            except Exception as exc:  # noqa: BLE001
                item.status, item.error = "failed", f"copied, but the source could not be deleted: {gcp_error_text(exc)}"
                job.log(f"FAILED to delete {src_uri} after copying it: {gcp_error_text(exc)}")
                return
            item.status = "moved"
            job.log(f"moved {src_uri} → {dst_uri}")
        else:
            item.status = "copied"
            job.log(f"copied {src_uri} → {dst_uri}")

    def _upload_one(self, job: CopyJob, item: CopyItem, _source: Any, target: Any) -> None:
        if job._cancel:
            return
        dst_uri = f"gs://{job.plan.destination_bucket}/{item.destination}"
        conditions = {} if job.plan.overwrite else {"if_generation_match": 0}
        try:
            target.blob(item.destination).upload_from_filename(item.local_path, **conditions)
        except Exception as exc:  # noqa: BLE001
            if self._skip(job, item, exc, dst_uri):
                return
            item.status, item.error = "failed", gcp_error_text(exc)
            job.log(f"FAILED {item.source} → {dst_uri}: {item.error}")
            return
        item.status = "copied"
        job.log(f"uploaded {item.source} → {dst_uri}")

    def _download_one(self, job: CopyJob, item: CopyItem, source: Any, _target: Any) -> None:
        if job._cancel:
            return
        src_uri = f"gs://{job.plan.source_bucket}/{item.source}"
        path = job._workdir / "files" / item.destination
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = source.get_blob(item.source)
            if blob is None:
                raise FileNotFoundError("the file is gone")
            blob.download_to_filename(str(path))
        except Exception as exc:  # noqa: BLE001
            item.status, item.error = "failed", gcp_error_text(exc)
            job.log(f"FAILED {src_uri}: {item.error}")
            return
        item.status = "copied"
        job.log(f"downloaded {src_uri}")

    def _package(self, job: CopyJob) -> None:
        """One file is handed over as it is; several go into one ZIP that keeps their folders."""
        done = [i for i in job.plan.items if i.status == "copied"]
        files = job._workdir / "files"
        if len(done) == 1 and len(job.plan.items) == 1:
            job.download_path = str(files / done[0].destination)
        elif done:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = job._workdir / f"{job.plan.source_bucket}_{stamp}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for item in done:
                    zf.write(files / item.destination, item.destination)
                    (files / item.destination).unlink()  # on Cloud Run the disk is memory: keep one copy only
            job.download_path = str(archive)
            job.log(f"packed {len(done)} file(s) into {archive.name}")

    @staticmethod
    def _skip(job: CopyJob, item: CopyItem, exc: Exception, dst_uri: str) -> bool:
        """A destination that already exists is skipped (unless overwriting)."""
        if not job.plan.overwrite and (getattr(exc, "code", None) == 412 or type(exc).__name__ == "PreconditionFailed"):
            item.status = "skipped"
            job.log(f"skipped {dst_uri}: already there")
            return True
        return False
