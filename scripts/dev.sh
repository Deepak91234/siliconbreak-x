#!/bin/zsh
# Start CircuitLoop for local development: FastAPI backend (port 8000) + Vite dev server (port 5173).
#
# The Vivado container is NOT started by this script. CircuitLoop reuses a running
# `vivado_container` (your usual start_container.sh session) or, after you approve
# it in the dashboard, starts its own headless `circuitloop_vivado` container.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "Python venv missing: /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 1
fi
if [[ ! -d frontend/node_modules ]]; then
  echo "Installing frontend dependencies..."
  (cd frontend && npm install)
fi

.venv/bin/python -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000 &
BACKEND=$!
(cd frontend && npm run dev -- --host 127.0.0.1 --port 5173) &
FRONTEND=$!

trap 'kill $BACKEND $FRONTEND 2>/dev/null; wait 2>/dev/null' INT TERM EXIT
echo "CircuitLoop: http://127.0.0.1:5173  (API http://127.0.0.1:8000/api/health)"
wait
