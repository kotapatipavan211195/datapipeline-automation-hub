#!/usr/bin/env bash
# Start Datapipeline Automation Hub locally (macOS/Linux):  ./run_local.sh   →  UI http://localhost:8080   API docs http://localhost:8080/api/docs
# Windows: run_local.bat. Both call run_local.py, which has the options (--port 8081, --no-browser, SF_GCP_PROJECT=...).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The launcher is standard-library Python; use the project venv if it exists, else the newest Python 3.10+ found.
for candidate in "$ROOT/.venv/bin/python" python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    exec "$candidate" "$ROOT/run_local.py" "$@"
  fi
done
echo "Datapipeline Automation Hub needs Python 3.10 or newer: https://www.python.org/downloads/" >&2
exit 1
