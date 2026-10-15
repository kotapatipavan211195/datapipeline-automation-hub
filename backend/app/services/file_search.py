"""Find files: search one or more buckets for a file name or a name pattern, in every folder.

Read-only. It runs as the hub's ADC identity with the Copy files tool's bucket rule: a hub on a laptop (auth_mode local)
may search any bucket your own credentials can read; a deployed hub only the lab buckets in SF_COPY_TOOL_BUCKETS.

How a pattern matches:

* plain text (``Daily_Engagement``): the file name contains it;
* with wildcards ``* ? [ ]`` (``*.csv``, ``MBR_EXTRACT_2026??.txt``): the whole file name matches it;
* with a ``/`` (``inbound/*.csv``): the path matches it from any folder down, so ``inbound/*.csv`` finds
  ``a/b/inbound/x.csv`` too.

Matching ignores case unless asked otherwise.
"""

from __future__ import annotations

import fnmatch
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models.rules import BUCKET_RE
from ..settings import Settings
from .copy_tool import CopyPolicy, folder
from .validators import ClientFactory, gcp_error_text

WILDCARDS = frozenset("*?[")
LIST_FIELDS = "items(name,size,updated),nextPageToken"  # only what the results show: smaller, faster pages


@dataclass
class Target:
    bucket: str
    prefix: str = ""  # folder inside the bucket ('' = the whole bucket)

    def uri(self) -> str:
        return f"gs://{self.bucket}/{self.prefix}"


@dataclass
class FoundFile:
    bucket: str
    name: str
    size: int = 0
    updated: str = ""  # ISO 8601, UTC

    def as_dict(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "name": self.name, "uri": f"gs://{self.bucket}/{self.name}", "size": self.size,
                "updated": self.updated}


@dataclass
class SearchResult:
    targets: list[str] = field(default_factory=list)
    files: list[FoundFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # nothing was searched, or a bucket could not be listed
    notes: list[str] = field(default_factory=list)  # the search stopped early somewhere
    scanned: int = 0  # files looked at
    truncated: bool = False
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"targets": self.targets, "files": [f.as_dict() for f in self.files], "problems": self.problems,
                "notes": self.notes, "scanned": self.scanned, "truncated": self.truncated, "seconds": round(self.seconds, 2)}


def parse_targets(text: str | list[str]) -> tuple[list[Target], list[str]]:
    """'bucket-a, gs://bucket-b/inbound/' → targets (commas, semicolons or spaces between them), plus problems."""
    items = text if isinstance(text, list) else re.split(r"[,;\s]+", text or "")
    targets: list[Target] = []
    problems: list[str] = []
    for raw in items:
        item = str(raw or "").strip()
        if not item:
            continue
        path = item[5:] if item.lower().startswith("gs://") else item
        bucket, _, prefix = path.partition("/")
        if not BUCKET_RE.match(bucket):
            problems.append(f"{item!r} is not a bucket name (write my-bucket or gs://my-bucket/folder/)")
            continue
        target = Target(bucket, folder(prefix))
        if target not in targets:
            targets.append(target)
    return targets, problems


def name_matcher(pattern: str, case_sensitive: bool = False) -> Callable[[str], bool]:
    """A test for object names (paths inside a bucket); see the module docstring for the rules."""
    pattern = pattern.strip().strip("/") if "/" in pattern else pattern.strip()
    norm: Callable[[str], str] = (lambda t: t) if case_sensitive else str.lower
    pat = norm(pattern)
    on_path = "/" in pattern
    glob = any(ch in WILDCARDS for ch in pattern)

    def candidates(name: str) -> list[str]:
        if not on_path:
            return [name.rsplit("/", 1)[-1]]
        parts = name.split("/")
        return ["/".join(parts[i:]) for i in range(len(parts))]  # the path from every folder down

    if glob:
        return lambda name: any(fnmatch.fnmatchcase(norm(c), pat) for c in candidates(name))
    if on_path:
        return lambda name: any(norm(c).startswith(pat) for c in candidates(name))
    return lambda name: pat in norm(name.rsplit("/", 1)[-1])


def search_files(
    clients: ClientFactory,
    settings: Settings,
    *,
    project: str,
    buckets: str | list[str],
    pattern: str,
    case_sensitive: bool = False,
    max_results: int | None = None,
    max_scan: int | None = None,
) -> SearchResult:
    """Every file in the buckets (and folders) whose name matches, at any folder depth; buckets are searched side by
    side. Stops at max_results matches, or after max_scan files in one bucket."""
    started = time.perf_counter()
    targets, problems = parse_targets(buckets)
    result = SearchResult(targets=[t.uri() for t in targets], problems=problems)
    policy = CopyPolicy(settings)
    if not settings.file_search_enabled:
        result.problems.append("Find files is turned off on this hub (SF_FILE_SEARCH_ENABLED)")
    elif policy.scope == "allowlist" and not policy.patterns:
        result.problems.append("no buckets are allowed yet: the hub admin lists the lab buckets in SF_COPY_TOOL_BUCKETS")
    if not pattern.strip():
        result.problems.append("enter a file name or a pattern such as *.csv")
    if not targets and not problems:
        result.problems.append("enter at least one bucket")
    for t in targets:
        if not policy.allows(t.bucket):
            result.problems.append(f"gs://{t.bucket} is not one of the buckets this hub may search "
                                   f"({', '.join(policy.patterns) or 'none configured'})")
    if result.problems:
        return result

    match = name_matcher(pattern, case_sensitive)
    try:
        client = clients.storage(project)
    except Exception as exc:  # noqa: BLE001  (no Application Default Credentials, …)
        result.problems.append(f"could not reach Cloud Storage as the hub's identity: {gcp_error_text(exc)}")
        return result
    max_results = max_results or settings.file_search_max_results
    max_scan = max_scan or settings.file_search_max_scan
    found: list[FoundFile] = []
    lock = threading.Lock()
    full = threading.Event()  # enough matches: every bucket stops

    def scan(t: Target) -> tuple[int, list[str], list[str]]:
        scanned, problems, notes = 0, [], []
        try:
            for blob in client.list_blobs(t.bucket, prefix=t.prefix or None, fields=LIST_FIELDS):
                if full.is_set():
                    break
                if scanned >= max_scan:
                    notes.append(f"stopped after {max_scan:,} files in {t.uri()}: add a folder "
                                 f"(gs://{t.bucket}/folder/) to search a smaller part")
                    break
                scanned += 1
                if blob.name.endswith("/") or not match(blob.name):
                    continue  # folder placeholders, other files
                updated = getattr(blob, "updated", None)
                with lock:
                    found.append(FoundFile(t.bucket, blob.name, int(blob.size or 0),
                                           updated.isoformat() if updated is not None else ""))
                    if len(found) >= max_results:
                        full.set()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not list {t.uri()}: {gcp_error_text(exc)}")
        return scanned, problems, notes

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        for scanned, problems, notes in pool.map(scan, targets):
            result.scanned += scanned
            result.problems += problems
            result.notes += notes
    unique = {(f.bucket, f.name): f for f in found}  # a folder given twice (bucket and bucket/folder) lists files twice
    result.files = sorted(unique.values(), key=lambda f: (f.bucket, f.name))[:max_results]
    if full.is_set():
        result.truncated = True
        result.notes.append(f"showing the first {max_results:,} matches: narrow the pattern or add a folder")
    elif result.notes:
        result.truncated = True
    result.seconds = time.perf_counter() - started
    return result
