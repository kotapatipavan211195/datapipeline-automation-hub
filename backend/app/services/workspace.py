"""Shared, versioned workspace: GCS bucket in production, a local folder on laptops/tests.

Layout (same for both backends):

    users/<owner>/<process>/meta.json                    owner, co-owners, version history
    users/<owner>/<process>/drafts/<user>.json           autosaved wizard state (owners/co-owners only)
    users/<owner>/<process>/versions/v0001/spec.json     immutable saved versions
    users/<owner>/<process>/versions/v0001/validation.json
    users/<owner>/<process>/versions/v0001/files/...     the generated bundle
    users/<owner>/<process>/versions/v0001/test_run.log  the lab test run of exactly that spec (if there was one)
    index/<owner>__<process>.json                        small summary used for listing
    copy_jobs/<user>/<UTC start>_<id>.json               the Copy files tool's audit trail

Everyone in the group can read and clone everything; only the owner and co-owners can save versions, and only the
owner can delete a process.
Concurrent saves are serialised with generation preconditions (if_generation_match), so nobody silently
overwrites someone else's version.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..models.rules import PROCESS_NAME_RE

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+$")


class WorkspaceError(Exception):
    status = 400


class NotFoundError(WorkspaceError):
    status = 404


class ConflictError(WorkspaceError):
    status = 409


class PermissionDenied(WorkspaceError):
    status = 403


# ====================================================================== backends
class Backend(Protocol):
    kind: str

    def read(self, path: str) -> tuple[bytes, int] | None: ...
    def write(self, path: str, data: bytes, *, if_generation_match: int | None = None, content_type: str = "application/json") -> int: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, path: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...


class LocalBackend:
    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if self.root.resolve() not in full.parents and full != self.root.resolve():
            raise WorkspaceError("invalid path")
        return full

    def read(self, path: str) -> tuple[bytes, int] | None:
        p = self._path(path)
        if not p.is_file():
            return None
        return p.read_bytes(), p.stat().st_mtime_ns

    def write(self, path: str, data: bytes, *, if_generation_match: int | None = None, content_type: str = "application/json") -> int:
        p = self._path(path)
        with self._lock:
            current = p.stat().st_mtime_ns if p.is_file() else 0
            if if_generation_match is not None and current != if_generation_match:
                raise ConflictError(f"{path} was changed by someone else")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            gen = p.stat().st_mtime_ns
            bump = 0
            # Rewritten within the same clock tick (coarse on Windows): force a newer generation. Re-read the stamp,
            # because the filesystem may round it (NTFS keeps 100 ns).
            while gen <= current:
                bump += 1_000
                os.utime(p, ns=(current + bump, current + bump))
                gen = p.stat().st_mtime_ns
            return gen

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix) if prefix else self.root
        folder = base if base.is_dir() else base.parent
        if not folder.exists():
            return []
        out = []
        for f in folder.rglob("*"):
            if f.is_file():
                rel = f.relative_to(self.root).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def delete(self, path: str) -> None:
        p = self._path(path)
        if p.is_file():
            p.unlink()

    def delete_prefix(self, prefix: str) -> int:
        """Delete every file under a folder (and the folder). Returns the number of files deleted."""
        folder = self._path(prefix.rstrip("/"))
        if folder == self.root.resolve() or not folder.is_dir():
            return 0
        with self._lock:
            count = sum(1 for f in folder.rglob("*") if f.is_file())
            shutil.rmtree(folder)
        return count


class GcsBackend:
    kind = "gcs"

    def __init__(self, bucket: str, client: Any = None):
        from google.cloud import storage

        self.client = client or storage.Client()
        self.bucket = self.client.bucket(bucket)
        self.bucket_name = bucket

    def read(self, path: str) -> tuple[bytes, int] | None:
        blob = self.bucket.get_blob(path)
        if blob is None:
            return None
        return blob.download_as_bytes(if_generation_match=blob.generation), int(blob.generation)

    def write(self, path: str, data: bytes, *, if_generation_match: int | None = None, content_type: str = "application/json") -> int:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(path)
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=if_generation_match)
        except PreconditionFailed as exc:
            raise ConflictError(f"{path} was changed by someone else") from exc
        return int(blob.generation or 0)

    def list(self, prefix: str) -> list[str]:
        return [b.name for b in self.client.list_blobs(self.bucket_name, prefix=prefix)]

    def delete(self, path: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self.bucket.blob(path).delete()
        except NotFound:
            pass

    def delete_prefix(self, prefix: str) -> int:
        if not prefix.strip("/"):
            return 0
        names = self.list(prefix.rstrip("/") + "/")
        for name in names:
            self.delete(name)
        return len(names)


# ====================================================================== models
class VersionInfo(BaseModel):
    version: int
    saved_at: str
    saved_by: str
    spec_hash: str
    note: str = ""
    checks_ok: bool = True
    lab_test: str | None = None  # status of the lab test run of exactly this spec (passed, failed, …), if any


class ProcessMeta(BaseModel):
    name: str
    owner: str
    co_owners: list[str] = Field(default_factory=list)
    description: str = ""
    team: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: str
    created_by: str
    updated_at: str
    latest_version: int = 0
    versions: list[VersionInfo] = Field(default_factory=list)
    cloned_from: str | None = None


class ProcessSummary(BaseModel):
    name: str
    owner: str
    co_owners: list[str] = Field(default_factory=list)
    description: str = ""
    team: str = ""
    tags: list[str] = Field(default_factory=list)
    latest_version: int = 0
    updated_at: str
    updated_by: str = ""
    step_count: int = 0
    checks_ok: bool | None = None
    process_type: str = "pipeline"  # pipeline | copy


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _key(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise WorkspaceError(f"invalid user {email!r}")
    return email


def _name(name: str) -> str:
    if not PROCESS_NAME_RE.match(name or ""):
        raise WorkspaceError(f"invalid process name {name!r}")
    return name


# ====================================================================== store
class WorkspaceStore:
    def __init__(self, backend: Backend):
        self.backend = backend

    @property
    def kind(self) -> str:
        return self.backend.kind

    # ------------------------------------------------------------------ paths
    @staticmethod
    def _base(owner: str, name: str) -> str:
        return f"users/{_key(owner)}/{_name(name)}"

    def _read_json(self, path: str) -> tuple[Any, int] | None:
        got = self.backend.read(path)
        if got is None:
            return None
        return json.loads(got[0]), got[1]

    def _write_json(self, path: str, data: Any, **kw) -> int:
        return self.backend.write(path, json.dumps(data, indent=2, default=str).encode(), **kw)

    # ------------------------------------------------------------------ reads
    def get_meta(self, owner: str, name: str) -> tuple[ProcessMeta, int]:
        got = self._read_json(f"{self._base(owner, name)}/meta.json")
        if got is None:
            raise NotFoundError(f"process {owner}/{name} not found")
        return ProcessMeta.model_validate(got[0]), got[1]

    @staticmethod
    def can_edit(meta: ProcessMeta, user: str) -> bool:
        user = user.lower()
        return user == meta.owner or user in meta.co_owners

    def list_processes(self, *, user: str, scope: str = "all", query: str = "") -> list[ProcessSummary]:
        paths = [p for p in self.backend.list("index/") if p.endswith(".json")]

        def load(path: str) -> ProcessSummary | None:
            got = self._read_json(path)
            return ProcessSummary.model_validate(got[0]) if got else None

        with ThreadPoolExecutor(max_workers=16) as pool:
            items = [s for s in pool.map(load, paths) if s is not None]
        user = user.lower()
        if scope == "mine":
            items = [s for s in items if s.owner == user or user in s.co_owners]
        q = query.strip().lower()
        if q:
            items = [
                s for s in items
                if q in s.name or q in s.owner or q in s.description.lower() or q in s.team.lower() or any(q in t.lower() for t in s.tags)
            ]
        return sorted(items, key=lambda s: s.updated_at, reverse=True)

    def get_version(self, owner: str, name: str, version: int) -> dict[str, Any]:
        base = f"{self._base(owner, name)}/versions/v{version:04d}"
        spec = self._read_json(f"{base}/spec.json")
        if spec is None:
            raise NotFoundError(f"version {version} of {owner}/{name} not found")
        validation = self._read_json(f"{base}/validation.json")
        test_run = self._read_json(f"{base}/test_run.json")
        files = [p[len(base) + len("/files/"):] for p in self.backend.list(f"{base}/files/")]
        return {"spec": spec[0], "validation": validation[0] if validation else None, "files": sorted(files),
                "test_run": test_run[0] if test_run else None}

    def get_version_files(self, owner: str, name: str, version: int) -> dict[str, str]:
        base = f"{self._base(owner, name)}/versions/v{version:04d}/files/"
        out = {}
        for path in self.backend.list(base):
            got = self.backend.read(path)
            if got:
                out[path[len(base):]] = got[0].decode("utf-8")
        if not out:
            raise NotFoundError(f"no generated files stored for version {version}")
        return out

    def get_version_log(self, owner: str, name: str, version: int) -> str | None:
        """The log of the lab test run saved with a version (None when the version was saved without one)."""
        got = self.backend.read(f"{self._base(owner, name)}/versions/v{version:04d}/test_run.log")
        return got[0].decode("utf-8", errors="replace") if got else None

    def get_draft(self, owner: str, name: str, user: str) -> dict[str, Any] | None:
        got = self._read_json(f"{self._base(owner, name)}/drafts/{_key(user)}.json")
        return got[0] if got else None

    # ------------------------------------------------------------------ writes
    def create_process(self, owner: str, spec: dict[str, Any], *, cloned_from: str | None = None) -> ProcessMeta:
        owner = _key(owner)
        md = spec.get("metadata", {})
        name = _name(md.get("name", ""))
        now = _now()
        meta = ProcessMeta(
            name=name, owner=owner, co_owners=[], description=md.get("description", ""), team=md.get("team", ""),
            tags=md.get("tags", []), created_at=now, created_by=owner, updated_at=now, cloned_from=cloned_from,
        )
        self._write_json(f"{self._base(owner, name)}/meta.json", meta.model_dump(), if_generation_match=0)
        self._write_json(f"{self._base(owner, name)}/drafts/{owner}.json", {"spec": spec, "saved_at": now})
        self._write_index(meta, owner, step_count=len(spec.get("steps", [])), checks_ok=None,
                          process_type=spec.get("process_type"))
        return meta

    def save_draft(self, owner: str, name: str, user: str, spec: dict[str, Any]) -> str:
        meta, _ = self.get_meta(owner, name)
        if not self.can_edit(meta, user):
            raise PermissionDenied("only the owner and co-owners can edit this process; clone it to make your own copy")
        now = _now()
        self._write_json(f"{self._base(owner, name)}/drafts/{_key(user)}.json", {"spec": spec, "saved_at": now})
        return now

    def discard_draft(self, owner: str, name: str, user: str) -> None:
        self.backend.delete(f"{self._base(owner, name)}/drafts/{_key(user)}.json")

    def save_version(
        self,
        owner: str,
        name: str,
        user: str,
        *,
        spec: dict[str, Any],
        spec_hash: str,
        files: dict[str, str],
        validation: dict[str, Any] | None,
        note: str = "",
        checks_ok: bool = True,
        test_run: dict[str, Any] | None = None,
        test_run_log: str | None = None,
    ) -> VersionInfo:
        meta, generation = self.get_meta(owner, name)
        if not self.can_edit(meta, user):
            raise PermissionDenied("only the owner and co-owners can save versions; clone it to make your own copy")
        version = meta.latest_version + 1
        base = f"{self._base(owner, name)}/versions/v{version:04d}"
        try:
            self._write_json(f"{base}/spec.json", spec, if_generation_match=0)
        except ConflictError as exc:
            raise ConflictError("someone saved a newer version a moment ago; reload and try again") from exc
        for rel, content in files.items():
            self.backend.write(f"{base}/files/{rel}", content.encode("utf-8"), content_type="text/plain; charset=utf-8")
        if validation is not None:
            self._write_json(f"{base}/validation.json", validation)
        if test_run is not None:
            self._write_json(f"{base}/test_run.json", test_run)
            if test_run_log:
                self.backend.write(f"{base}/test_run.log", test_run_log.encode("utf-8"), content_type="text/plain; charset=utf-8")
        info = VersionInfo(version=version, saved_at=_now(), saved_by=_key(user), spec_hash=spec_hash, note=note[:300],
                           checks_ok=checks_ok, lab_test=(test_run or {}).get("status"))
        md = spec.get("metadata", {})
        meta.latest_version = version
        meta.versions.append(info)
        meta.updated_at = info.saved_at
        meta.description, meta.team, meta.tags = md.get("description", meta.description), md.get("team", meta.team), md.get("tags", meta.tags)
        try:
            self._write_json(f"{self._base(owner, name)}/meta.json", meta.model_dump(), if_generation_match=generation)
        except ConflictError as exc:
            raise ConflictError("the process was changed while saving; reload and try again") from exc
        self._write_index(meta, user, step_count=len(spec.get("steps", [])), checks_ok=checks_ok,
                          process_type=spec.get("process_type"))
        return info

    def set_co_owners(self, owner: str, name: str, user: str, co_owners: list[str]) -> ProcessMeta:
        meta, generation = self.get_meta(owner, name)
        if _key(user) != meta.owner:
            raise PermissionDenied("only the owner can change co-owners")
        meta.co_owners = sorted({_key(e) for e in co_owners if e.strip()} - {meta.owner})
        meta.updated_at = _now()
        self._write_json(f"{self._base(owner, name)}/meta.json", meta.model_dump(), if_generation_match=generation)
        self._write_index(meta, user, step_count=None, checks_ok=None)
        return meta

    def delete_process(self, owner: str, name: str, user: str) -> int:
        """Delete a process with its drafts and every saved version (owner only). Returns the number of files deleted.
        The listing entry goes first, so the process leaves the home page even if a later delete fails."""
        meta, _ = self.get_meta(owner, name)
        if _key(user) != meta.owner:
            raise PermissionDenied("only the owner can delete a process")
        self.backend.delete(f"index/{meta.owner}__{meta.name}.json")
        return self.backend.delete_prefix(self._base(meta.owner, meta.name))

    def clone(self, src_owner: str, name: str, version: int | None, new_owner: str, new_name: str) -> ProcessMeta:
        meta, _ = self.get_meta(src_owner, name)
        if version is None and meta.latest_version:
            version = meta.latest_version
        if version:
            spec = self.get_version(src_owner, name, version)["spec"]
        else:
            draft = self.get_draft(src_owner, name, meta.owner)
            if not draft:
                raise NotFoundError("nothing to clone yet (no saved version)")
            spec = draft["spec"]
        spec = json.loads(json.dumps(spec))
        spec.setdefault("metadata", {})
        spec["metadata"].update(name=_name(new_name), owner=_key(new_owner), co_owners=[])
        return self.create_process(new_owner, spec, cloned_from=f"{meta.owner}/{name}@v{version or 0}")

    # ------------------------------------------------------------------ copy tool audit trail
    def record_copy_job(self, record: dict[str, Any]) -> None:
        """copy_jobs/<user>/<UTC start>_<id>.json: who copied or moved what, where, and how it went."""
        stamp = re.sub(r"[^0-9T]", "", str(record.get("started_at") or _now()))[:15]
        self._write_json(f"copy_jobs/{_key(record['user'])}/{stamp}_{record['id']}.json", record)

    def list_copy_jobs(self, user: str, *, limit: int = 20) -> list[dict[str, Any]]:
        paths = sorted((p for p in self.backend.list(f"copy_jobs/{_key(user)}/") if p.endswith(".json")), reverse=True)
        out = []
        for path in paths[:limit]:
            got = self._read_json(path)
            if got:
                out.append(got[0])
        return out

    def _write_index(self, meta: ProcessMeta, updated_by: str, *, step_count: int | None, checks_ok: bool | None,
                     process_type: str | None = None) -> None:
        path = f"index/{meta.owner}__{meta.name}.json"
        previous = self._read_json(path)
        prev = previous[0] if previous else {}
        summary = ProcessSummary(
            name=meta.name, owner=meta.owner, co_owners=meta.co_owners, description=meta.description, team=meta.team,
            tags=meta.tags, latest_version=meta.latest_version, updated_at=meta.updated_at, updated_by=_key(updated_by),
            step_count=step_count if step_count is not None else prev.get("step_count", 0),
            checks_ok=checks_ok if checks_ok is not None else prev.get("checks_ok"),
            process_type=process_type or prev.get("process_type") or "pipeline",
        )
        self._write_json(path, summary.model_dump())


def make_store(bucket: str | None, local_dir: Path) -> WorkspaceStore:
    return WorkspaceStore(GcsBackend(bucket) if bucket else LocalBackend(local_dir))
