#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN_DIR="$ROOT_DIR/.run"

append_pid() {
  pid="$1"
  case " $PIDS " in
    *" $pid "*) ;;
    *) PIDS="$PIDS $pid" ;;
  esac
}

append_descendants() {
  parent="$1"
  ps -eo pid=,ppid= 2>/dev/null | while read -r pid ppid; do
    if [ "$ppid" = "$parent" ]; then
      printf '%s\n' "$pid"
      append_descendants "$pid"
    fi
  done
}

PIDS=""

for name in backend frontend; do
  pid_file="$RUN_DIR/$name.pid"
  if [ -f "$pid_file" ]; then
    pid=$(cat "$pid_file" 2>/dev/null || true)
    case "$pid" in
      ''|*[!0-9]*) ;;
      *)
        if kill -0 "$pid" 2>/dev/null; then
          append_pid "$pid"
          for child in $(append_descendants "$pid"); do
            append_pid "$child"
          done
        fi
        ;;
    esac
  fi
done

if [ -d /proc ]; then
  for proc in /proc/[0-9]*; do
    [ -d "$proc" ] || continue
    pid=${proc##*/}
    [ "$pid" != "$$" ] || continue
    cwd=$(readlink "$proc/cwd" 2>/dev/null || true)
    cmd=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)
    case "$cwd:$cmd" in
      "$ROOT_DIR/backend:"*"main.py"*|"$ROOT_DIR/backend:"*"uvicorn main:app"*|"$ROOT_DIR/frontend:"*"npm run dev"*|"$ROOT_DIR/frontend:"*"vite"*)
        append_pid "$pid"
        ;;
    esac
  done
fi

if [ -z "$PIDS" ]; then
  echo "No matching backend/frontend processes found."
  rm -f "$RUN_DIR/backend.pid" "$RUN_DIR/frontend.pid" "$RUN_DIR/frontend.port" "$RUN_DIR/start-all-parent.pid" 2>/dev/null || true
  exit 0
fi

echo "Stopping:$PIDS"
for pid in $PIDS; do
  kill "$pid" 2>/dev/null || true
done

sleep 1

for pid in $PIDS; do
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
done

rm -f "$RUN_DIR/backend.pid" "$RUN_DIR/frontend.pid" "$RUN_DIR/frontend.port" "$RUN_DIR/start-all-parent.pid" 2>/dev/null || true
echo "Done."
