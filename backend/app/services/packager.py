"""Package a rendered bundle as an in-memory ZIP (one folder, named after the script)."""

from __future__ import annotations

import io
import zipfile


def build_zip(folder: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{folder}/{rel}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[rel])
    return buf.getvalue()


def bundle_stem(files: dict[str, str], default: str) -> str:
    """The script stem of a stored bundle: its top-level .py file (older bundles: the process name)."""
    roots = sorted(p[:-3] for p in files if p.endswith(".py") and "/" not in p)
    return roots[0] if roots else default
