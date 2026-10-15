"""BigQuery column schema model shared by the wizard, the inference helpers and the renderer."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

COLUMN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,299}$")

# Canonical BigQuery type names the UI offers (legacy aliases are normalised on input).
BQ_TYPES = (
    "STRING",
    "BYTES",
    "INTEGER",
    "FLOAT",
    "NUMERIC",
    "BIGNUMERIC",
    "BOOLEAN",
    "TIMESTAMP",
    "DATE",
    "TIME",
    "DATETIME",
    "GEOGRAPHY",
    "JSON",
    "RECORD",
)
_TYPE_ALIASES = {
    "INT64": "INTEGER",
    "INT": "INTEGER",
    "SMALLINT": "INTEGER",
    "BIGINT": "INTEGER",
    "TINYINT": "INTEGER",
    "BYTEINT": "INTEGER",
    "FLOAT64": "FLOAT",
    "DOUBLE": "FLOAT",
    "BOOL": "BOOLEAN",
    "STRUCT": "RECORD",
    "DECIMAL": "NUMERIC",
    "BIGDECIMAL": "BIGNUMERIC",
}

Mode = Literal["NULLABLE", "REQUIRED", "REPEATED"]


def normalize_type(value: str) -> str:
    t = (value or "").strip().upper()
    t = _TYPE_ALIASES.get(t, t)
    if t not in BQ_TYPES:
        raise ValueError(f"unsupported BigQuery type {value!r}; use one of {', '.join(BQ_TYPES)}")
    return t


def sanitize_column_name(raw: str, used: set[str] | None = None) -> str:
    """Turn an arbitrary header into a valid, unique BigQuery column name."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", (raw or "").strip())
    name = re.sub(r"_+", "_", name).strip("_") or "column"
    if name[0].isdigit():
        name = f"_{name}"
    name = name[:300]
    if used is not None:
        base, n = name, 2
        while name.lower() in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name.lower())
    return name


class Column(BaseModel):
    name: str
    type: str = "STRING"
    mode: Mode = "NULLABLE"
    description: str = ""
    fields: list[Column] = Field(default_factory=list)
    width: int | None = Field(default=None, ge=1, le=100_000)  # fixed-width files: characters this column takes

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not COLUMN_NAME_RE.match(v or ""):
            raise ValueError(
                f"invalid column name {v!r}: use letters, digits and underscores, not starting with a digit"
            )
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        return normalize_type(v)

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, v: Any) -> Any:
        return (v or "NULLABLE").upper() if isinstance(v, str) or v is None else v

    @model_validator(mode="after")
    def _record_fields(self) -> Column:
        if self.type == "RECORD" and not self.fields:
            raise ValueError(f"RECORD column {self.name!r} needs at least one nested field")
        if self.type != "RECORD" and self.fields:
            raise ValueError(f"column {self.name!r} has nested fields but type {self.type} (use RECORD)")
        names = [f.name.lower() for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"RECORD {self.name!r} has duplicate field names: {', '.join(sorted(dupes))}")
        return self

    def to_bq_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "mode": self.mode}
        if self.description:
            out["description"] = self.description
        if self.fields:
            out["fields"] = [f.to_bq_json() for f in self.fields]
        return out

    def is_nested(self) -> bool:
        return self.type == "RECORD" or self.mode == "REPEATED"


def columns_to_bq_json(columns: list[Column]) -> list[dict[str, Any]]:
    return [c.to_bq_json() for c in columns]


def columns_from_bq_json(data: Any) -> list[Column]:
    """Accept a BigQuery JSON schema in any of its usual wrappings."""
    if isinstance(data, dict):
        if "schema" in data and isinstance(data["schema"], dict):
            data = data["schema"]
        if "fields" in data:
            data = data["fields"]
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of fields, or an object with a 'fields' array")
    return [_column_from_dict(d) for d in data]


def _column_from_dict(d: Any) -> Column:
    if not isinstance(d, dict) or "name" not in d:
        raise ValueError(f"each field needs at least a 'name': got {d!r}")
    return Column(
        name=d["name"],
        type=d.get("type", "STRING"),
        mode=d.get("mode") or "NULLABLE",
        description=d.get("description") or "",
        fields=[_column_from_dict(f) for f in d.get("fields") or []],
        width=d.get("width"),
    )


def check_unique_names(columns: list[Column]) -> list[str]:
    names = [c.name.lower() for c in columns]
    return sorted({n for n in names if names.count(n) > 1})
