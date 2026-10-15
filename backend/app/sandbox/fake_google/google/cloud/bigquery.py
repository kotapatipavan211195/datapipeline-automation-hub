"""Fake google.cloud.bigquery: records every call, never talks to Google.

Data-quality queries (``... AS value``) return a value that satisfies any comparison, so smoke runs exercise the
whole script without real data.
"""

import itertools
import json
import os
import re

_ids = itertools.count(1)


def _record(kind, **data):
    path = os.environ.get("DAHUB_SMOKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **data}, default=str) + "\n")


class PassValue:
    """Compares as 'passing' with any data-quality expectation."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __ge__(self, other):
        return True

    def __le__(self, other):
        return True

    def __gt__(self, other):
        return True

    def __lt__(self, other):
        return True

    def __hash__(self):
        return 0

    def __format__(self, spec):
        return "ok"

    def __str__(self):
        return "ok"


class _Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _empty_frame():
    import pandas  # only scripts that read rows into pandas (Excel exports) get here

    return pandas.DataFrame()


class _Job:
    def __init__(self, rows=None):
        self.job_id = f"smoke_job_{next(_ids)}"
        self._rows = rows or []
        self.num_dml_affected_rows = 0
        self.total_bytes_processed = 0

    def result(self, *args, **kwargs):
        return list(self._rows)

    def to_dataframe(self, *args, **kwargs):
        return _empty_frame()

    def to_api_repr(self):
        return {"jobReference": {"jobId": self.job_id},
                "statistics": {"query": {"exportDataStatistics": {"fileCount": "1", "rowCount": "2"}}}}


class _RowIterator(list):
    def to_dataframe(self, *args, **kwargs):
        return _empty_frame()


class _Table:
    def __init__(self, table_id):
        self.full_table_id = str(table_id)
        self.num_rows = 2
        self.schema = []


class _Config:
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)


class QueryJobConfig(_Config):
    pass


class LoadJobConfig(_Config):
    pass


class ExtractJobConfig(_Config):
    pass


class ScalarQueryParameter:
    def __init__(self, name, type_, value):
        self.name, self.type_, self.value = name, type_, value


class SchemaField:
    def __init__(self, name, field_type, mode="NULLABLE", **kwargs):
        self.name, self.field_type, self.mode = name, field_type, mode


class _Enum:
    def __getattr__(self, name):
        return name


DestinationFormat = _Enum()
Compression = _Enum()
SourceFormat = _Enum()
WriteDisposition = _Enum()
CreateDisposition = _Enum()


class Client:
    def __init__(self, project=None, credentials=None, **kwargs):
        self.project = project

    def query(self, query=None, job_config=None, **kwargs):
        sql = query if query is not None else kwargs.get("sql", "")
        params = [(p.name, p.type_, p.value) for p in getattr(job_config, "query_parameters", []) or []]
        _record("query", sql=sql, params=params)
        rows = [_Row(value=PassValue())] if re.search(r"\bAS value\b", sql or "") else []
        return _Job(rows)

    def get_table(self, table_id):
        _record("get_table", table=str(table_id))
        return _Table(table_id)

    def list_rows(self, table, *args, **kwargs):
        _record("list_rows", table=str(table))
        return _RowIterator()

    def extract_table(self, source, destination_uris, location=None, job_config=None, **kwargs):
        _record("extract", source=str(source), destination=str(destination_uris), location=location)
        return _Job()

    def load_table_from_dataframe(self, dataframe, destination, job_config=None, **kwargs):
        import pyarrow  # the real client converts the frame with pyarrow: catch unconvertible columns here too

        pyarrow.Table.from_pandas(dataframe, preserve_index=False)
        _record("load_dataframe", table=str(destination), rows=len(dataframe), columns=list(dataframe.columns),
                write_disposition=getattr(job_config, "write_disposition", None))
        return _Job()

    def load_table_from_uri(self, source_uris, destination, job_config=None, **kwargs):
        _record("load_uri", table=str(destination), uris=source_uris)
        return _Job()
