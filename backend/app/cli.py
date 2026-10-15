"""Command-line generator: render a spec.json without the web app.

    python -m app.cli generate ../samples/sales_daily.json -o ../output
    python -m app.cli validate ../samples/sales_daily.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .models.spec import ProcessSpec, format_validation_errors
from .services.adc import lookup_adc_identity
from .services.packager import build_zip
from .services.renderer import RenderError, render_process, write_bundle


def _load(path: str) -> ProcessSpec:
    try:
        return ProcessSpec.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except ValidationError as exc:
        for err in format_validation_errors(exc):
            print(f"  ✖ {err['path'] or 'spec'}: {err['message']}", file=sys.stderr)
        raise SystemExit(3) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dahub", description="Generate BigQuery/GCS pipeline scripts from a spec")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="render the bundle into a folder (and optionally a zip)")
    gen.add_argument("spec")
    gen.add_argument("-o", "--output", default="output")
    gen.add_argument("--zip", action="store_true", help="also write <script>.zip")
    gen.add_argument("--skip-smoke", action="store_true", help="do not smoke-run the script offline (fake Google clients)")
    gen.add_argument("--adc-email", help="ADC identity to record in the files (default: look it up from your credentials)")
    val = sub.add_parser("validate", help="check a spec without writing anything")
    val.add_argument("spec")
    args = parser.parse_args(argv)

    spec = _load(args.spec)
    if args.command == "validate":
        print(f"✔ {spec.metadata.name}: spec is valid ({len(spec.steps)} step(s))")
        return 0
    try:
        adc_email = args.adc_email or lookup_adc_identity().email or "unknown"
        result = render_process(spec, adc_email=adc_email, smoke_run=not args.skip_smoke)
    except RenderError as exc:
        for problem in exc.problems:
            print(f"  ✖ {problem}", file=sys.stderr)
        return 1
    out = Path(args.output) / result.script
    write_bundle(result.files, out)
    for check in result.checks:
        print(f"  ✔ {check.name}{f' — {check.detail}' if check.detail else ''}")
    for warning in result.warnings:
        print(f"  ⚠ {warning}")
    print(f"Wrote {len(result.files)} files to {out}")
    if args.zip:
        zip_path = Path(args.output) / f"{result.script}.zip"
        zip_path.write_bytes(build_zip(result.script, result.files))
        print(f"Wrote {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
