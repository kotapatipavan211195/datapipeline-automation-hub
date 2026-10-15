"""Fake GCP + Gemini clients for backend tests."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace


class NotFound(Exception):
    pass


NotFound.__name__ = "NotFound"


class PreconditionFailed(Exception):
    code = 412


class FakeBlob:
    def __init__(self, name: str, data: bytes = b"", updated: dt.datetime | None = None, bucket: "FakeBucket | None" = None,
                 generation: int = 1):
        self.name, self._data = name, data
        self.size = len(data)
        self.updated = updated or dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.bucket, self.generation = bucket, generation

    def download_as_bytes(self, start=None, end=None):
        if start is None:
            return self._data
        return self._data[start : (end + 1 if end is not None else None)]

    def rewrite(self, source, token=None, if_generation_match=None, if_source_generation_match=None, **_):
        """Server-side copy of source into this (destination) blob, like google-cloud-storage's Blob.rewrite."""
        gcs, bucket = self.bucket.gcs, self.bucket.name
        gcs.check(bucket)
        objects = gcs.objects.setdefault(bucket, {})
        if if_generation_match == 0 and self.name in objects:
            raise PreconditionFailed(f"412 gs://{bucket}/{self.name} already exists")
        current = gcs.objects.get(source.bucket.name, {})
        if source.name not in current:
            raise NotFound(f"404 gs://{source.bucket.name}/{source.name}")
        objects[self.name] = current[source.name]
        gcs.calls.append(f"rewrite {source.bucket.name}/{source.name} -> {bucket}/{self.name}")
        return None, len(objects[self.name]), len(objects[self.name])

    def upload_from_filename(self, filename, if_generation_match=None, **_):
        gcs, bucket = self.bucket.gcs, self.bucket.name
        gcs.check(bucket)
        objects = gcs.objects.setdefault(bucket, {})
        if if_generation_match == 0 and self.name in objects:
            raise PreconditionFailed(f"412 gs://{bucket}/{self.name} already exists")
        with open(filename, "rb") as f:
            objects[self.name] = f.read()
        gcs.calls.append(f"upload {filename} -> {bucket}/{self.name}")

    def download_to_filename(self, filename, **_):
        self.bucket.gcs.check(self.bucket.name)
        with open(filename, "wb") as f:
            f.write(self.bucket.gcs.objects[self.bucket.name][self.name])

    def delete(self, if_generation_match=None, **_):
        gcs, bucket = self.bucket.gcs, self.bucket.name
        gcs.check(bucket)
        gcs.objects.get(bucket, {}).pop(self.name, None)
        gcs.calls.append(f"delete {bucket}/{self.name}")


class FakeBucket:
    def __init__(self, gcs: "FakeGcs", name: str):
        self.gcs, self.name = gcs, name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(name, bucket=self)

    def get_blob(self, name: str) -> FakeBlob | None:
        self.gcs.check(self.name)
        data = self.gcs.objects.get(self.name, {}).get(name)
        return None if data is None else FakeBlob(name, data, bucket=self)


class _Listing(list):
    prefixes: list[str] = []


class FakeGcs:
    def __init__(self, objects: dict[str, dict[str, bytes]] | None = None, forbidden: set[str] | None = None):
        self.objects = objects or {}
        self.forbidden = forbidden or set()
        self.calls: list[str] = []  # every bucket/prefix touched (the lab-only guarantee is tested on this)

    def check(self, bucket: str) -> None:
        if bucket in self.forbidden:
            raise PermissionError(f"403 no access to {bucket}")

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)

    def list_buckets(self, project=None, max_results=None, **_):
        return [SimpleNamespace(name=b) for b in sorted(self.objects) if b not in self.forbidden]

    def list_blobs(self, bucket, prefix=None, match_glob=None, max_results=None, delimiter=None, **_):
        import fnmatch

        self.calls.append(f"{bucket}/{prefix or ''}")
        self.check(bucket)
        names = sorted(self.objects.get(bucket, {}))
        out = _Listing(
            FakeBlob(n, self.objects[bucket][n], bucket=FakeBucket(self, bucket))
            for n in names
            if (not prefix or n.startswith(prefix)) and (not match_glob or fnmatch.fnmatchcase(n, match_glob))
        )
        if delimiter:  # one folder level, like the real listing: sub-folders become prefixes
            start = len(prefix or "")
            out.prefixes = sorted({b.name[: b.name.index(delimiter, start) + 1] for b in out if delimiter in b.name[start:]})
            out[:] = [b for b in out if delimiter not in b.name[start:]]
        if max_results:
            out[:] = out[:max_results]
        return out


