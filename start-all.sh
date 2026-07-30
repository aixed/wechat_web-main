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

normalize_yaml_scalar() {
  printf '%s' "$1" |
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
      -e 's/^"//' -e 's/"$//' \
      -e "s/^'//" -e "s/'$//"
}

ensure_config() {
  cfg="$ROOT_DIR/config.yaml"
  default_key="admin"

  if [ ! -f "$cfg" ]; then
    if [ -f "$ROOT_DIR/config.example.yaml" ]; then
      log "[SETUP] config.yaml not found. Creating it from config.example.yaml."
      cp "$ROOT_DIR/config.example.yaml" "$cfg"
    else
      log "[ERROR] config.yaml and config.example.yaml were not found."
      exit 1
    fi
  fi

  raw_value=$(sed -n 's/^[[:space:]]*web_access_key[[:space:]]*:[[:space:]]*\([^#]*\).*$/\1/p' "$cfg" | head -n 1 || true)
  key_value=$(normalize_yaml_scalar "$raw_value")
  uses_default=0

  if ! grep -q '^[[:space:]]*web_access_key[[:space:]]*:' "$cfg"; then
    printf '\nweb_access_key: "%s"\n' "$default_key" >> "$cfg"
    log "[SETUP] web_access_key was not configured. Set default value: $default_key"
    uses_default=1
  elif [ -z "$key_value" ] || [ "$key_value" = "~" ] || [ "$(printf '%s' "$key_value" | tr '[:upper:]' '[:lower:]')" = "null" ]; then
    tmp_file="$RUN_DIR/config.web_access_key.tmp"
    awk -v default_key="$default_key" '
      /^[[:space:]]*web_access_key[[:space:]]*:/ && !done {
        sub(/:.*/, ": \"" default_key "\"")
        done = 1
      }
      { print }
    ' "$cfg" > "$tmp_file"
    mv "$tmp_file" "$cfg"
    log "[SETUP] web_access_key was not configured. Set default value: $default_key"
    uses_default=1
  elif [ "$key_value" = "$default_key" ]; then
    uses_default=1
  fi

  if [ "$uses_default" -eq 1 ]; then
    log "[WARN] Using default web access key: $default_key"
    log "[WARN] Please edit config.yaml and change the web_access_key parameter before exposing this service."
  fi
}

ensure_python_stack() {
  PYTHON_BASE="${PYTHON_BIN:-}"
  if [ -z "$PYTHON_BASE" ]; then
    if command -v python3.13 >/dev/null 2>&1; then
      PYTHON_BASE="python3.13"
    elif command -v python3 >/dev/null 2>&1; then
      PYTHON_BASE="python3"
    fi
  fi
  if [ -z "$PYTHON_BASE" ]; then
    ensure_apt_packages python3
    PYTHON_BASE="python3"
  fi
  export PYTHON_BASE

  py_version=$("$PYTHON_BASE" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  if [ "$py_version" != "3.13" ]; then
    log "[WARN] Python 3.13 was not found. Set PYTHON_BIN to a Python 3.13 executable to run the backend on Python 3.13."
  fi

  if ! "$PYTHON_BASE" -m venv --help >/dev/null 2>&1; then
    py_venv_pkg=$("$PYTHON_BASE" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")')
    ensure_apt_packages "$py_venv_pkg" || ensure_apt_packages python3-venv
  fi

  py_dev_pkg=$("$PYTHON_BASE" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")')
  ensure_apt_packages build-essential "$py_dev_pkg" || ensure_apt_packages build-essential python3-dev

  if [ -x "$ROOT_DIR/backend/.venv/bin/python" ]; then
    venv_version=$("$ROOT_DIR/backend/.venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if [ "$venv_version" != "$py_version" ]; then
      log "[SETUP] recreating backend virtualenv for Python $py_version"
      rm -rf "$ROOT_DIR/backend/.venv"
    fi
  fi

  if [ ! -x "$ROOT_DIR/backend/.venv/bin/python" ]; then
    log "[SETUP] creating backend virtualenv"
    "$PYTHON_BASE" -m venv "$ROOT_DIR/backend/.venv"
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

ensure_config
ensure_python_stack
ensure_node_stack

log "Starting backend..."
(
  cd "$ROOT_DIR/backend"
  "$PYTHON_CMD" main.py
) &
BACKEND_PID=$!
printf '%s\n' "$BACKEND_PID" > "$RUN_DIR/backend.pid"

sleep 2

log "Starting frontend..."
(
  cd "$ROOT_DIR/frontend"
  npm run dev
) &
FRONTEND_PID=$!
printf '%s\n' "$FRONTEND_PID" > "$RUN_DIR/frontend.pid"

echo
echo "Backend and frontend are starting."
echo "Frontend and backend host/port are configured by config.yaml."
echo "Press Ctrl+C to stop both."

wait
