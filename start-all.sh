#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN_DIR="$ROOT_DIR/.run"
mkdir -p "$RUN_DIR"

APT_UPDATED=0

log() {
  printf '%s\n' "$*"
}

apt_install() {
  if ! command -v apt-get >/dev/null 2>&1; then
    log "[ERROR] Missing dependency: $*. apt-get is not available on this system."
    exit 1
  fi

  if [ "$APT_UPDATED" -eq 0 ]; then
    log "[SETUP] apt-get update"
    DEBIAN_FRONTEND=noninteractive apt-get update
    APT_UPDATED=1
  fi

  log "[SETUP] apt-get install -y $*"
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

package_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

ensure_apt_packages() {
  missing=""
  for pkg in "$@"; do
    if ! package_installed "$pkg"; then
      missing="$missing $pkg"
    fi
  done
  if [ -n "$missing" ]; then
    # shellcheck disable=SC2086
    apt_install $missing
  fi
}

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    cksum "$1"
  fi
}

ensure_python_stack() {
  if ! command -v python3 >/dev/null 2>&1; then
    ensure_apt_packages python3
  fi

  if ! python3 -m venv --help >/dev/null 2>&1; then
    py_venv_pkg=$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")')
    ensure_apt_packages "$py_venv_pkg" || ensure_apt_packages python3-venv
  fi

  py_dev_pkg=$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")')
  ensure_apt_packages build-essential "$py_dev_pkg" || ensure_apt_packages build-essential python3-dev

  if [ ! -x "$ROOT_DIR/backend/.venv/bin/python" ]; then
    log "[SETUP] creating backend virtualenv"
    python3 -m venv "$ROOT_DIR/backend/.venv"
  fi

  PYTHON_CMD="$ROOT_DIR/backend/.venv/bin/python"
  export PYTHON_CMD

  req_hash=$(hash_file "$ROOT_DIR/backend/requirements.txt")
  req_stamp="$RUN_DIR/backend.requirements.sha256"

  if [ ! -f "$req_stamp" ] || [ "$(cat "$req_stamp" 2>/dev/null || true)" != "$req_hash" ] ||
    ! "$PYTHON_CMD" -c "import fastapi, uvicorn, httpx, yaml, requests, PIL, lz4" >/dev/null 2>&1; then
    log "[SETUP] installing backend Python dependencies"
    "$PYTHON_CMD" -m pip install --upgrade pip setuptools wheel
    "$PYTHON_CMD" -m pip install -r "$ROOT_DIR/backend/requirements.txt"
    printf '%s\n' "$req_hash" > "$req_stamp"
  fi
}

ensure_node_stack() {
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    ensure_apt_packages nodejs npm
  fi

  pkg_hash=$(hash_file "$ROOT_DIR/frontend/package-lock.json")
  pkg_stamp="$RUN_DIR/frontend.package-lock.sha256"

  if [ ! -d "$ROOT_DIR/frontend/node_modules" ] || [ ! -f "$pkg_stamp" ] ||
    [ "$(cat "$pkg_stamp" 2>/dev/null || true)" != "$pkg_hash" ]; then
    log "[SETUP] installing frontend npm dependencies"
    (cd "$ROOT_DIR/frontend" && npm install)
    printf '%s\n' "$pkg_hash" > "$pkg_stamp"
  fi
}

is_port_free() {
  "$PYTHON_CMD" - "$1" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

choose_frontend_port() {
  configured_port="${FRONTEND_PORT:-}"
  if [ -z "$configured_port" ] && [ -f "$ROOT_DIR/config.yaml" ]; then
    configured_port=$("$PYTHON_CMD" - "$ROOT_DIR/config.yaml" <<'PY'
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
value = data.get("frontend_port")
if value in (None, ""):
    raise SystemExit(0)
try:
    port = int(value)
except (TypeError, ValueError):
    raise SystemExit("frontend_port must be an integer")
if not 1 <= port <= 65535:
    raise SystemExit("frontend_port must be between 1 and 65535")
print(port)
PY
)
  fi

  if [ -n "$configured_port" ]; then
    if is_port_free "$configured_port"; then
      FRONTEND_PORT="$configured_port"
      export FRONTEND_PORT
      return
    fi
    log "[ERROR] Frontend port $configured_port is already in use."
    exit 1
  fi

  port="3001"
  while [ "$port" -le 3999 ]; do
    if is_port_free "$port"; then
      FRONTEND_PORT="$port"
      export FRONTEND_PORT
      return
    fi
    port=$((port + 1))
  done
  log "[ERROR] No free frontend port found in 3000-3999."
  exit 1
}

descendants() {
  parent="$1"
  ps -eo pid=,ppid= 2>/dev/null | while read -r pid ppid; do
    if [ "$ppid" = "$parent" ]; then
      printf '%s\n' "$pid"
      descendants "$pid"
    fi
  done
}

cleanup() {
  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    if [ -n "$pid" ]; then
      for child in $(descendants "$pid"); do
        kill "$child" 2>/dev/null || true
      done
      kill "$pid" 2>/dev/null || true
    fi
  done
  rm -f "$RUN_DIR/backend.pid" "$RUN_DIR/frontend.pid"
}
trap cleanup INT TERM EXIT

if [ ! -f "$ROOT_DIR/config.yaml" ]; then
  log "[WARN] config.yaml not found. Please create it before starting the backend."
fi

ensure_python_stack
ensure_node_stack
choose_frontend_port

log "Starting backend..."
(
  cd "$ROOT_DIR/backend"
  "$PYTHON_CMD" main.py
) &
BACKEND_PID=$!
printf '%s\n' "$BACKEND_PID" > "$RUN_DIR/backend.pid"

sleep 2

log "Starting frontend on port $FRONTEND_PORT..."
(
  cd "$ROOT_DIR/frontend"
  FRONTEND_PORT="$FRONTEND_PORT" npm run dev
) &
FRONTEND_PID=$!
printf '%s\n' "$FRONTEND_PID" > "$RUN_DIR/frontend.pid"
printf '%s\n' "$FRONTEND_PORT" > "$RUN_DIR/frontend.port"

echo
echo "Backend and frontend are starting."
echo "Frontend: http://127.0.0.1:$FRONTEND_PORT"
echo "Backend:  http://127.0.0.1:5000"
echo "Press Ctrl+C to stop both."

wait
