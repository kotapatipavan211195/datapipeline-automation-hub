#!/usr/bin/env python3
"""Start Datapipeline Automation Hub locally on Windows, macOS or Linux.

    Windows:      run_local.bat      (double-click it, or run it in a terminal; or:  py run_local.py)
    macOS/Linux:  ./run_local.sh     (or:  python3 run_local.py)

UI http://localhost:8080 · API docs http://localhost:8080/api/docs

Uses ./.venv (created and filled from backend/requirements.txt on first run), whatever venv is active.
The server runs in this terminal until you press Ctrl+C; the browser opens once it is ready.

Options:  --port 8081      (or PORT=8081)       another port
          --no-browser     (or NO_BROWSER=1)    don't open the browser
          SF_GCP_PROJECT=my-dev-project         turn on Gemini (Vertex AI in that project)
          SF_DEV_USER_EMAIL=you@company.com     who you are in the app (no IAP locally)
All Google calls (Gemini, BigQuery dry-runs, GCS) use Application Default Credentials:
          gcloud auth application-default login [--impersonate-service-account=<the app's SA>]
Any other options are passed to uvicorn, e.g.  --log-level debug

Standard library only, so it runs before the virtualenv exists.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
REQUIREMENTS = BACKEND / "requirements.txt"
IMPORT_CHECK = "import fastapi, nicegui, black, ruff, sqlglot, jinja2, google.genai, google.cloud.bigquery, pandas, openpyxl"
MIN_PYTHON = (3, 10)
WINDOWS = os.name == "nt"
LAUNCHER = "run_local.bat" if WINDOWS else "./run_local.sh"

# Talk to localhost directly, never through a corporate HTTP proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def say(message: str) -> None:
    print(f"\n  {message}\n", flush=True)


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if WINDOWS else "bin/python")


def is_hub_running(port: int, timeout: float = 1.0) -> bool:
    try:
        with _LOCAL.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return b'"status":"ok"' in resp.read()
    except Exception:  # refused, timed out, or something that isn't the hub
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop_hint(port: int) -> str:
    if WINDOWS:
        return f"netstat -ano | findstr :{port}   then   taskkill /PID <pid> /T /F"
    return f"kill $(lsof -t -iTCP:{port} -sTCP:LISTEN)"


def ensure_venv(venv: Path) -> Path:
    python = venv_python(venv)
    if not python.exists():
        if sys.version_info < MIN_PYTHON:
            raise SystemExit(
                f"Datapipeline Automation Hub needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer (this is {sys.version.split()[0]}).\n"
                "Install one from https://www.python.org/downloads/ and run this again."
            )
        print(f"Creating virtualenv {venv}", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    print(f"Checking dependencies in {venv} ...", flush=True)
    if subprocess.run([str(python), "-c", IMPORT_CHECK], capture_output=True).returncode != 0:
        print("Installing dependencies from backend/requirements.txt (first run only, takes a minute or two)", flush=True)
        install = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]
        if subprocess.run(install).returncode != 0:
            raise SystemExit("\nInstalling dependencies failed; see the pip messages above.")
    return python


def announce_when_ready(server: subprocess.Popen, port: int, url: str, open_browser: bool) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.poll() is not None:  # failed to start: its error is already on screen
            return
        if is_hub_running(port):
            say(f"✔ Datapipeline Automation Hub is ready → {url}    (this terminal now shows the server log; Ctrl+C stops the server)")
            if open_browser:
                webbrowser.open(url)
            return
        time.sleep(0.5)
    say(f"Datapipeline Automation Hub did not answer on {url} after 2 minutes; check the messages above.")


def main(argv: list[str] | None = None) -> int:
    try:  # never crash on a console that can't show ✔ / →
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="Start Datapipeline Automation Hub locally. Unknown options are passed to uvicorn.", allow_abbrev=False
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8080), help="default 8080 (or $PORT)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        default=os.environ.get("NO_BROWSER", "").lower() not in ("", "0", "false", "no"),
        help="don't open the browser",
    )
    parser.add_argument(
        "--venv", type=Path, default=Path(os.environ.get("VENV") or ROOT / ".venv"), help="virtualenv to use (default ./.venv)"
    )
    args, uvicorn_args = parser.parse_known_args(argv)
    url = f"http://localhost:{args.port}"

    if is_hub_running(args.port, timeout=2):
        say(f"Datapipeline Automation Hub is already running at {url} (started from another terminal). Open that address in your browser.")
        if not args.no_browser:
            webbrowser.open(url)
        print(f"  To stop it: press Ctrl+C in the terminal where it runs, or:  {stop_hint(args.port)}", flush=True)
        return 0
    if port_in_use(args.port):
        say(f"Port {args.port} is used by another program. Start on another port:  {LAUNCHER} --port {args.port + 1}")
        return 1

    python = ensure_venv(args.venv)
    env = dict(os.environ)
    env.setdefault("SF_LOG_FORMAT", "text")
    env["PYTHONUTF8"] = "1"  # UTF-8 files, console and subprocess pipes on Windows too
    command = [
        str(python), "-m", "uvicorn", "app.main:create_app", "--factory",
        "--host", "127.0.0.1", "--port", str(args.port),
        # Watch only source + templates, so saving processes / generating bundles never restarts the server.
        "--reload", "--reload-dir", "app", "--reload-dir", "templates", "--reload-include", "*.j2",
        *uvicorn_args,
    ]

    say(f"Starting Datapipeline Automation Hub on {url} ...")
    server = subprocess.Popen(command, cwd=BACKEND, env=env)  # cwd: the 'app' package lives in backend/
    threading.Thread(target=announce_when_ready, args=(server, args.port, url, not args.no_browser), daemon=True).start()
    try:
        while True:
            try:
                return server.wait(timeout=0.5)  # short waits keep Ctrl+C responsive on Windows
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        # Ctrl+C reached the server as well; give it time to shut down cleanly.
        try:
            server.wait(timeout=20)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            server.kill()
        print("Datapipeline Automation Hub stopped.", flush=True)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # Ctrl+C while installing
        sys.exit(130)
