"""The "Copy files" page: move files right now, in three directions.

* **Bucket → bucket**: browse the source bucket, tick files (or take every file matching a pattern), choose the
  destination, copy or move.
* **My computer → bucket**: pick files on your computer and upload them into a bucket folder.
* **Bucket → my computer**: tick files in a bucket and download them (several come as one ZIP).

Everything runs as the hub's ADC identity: your own credentials on a laptop, the lab-allowlisted buckets on a deployed
hub (services/copy_tool.py). To do a copy every day, the page links to the copy-files process, which generates a script.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from nicegui import run, ui

from ..services.copy_tool import CopyRefused, LocalFile, new_temp_dir
from ..services.gcp_tools import gcs_browse
from ..services.validators import gcp_error_text
from .theme import hint, human_bytes, level_icon, section

DIRECTIONS = {"bucket_to_bucket": "Bucket → bucket", "local_to_bucket": "My computer → bucket",
              "bucket_to_local": "Bucket → my computer"}
STATUS_COLORS = {"running": "primary", "done": "positive", "failed": "negative", "cancelled": "grey"}
RUN_LABELS = {"copy": "Copy", "move": "Move", "upload": "Upload", "download": "Download"}
PREVIEW_ROWS = 200


class CopyPage:
    def __init__(self, svc: Any, user: str):
        self.svc, self.user = svc, user
        self.project = svc.copy_project()
        self.settings = svc.settings
        self.direction = "bucket_to_bucket"
        self.buckets: list[str] = []
        self.src = {"bucket": "", "prefix": ""}
        self.listing: dict[str, Any] = {}
        self.selected: set[str] = set()  # file names relative to the source folder
        self.staged: list[LocalFile] = []  # files from your computer, kept by the hub until uploaded
        self.staging_dir: Path | None = None
        self.form = {"pattern": "*", "recursive": False, "dst_bucket": "", "dst_prefix": "", "operation": "copy",
                     "overwrite": False}
        self.plan = None
        self.plan_key: tuple | None = None
        self.job_id: str | None = None
        self._line = 0
        self._poll: ui.timer | None = None
        self._announced: str | None = None  # the finished job already notified (and its download handed over)

    async def build(self) -> None:
        policy = self.svc.copy_jobs.policy()
        identity = await run.io_bound(self.svc.adc_email)
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("drive_file_move", size="md").classes("text-brown-700")
            ui.label("Copy files").classes("text-2xl font-semibold")
            ui.chip("your credentials" if policy.scope == "any" else "lab buckets only", icon="key",
                    color="primary" if policy.scope == "any" else "warning").props("outline").mark("copy-scope")
            ui.space()
            ui.button("Make a script that does it every day", icon="description",
                      on_click=lambda: ui.navigate.to("/new?type=copy")).props("flat no-caps").mark("copy-make-script")
        hint("Copy files between buckets, upload them from your computer or download them, right now. Between buckets the "
             "copy happens inside Cloud Storage: nothing passes through the hub. " + policy.describe(identity))
        unavailable = policy.unavailable()
        if unavailable:
            with ui.card().classes("sf-card w-full bg-amber-50").mark("copy-unavailable"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    level_icon("warn")
                    ui.label(f"The Copy files tool cannot be used here: {unavailable}.").classes("text-sm")
            return
        self.buckets = await run.io_bound(self._buckets, policy)
        ui.toggle(DIRECTIONS, value=self.direction, on_change=lambda e: self.set_direction(e.value)).props(
            "no-caps spread").classes("w-full").mark("copy-direction")
        self.body()
        with section("3 · Progress", icon="sync"):
            self.job_view()
        with section("Your recent copies", "Every copy, upload and download is recorded with who ran it.", "history"):
            self.recent_view()

    def _buckets(self, policy: Any) -> list[str]:
        literal = [p for p in policy.patterns if not any(ch in p for ch in "*?[")]
        try:
            found = [b.name for b in self.svc.clients.storage(self.project).list_buckets(project=self.project or None,
                                                                                          max_results=500)]
        except Exception:  # noqa: BLE001 - no list permission: the names can still be typed
            found = []
        if policy.scope == "allowlist":
            found = [b for b in found if policy.allows(b)]
        return sorted(set(found) | set(literal))

    def _options(self, current: str) -> list[str]:
        return sorted(set(self.buckets) | ({current} if current else set()))

    def set_direction(self, direction: str) -> None:
        self.direction, self.plan = direction, None
        self.body.refresh()

    @ui.refreshable_method
    def body(self) -> None:
        if self.direction == "local_to_bucket":
            self.from_computer()
        else:
            self.from_bucket()
        if self.direction == "bucket_to_local":
            self.to_computer()
        else:
            self.to_bucket()

    # ------------------------------------------------------------------ 1 · from a bucket
    def from_bucket(self) -> None:
        with section("1 · From a bucket", "Pick the bucket and open the folder. Tick files, or leave them all unticked to "
                     "take every file matching the pattern.", "folder_open"):
            ui.select(self._options(self.src["bucket"]), value=self.src["bucket"] or None, label="Source bucket",
                      new_value_mode="add-unique", with_input=True,
                      on_change=lambda e: self.open_folder(e.value or "", "")).props("outlined dense").classes(
                "w-96 sf-mono").mark("copy-src-bucket")
            self.browser()
            with ui.row().classes("w-full gap-4 items-center"):
                ui.input("Files matching", placeholder="*").bind_value(self.form, "pattern").props("outlined dense").classes(
                    "w-72 sf-mono").mark("copy-pattern")
                ui.switch("Include subfolders").bind_value(self.form, "recursive").mark("copy-recursive")
                hint("* matches any text: Daily File_*.csv, *.txt. Used when no file is ticked.")

    async def open_folder(self, bucket: str, prefix: str) -> None:
        self.src.update(bucket=bucket.strip(), prefix=prefix)
        self.selected.clear()
        self.listing = {"loading": True} if bucket else {}
        self.browser.refresh()
        if not bucket:
            return
        try:
            self.listing = await run.io_bound(gcs_browse, self.svc.clients, project_id=self.project, bucket=bucket.strip(),
                                              prefix=prefix)
        except Exception as exc:  # noqa: BLE001
            self.listing = {"error": gcp_error_text(exc)}
        self.browser.refresh()

    @ui.refreshable_method
    def browser(self) -> None:
        bucket, prefix, data = self.src["bucket"], self.src["prefix"], self.listing
        if not bucket:
            hint("Choose the source bucket.")
            return
        with ui.card().classes("sf-card w-full gap-1 max-h-[28rem] overflow-auto").mark("copy-browser"):
            with ui.row().classes("items-center gap-2"):
                ui.label(f"gs://{bucket}/{prefix}").classes("sf-mono font-medium")
                if prefix:
                    parent = "/".join(prefix.rstrip("/").split("/")[:-1])
                    ui.button("up", icon="arrow_upward", on_click=lambda: self.open_folder(bucket, f"{parent}/" if parent else "")).props(
                        "flat dense no-caps size=sm")
            if data.get("loading"):
                ui.spinner()
                return
            if data.get("error"):
                ui.label(data["error"]).classes("sf-problem")
                return
            for p in data.get("prefixes", []):
                ui.button(p[len(prefix):], icon="folder", on_click=lambda p=p: self.open_folder(bucket, p)).props(
                    "flat dense no-caps align=left").classes("w-full")
            files = data.get("objects", [])
            if not files and not data.get("prefixes"):
                hint("This folder is empty.")
            for o in files:
                rel = o["name"][len(prefix):]
                with ui.row().classes("items-center gap-2 no-wrap w-full"):
                    ui.checkbox(value=rel in self.selected, on_change=lambda e, rel=rel: self.toggle(rel, e.value)).props("dense")
                    ui.label(rel).classes("sf-mono text-sm flex-grow")
                    ui.label(human_bytes(o.get("size") or 0)).classes("sf-hint w-24 text-right")
                    ui.label((o.get("updated") or "")[:16].replace("T", " ")).classes("sf-hint w-36")
            if files:
                with ui.row().classes("items-center gap-2"):
                    ui.button("Tick files matching the pattern", on_click=self.tick_matching).props("flat dense no-caps size=sm")
                    ui.button("Clear", on_click=self.clear_ticks).props("flat dense no-caps size=sm")
                    self.ticked_label = ui.label(self._ticked_text()).classes("sf-hint")
                    if len(files) >= 500:
                        hint("Showing the first 500 files: use the pattern to take the rest.")

    def toggle(self, rel: str, on: bool) -> None:
        (self.selected.add if on else self.selected.discard)(rel)
        self.ticked_label.text = self._ticked_text()

    def tick_matching(self) -> None:
        pattern = self.form["pattern"] or "*"
        prefix = self.src["prefix"]
        self.selected = {o["name"][len(prefix):] for o in self.listing.get("objects", [])
                         if fnmatch.fnmatchcase(o["name"][len(prefix):], pattern)}
        self.browser.refresh()

    def clear_ticks(self) -> None:
        self.selected.clear()
        self.browser.refresh()

    def _ticked_text(self) -> str:
        n = len(self.selected)
        return f"{n} file(s) ticked" if n else "nothing ticked: every file matching the pattern is taken"

    # ------------------------------------------------------------------ 1 · from your computer
    def from_computer(self) -> None:
        limit = self.settings.copy_tool_max_upload_mb
        with section("1 · From your computer", f"Pick one or more files (up to {limit} MB each). They keep their names in "
                     "the destination folder.", "upload_file"):
            ui.upload(on_upload=self.on_file, multiple=True, auto_upload=True, max_file_size=limit * 1024 * 1024,
                      label="Drop files here or click +",
                      on_rejected=lambda _e: ui.notify(f"Files over {limit} MB cannot be uploaded through the hub",
                                                       type="warning")).props("flat").classes("w-96").mark("copy-upload")
            self.staged_view()

    async def on_file(self, e: Any) -> None:
        if self.staging_dir is None or not self.staging_dir.exists():
            self.staging_dir = new_temp_dir("upload")
        name = Path(e.file.name).name or "file"
        path = self.staging_dir / name
        await e.file.save(path)
        self.staged = [f for f in self.staged if f.name != name] + [LocalFile(name=name, size=path.stat().st_size, path=str(path))]
        self.staged_view.refresh()

    def unstage(self, name: str) -> None:
        for f in [f for f in self.staged if f.name == name]:
            Path(f.path).unlink(missing_ok=True)
        self.staged = [f for f in self.staged if f.name != name]
        self.staged_view.refresh()

    @ui.refreshable_method
    def staged_view(self) -> None:
        if not self.staged:
            hint("No files yet.")
            return
        with ui.column().classes("w-full gap-0").mark("copy-staged"):
            for f in self.staged:
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("description").classes("text-gray-500")
                    ui.label(f.name).classes("sf-mono text-sm w-96")
                    ui.label(human_bytes(f.size)).classes("sf-hint w-24 text-right")
                    ui.button(icon="close", on_click=lambda f=f: self.unstage(f.name)).props("flat dense round size=sm")
            hint(f"{len(self.staged)} file(s), {human_bytes(sum(f.size for f in self.staged))}")

    # ------------------------------------------------------------------ 2 · destinations
    def to_bucket(self) -> None:
        with section("2 · To a bucket", "Files keep their names (and their subfolders, when included) under the "
                     "destination folder.", "drive_file_move"):
            with ui.row().classes("w-full gap-4 items-center"):
                ui.select(self._options(self.form["dst_bucket"]), label="Destination bucket", new_value_mode="add-unique",
                          with_input=True).bind_value(self.form, "dst_bucket").props("outlined dense").classes(
                    "w-96 sf-mono").mark("copy-dst-bucket")
                ui.input("Destination folder", placeholder="inbound/vendor ('' = the bucket root)").bind_value(
                    self.form, "dst_prefix").props("outlined dense").classes("w-96 sf-mono").mark("copy-dst-folder")
            with ui.row().classes("w-full gap-4 items-center"):
                if self.direction == "bucket_to_bucket":
                    ui.toggle({"copy": "Copy", "move": "Move (delete the sources)"}).bind_value(self.form, "operation").props(
                        "no-caps dense").mark("copy-op")
                ui.toggle({False: "Skip files already there", True: "Overwrite them"}).bind_value(self.form, "overwrite").props(
                    "no-caps dense").mark("copy-overwrite")
            ui.button("Preview", icon="preview", on_click=self.preview).props("unelevated no-caps").mark("copy-preview")
            self.plan_view()

    def to_computer(self) -> None:
        limit = self.settings.copy_tool_max_download_mb
        with section("2 · To your computer", f"One file is saved as it is; several come as one ZIP that keeps their "
                     f"folders (up to {limit:,} MB). Nothing in the bucket changes.", "download"):
            ui.button("Preview", icon="preview", on_click=self.preview).props("unelevated no-caps").mark("copy-preview")
            self.plan_view()

    # ------------------------------------------------------------------ preview and confirm
    def _key(self) -> tuple:
        return (self.direction, self.src["bucket"], self.src["prefix"], tuple(sorted(self.selected)),
                tuple((f.name, f.size) for f in self.staged), *(self.form[k] for k in sorted(self.form)))

    async def preview(self) -> None:
        f, jobs = self.form, self.svc.copy_jobs
        if self.direction == "local_to_bucket":
            self.plan = await run.io_bound(lambda: jobs.plan_upload(
                project=self.project, files=list(self.staged), destination_bucket=(f["dst_bucket"] or "").strip(),
                destination_prefix=f["dst_prefix"] or "", overwrite=bool(f["overwrite"]),
                staging_dir=str(self.staging_dir) if self.staging_dir else None))
        elif not self.src["bucket"]:
            ui.notify("Choose the source bucket first", type="warning")
            return
        elif self.direction == "bucket_to_local":
            self.plan = await run.io_bound(lambda: jobs.plan_download(
                project=self.project, source_bucket=self.src["bucket"], source_prefix=self.src["prefix"],
                names=sorted(self.selected) or None, pattern=f["pattern"] or "*", recursive=bool(f["recursive"])))
        else:
            self.plan = await run.io_bound(lambda: jobs.plan(
                project=self.project, operation=f["operation"], source_bucket=self.src["bucket"],
                source_prefix=self.src["prefix"], names=sorted(self.selected) or None, pattern=f["pattern"] or "*",
                recursive=bool(f["recursive"]), destination_bucket=(f["dst_bucket"] or "").strip(),
                destination_prefix=f["dst_prefix"] or "", overwrite=bool(f["overwrite"])))
        self.plan_key = self._key()
        self.plan_view.refresh()

    @ui.refreshable_method
    def plan_view(self) -> None:
        plan = self.plan
        if plan is None:
            return
        with ui.card().classes("sf-card w-full gap-2 bg-slate-50").mark("copy-plan"):
            if plan.problems:
                for p in plan.problems:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        level_icon("error")
                        ui.label(p).classes("text-sm text-red-800")
                return
            op = plan.operation
            ui.label(f"{len(plan.items)} file(s), {human_bytes(plan.total_bytes)}: {op} {plan.source_uri()} → "
                     f"{plan.destination_uri()}").classes("font-semibold").mark("copy-plan-summary")
            if plan.existing:
                ui.label(f"{plan.existing} already at the destination: "
                         + ("they will be overwritten" if plan.overwrite else "they will be skipped")).classes("text-sm text-amber-800")
            if op == "upload":
                rows = [{"file": i.source, "becomes": f"gs://{plan.destination_bucket}/{i.destination}"} for i in plan.items]
            else:
                rows = [{"file": i.source[len(plan.source_prefix):], "becomes": i.destination} for i in plan.items]
            for row, item in zip(rows, plan.items):
                row.update(size=human_bytes(item.size), there="already there" if item.exists else "")
            becomes = "Saved as" if op == "download" else "Becomes"
            ui.table(columns=[{"name": k, "label": label, "field": k, "align": "left"} for k, label in
                              (("file", "File"), ("becomes", becomes), ("size", "Size"), ("there", ""))],
                     rows=rows[:PREVIEW_ROWS], row_key="becomes").props("dense flat").classes("w-full max-h-80")
            if len(plan.items) > PREVIEW_ROWS:
                hint(f"… and {len(plan.items) - PREVIEW_ROWS:,} more")
            ui.button(f"{RUN_LABELS[op]} {len(plan.items)} file(s)", icon="play_arrow", on_click=self.confirm).props(
                f"unelevated no-caps color={'negative' if op == 'move' else 'primary'}").mark("copy-run")

    async def confirm(self) -> None:
        plan = self.plan
        if plan is None or plan.problems:
            return
        if self._key() != self.plan_key:
            ui.notify("The selection changed since the preview: click Preview again", type="warning")
            return
        identity = await run.io_bound(self.svc.adc_email)
        if plan.operation != "download":  # a download changes nothing: no confirmation needed
            move = plan.operation == "move"
            with ui.dialog() as dialog, ui.card().classes("w-[760px] max-w-full"):
                ui.label(f"{RUN_LABELS[plan.operation]} {len(plan.items)} file(s) now?").classes("text-lg font-semibold")
                ui.label(f"{plan.source_uri()} → {plan.destination_uri()}").classes("sf-mono text-sm")
                hint(f"Runs as {identity or 'the hub ADC identity'}. {human_bytes(plan.total_bytes)} in total.")
                if plan.existing:
                    ui.label(f"{plan.existing} file(s) already at the destination will be "
                             + ("overwritten." if plan.overwrite else "skipped.")).classes("text-sm")
                if move:
                    with ui.card().classes("sf-card w-full bg-red-50 gap-0"):
                        ui.label("Moving deletes each source file once it is copied:").classes("text-sm font-semibold text-red-800")
                        for item in plan.items[:15]:
                            ui.label(f"gs://{plan.source_bucket}/{item.source}").classes("sf-mono text-xs")
                        if len(plan.items) > 15:
                            ui.label(f"… and {len(plan.items) - 15:,} more").classes("text-xs")
                with ui.row().classes("w-full justify-end"):
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat no-caps")
                    ui.button("Move and delete the sources" if move else RUN_LABELS[plan.operation], icon="play_arrow",
                              on_click=lambda: dialog.submit(True)).props(
                        f"unelevated no-caps color={'negative' if move else 'primary'}").mark("copy-confirm")
            if not await dialog:
                return
        try:
            job = await run.io_bound(lambda: self.svc.copy_jobs.start(plan, project=self.project, user=self.user,
                                                                      identity=identity))
        except CopyRefused as exc:
            ui.notify("The copy cannot start: " + "; ".join(exc.problems), type="negative", multi_line=True)
            return
        if plan.operation == "upload":  # the job owns the hub's copy of the files now
            self.staged, self.staging_dir = [], None
            self.body.refresh()
        self.job_id, self._line, self.plan = job.id, 0, None
        self.job_view.refresh()  # its timer follows the job (created inside the view, not under the clicked button)
        self.plan_view.refresh()

    # ------------------------------------------------------------------ 3 · progress
    @ui.refreshable_method
    def job_view(self) -> None:
        job = self.svc.copy_jobs.get(self.job_id) if self.job_id else None
        if job is None:
            hint("Nothing running. Preview a copy above.")
            return
        with ui.row().classes("items-center gap-3"):
            self.job_status = ui.chip(job.status, icon="sync").props("outline").mark("copy-status")
            self.job_counts = ui.label().classes("text-sm").mark("copy-counts")
            self.cancel_btn = ui.button("Cancel", icon="stop", on_click=self.cancel).props("flat no-caps color=negative")
            self.save_btn = ui.button("Save the download", icon="save", on_click=lambda: self.save(job)).props(
                "unelevated no-caps").mark("copy-save")
            self.save_btn.visible = False
        self.progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
        self.job_log = ui.log(max_lines=2000).classes("w-full h-60 sf-mono text-xs").mark("copy-log")
        self._line = 0
        self._poll = ui.timer(0.5, self.poll) if job.active else None
        self.poll()

    def save(self, job: Any) -> None:
        if job.download_path and Path(job.download_path).is_file():
            # streamed by the API, which also checks that the job is yours
            ui.download.from_url(f"/api/copy/jobs/{job.id}/file", Path(job.download_path).name)
        else:
            ui.notify("This download is no longer on the hub (downloads are kept for an hour): download again", type="warning")

    def poll(self) -> None:
        job = self.svc.copy_jobs.get(self.job_id) if self.job_id else None
        if job is None or not hasattr(self, "job_status"):
            return
        counts, total = job.counts(), len(job.plan.items) or 1
        finished = total - counts["pending"]
        self.progress.value = finished / total
        self.job_status.text = job.status
        self.job_status.props(f"color={STATUS_COLORS.get(job.status, 'grey')}")
        self.job_counts.text = job.message if not job.active else (
            f"{finished} of {total}: {counts['copied'] + counts['moved']} {job.verb}, {counts['skipped']} skipped, "
            f"{counts['failed']} failed")
        lines = list(job.lines)
        for line in lines[self._line:]:
            self.job_log.push(line)
        self._line = len(lines)
        self.cancel_btn.visible = job.active
        if job.download_path:
            self.save_btn.text = f"Save {Path(job.download_path).name}"
            self.save_btn.visible = True
        if job.active:
            return
        if self._poll is not None:
            self._poll.deactivate()
            self._poll = None
        if self._announced != job.id:
            self._announced = job.id
            self.recent_view.refresh()
            ui.notify(job.message, type="positive" if job.status == "done" else "warning", multi_line=True)
            if job.download_path:
                self.save(job)  # hand the file to the browser straight away

    def cancel(self) -> None:
        if self.job_id:
            self.svc.copy_jobs.cancel(self.job_id, self.user)
            ui.notify("Stopping after the files already started…", type="info")

    @ui.refreshable_method
    def recent_view(self) -> None:
        jobs = self.svc.copy_jobs.recent(self.user)
        if not jobs:
            hint("No copies yet.")
            return
        for r in jobs:
            with ui.row().classes("items-center gap-3 w-full no-wrap"):
                ui.chip(r["status"], color=STATUS_COLORS.get(r["status"], "grey")).props("outline dense")
                ui.label(f"{r['started_at'][:16].replace('T', ' ')} UTC").classes("text-sm w-40")
                pattern = "" if r["pattern"] == "*" or r["operation"] == "upload" else r["pattern"]
                ui.label(f"{r['operation']} {r['source']}{pattern} → {r['destination']}").classes("sf-mono text-xs flex-grow")
                ui.label(r.get("message") or f"{r['files']} file(s)").classes("sf-hint")
