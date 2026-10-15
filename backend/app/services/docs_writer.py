"""Optional Gemini-written README overview (falls back to nothing; the template has its own structure)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from ..models.spec import ProcessSpec
from .gemini import GeminiError, GeminiService

log = logging.getLogger("dahub.docs")
PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "readme_writer.md"
_cache: dict[str, str] = {}


def spec_digest(spec: ProcessSpec) -> dict:
    lab = spec.variable_values("lab")
    steps = []
    for s in spec.enabled_steps():
        item = {"function": s.function_name, "type": s.type, "description": s.description}
        if s.type == "ingest":
            item.update(source=f"files named '{s.filename}*' in gs://{s.read_bucket}/{s.read_path}",
                        target=s.target() if s.target_enabled else None, history=s.history,
                        load=s.load_mode if s.target_enabled else "raw rows kept in the staging table for the next steps")
        elif s.type == "sql":
            item.update(kind=s.statement_kind, targets=s.targets or [s.destination_table])
        elif s.type == "export":
            item.update(destination=f"gs://{s.bucket}/{s.path}/{s.file_name}<date>", format=s.file_format)
        elif s.type == "copy":
            item.update(command=f"gcloud storage {s.operation}", sources=s.sources, destination=s.destination)
        elif s.type == "dq":
            item.update(checks=[c.label() for c in s.checks])
        elif s.type == "custom":
            item.update(request=s.request, settings=[x.key for x in s.settings])
        steps.append(item)
    return {
        "name": spec.metadata.name,
        "description": spec.metadata.description,
        "team": spec.metadata.team,
        "script": f"{spec.script_stem()}.py",
        "environments": "lab (dev) and value (prod)" + (" and qa" if spec.environments.qa_enabled else ""),
        "lab_projects": {"read": lab.get("read_project"), "write": lab.get("write_project")},
        "email": spec.email.enabled,
        "steps": steps,
    }


def ai_overview(spec: ProcessSpec, gemini: GeminiService | None) -> str:
    if gemini is None or not gemini.available:
        return ""
    digest = json.dumps(spec_digest(spec), indent=1)
    key = hashlib.sha256(digest.encode()).hexdigest()
    if key in _cache:
        return _cache[key]
    try:
        text, _usage = gemini.generate_text(
            f"<specification>\n{digest}\n</specification>", system_instruction=PROMPT.read_text(encoding="utf-8")
        )
    except GeminiError as exc:
        log.warning("README overview not generated: %s", exc)
        return ""
    text = " ".join(text.split())[:1500]
    _cache[key] = text
    return text
