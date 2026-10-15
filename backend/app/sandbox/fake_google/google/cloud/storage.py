"""Fake google.cloud.storage backed by a manifest of sample files (DAHUB_SMOKE_MANIFEST):
{"bucket": {"object/name.csv": "/local/sample/file"}}. Copies and deletes are recorded, not performed."""

import datetime
import json
import os


def _record(kind, **data):
    path = os.environ.get("DAHUB_SMOKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **data}, default=str) + "\n")


def _manifest():
    path = os.environ.get("DAHUB_SMOKE_MANIFEST")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Blob:
    def __init__(self, bucket, name, local_path=None):
        self.bucket = bucket
        self.name = name
        self._local = local_path
        self.size = os.path.getsize(local_path) if local_path and os.path.exists(local_path) else 0
        self.updated = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)  # same time: ties sort by name

    def download_as_bytes(self, *args, **kwargs):
        if self._local and os.path.exists(self._local):
            with open(self._local, "rb") as fh:
                return fh.read()
        return b"smoke-run placeholder\n"

    def download_as_text(self, *args, encoding="utf-8", **kwargs):
        return self.download_as_bytes().decode(encoding)

    def delete(self, *args, **kwargs):
        _record("gcs_delete", uri=f"gs://{self.bucket.name}/{self.name}")

    def upload_from_string(self, data, *args, **kwargs):
        _record("gcs_upload", uri=f"gs://{self.bucket.name}/{self.name}")


class Bucket:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def blob(self, name):
        return Blob(self, name, _manifest().get(self.name, {}).get(name))

    def copy_blob(self, blob, destination_bucket, new_name=None, **kwargs):
        _record("gcs_copy", source=f"gs://{self.name}/{blob.name}", destination=f"gs://{destination_bucket.name}/{new_name}")
        return Blob(destination_bucket, new_name or blob.name)


class Client:
    def __init__(self, project=None, credentials=None, **kwargs):
        self.project = project

    def bucket(self, name):
        return Bucket(self, name)

    def get_bucket(self, name):
        return Bucket(self, name)

    def list_blobs(self, bucket_or_name, prefix=None, **kwargs):
        name = getattr(bucket_or_name, "name", bucket_or_name)
        bucket = Bucket(self, name)
        files = _manifest().get(name, {})
        return [Blob(bucket, obj, path) for obj, path in files.items() if obj.startswith(prefix or "")]
