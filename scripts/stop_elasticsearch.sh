#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$PROJECT_ROOT/.tmp"
PID_FILE="$PID_DIR/elasticsearch.pid"
ES_URL="${ES_URL:-http://localhost:9200}"

kill_tree() {
  local pid="$1"
  local signal="${2:-TERM}"

  local children
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  for child_pid in $children; do
    kill_tree "$child_pid" "$signal"
  done

  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "-$signal" "$pid" >/dev/null 2>&1 || true
  fi
}

wait_for_exit() {
  local pid="$1"
  local attempts="${2:-20}"

  for _ in $(seq 1 "$attempts"); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  return 1
}

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
    echo "Stopping Elasticsearch process tree rooted at PID $pid"
    kill_tree "$pid" TERM

    if wait_for_exit "$pid" 20; then
      rm -f "$PID_FILE"
      echo "Elasticsearch stopped"
      exit 0
    fi

    echo "Process did not exit cleanly, sending SIGKILL"
    kill_tree "$pid" KILL
    wait_for_exit "$pid" 10 || true
    rm -f "$PID_FILE"
    echo "Elasticsearch forced to stop"
    exit 0
  fi

  echo "Removing stale PID file: $PID_FILE"
  rm -f "$PID_FILE"
fi

listener_pids="$(lsof -ti tcp:9200 2>/dev/null || true)"
if [[ -n "$listener_pids" ]]; then
  echo "No PID file found, but a process is still listening on $ES_URL"
  for pid in $listener_pids; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      echo "Stopping listener PID $pid"
      kill -TERM "$pid" >/dev/null 2>&1 || true
    fi
  done

  sleep 3
  listener_pids="$(lsof -ti tcp:9200 2>/dev/null || true)"
  if [[ -n "$listener_pids" ]]; then
    for pid in $listener_pids; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        echo "Force stopping listener PID $pid"
        kill -KILL "$pid" >/dev/null 2>&1 || true
      fi
    done
  fi

  echo "Elasticsearch stopped via port listener cleanup"
  exit 0
fi

echo "Elasticsearch is not running"
exit 0