class FakeField(SimpleNamespace):
    pass


class FakeBq:
    def __init__(self, datasets=(), tables=None, failing_sql: dict[str, str] | None = None, schema=None,
                 guess_types=(), bad_rows=(), load_error: str | None = None):
        self.datasets = set(datasets)
        self.tables = tables or {}
        self.failing_sql = failing_sql or {}
        self.queries = []
        self.refs: list[str] = []  # every dataset/table looked up
        self.schema = schema
        self.guess_types, self.bad_rows, self.load_error = list(guess_types), list(bad_rows), load_error
        self.loads: list[tuple[str, str, object]] = []  # (table, CSV text, job config) of every load job
        self.deleted: list[str] = []

    def load_table_from_file(self, file_obj, destination, job_config=None, location=None):
        """Load job with autodetect: the table gets ``guess_types`` (named like BigQuery's headerless columns);
        ``bad_rows`` are the error messages of rejected rows."""
        text = file_obj.read().decode("utf-8")
        self.loads.append((destination, text, job_config))
        if self.load_error:
            raise RuntimeError(self.load_error)
        fields = [SimpleNamespace(to_api_repr=lambda i=i, t=t: {"name": f"{t.lower()}_field_{i}", "type": t, "mode": "NULLABLE"})
                  for i, t in enumerate(self.guess_types)]
        self.tables[destination] = SimpleNamespace(schema=fields)
        rows = len(text.splitlines())
        return SimpleNamespace(result=lambda timeout=None: None, output_rows=rows - len(self.bad_rows),
                               errors=[{"message": m} for m in self.bad_rows])

    def delete_table(self, table, not_found_ok=False):
        self.deleted.append(table)
        self.tables.pop(table, None)

    def query(self, sql, job_config=None, location=None):
        self.queries.append((sql, job_config))
        for marker, error in self.failing_sql.items():
            if marker in sql:
                raise RuntimeError(error)
        return SimpleNamespace(total_bytes_processed=123456789, schema=self.schema, referenced_tables=[])

    def get_dataset(self, ref):
        self.refs.append(ref)
        if ref not in self.datasets:
            raise NotFound(f"404 Not found: Dataset {ref}")
        return SimpleNamespace(dataset_id=ref)

    def get_table(self, ref):
        self.refs.append(ref)
        if ref not in self.tables:
            raise NotFound(f"404 Not found: Table {ref}")
        return self.tables[ref]


class FakeSecretManager:
    """Secret Manager metadata stand-in: {"projects/p/secrets/s": [(version, state), ...]} (newest last)."""

    def __init__(self, secrets: dict[str, list[tuple[int, str]]] | None = None):
        self.secrets = secrets or {}

    def get_secret_version(self, request):
        parent, _, version = request["name"].rpartition("/versions/")
        versions = self.secrets.get(parent)
        if not versions:
            raise NotFound(f"404 Secret [{parent}] not found or has no versions")
        number, state = versions[-1] if version == "latest" else next((v for v in versions if str(v[0]) == version), (None, None))
        if number is None:
            raise NotFound(f"404 {request['name']} not found")
        return SimpleNamespace(name=f"{parent}/versions/{number}", state=SimpleNamespace(name=state),
                               create_time=dt.datetime(2026, 1, number, tzinfo=dt.timezone.utc))


class FakeGemini:
    """Stands in for GeminiService: returns a canned GeminiSqlAnalysis."""

    def __init__(self, response=None, error: Exception | None = None):
        from app.settings import Settings

        self.settings = Settings(gcp_project="test")
        self.response, self.error = response, error
        self.calls = 0

    @property
    def available(self):
        return True

    def generate_structured(self, prompt, schema, **_):
        from app.services.gemini import Usage

        self.calls += 1
        if self.error:
            raise self.error
        return schema.model_validate(self.response), Usage(10, 0, 5)

    def generate_text(self, prompt, **_):
        from app.services.gemini import Usage

        return "This process loads sales.", Usage(1, 0, 1)
