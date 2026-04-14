#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"
PID_DIR="$PROJECT_ROOT/.tmp"
PID_FILE="$PID_DIR/elasticsearch.pid"
LOG_FILE="$LOG_DIR/elasticsearch.log"
LOCAL_CONF_DIR="$PID_DIR/elasticsearch-config"
ES_URL="${ES_URL:-https://localhost:9200}"
ES_STARTUP_TIMEOUT="${ES_STARTUP_TIMEOUT:-180}"
ES_STARTUP_ARGS=(
  -E discovery.type=single-node
  -E xpack.security.enabled=true
  -E xpack.security.enrollment.enabled=false
  -E xpack.security.http.ssl.enabled=true
  -E xpack.security.http.ssl.key=certs/server.key
  -E xpack.security.http.ssl.certificate=certs/server.crt
  -E xpack.security.transport.ssl.enabled=true
  -E xpack.security.transport.ssl.key=certs/server.key
  -E xpack.security.transport.ssl.certificate=certs/server.crt
  -E xpack.security.transport.ssl.verification_mode=none
  -E logger.org.elasticsearch.http.HttpTracer=TRACE
)

mkdir -p "$LOG_DIR" "$PID_DIR"

prepare_local_config() {
  local source_conf_dir="$ES_HOME/config"
  rm -rf "$LOCAL_CONF_DIR"
  mkdir -p "$LOCAL_CONF_DIR/certs"
  cp -R "$source_conf_dir/." "$LOCAL_CONF_DIR/"

  # 复制证书到配置目录
  cp "$PROJECT_ROOT/backend/certs/server.crt" "$LOCAL_CONF_DIR/certs/"
  cp "$PROJECT_ROOT/backend/certs/server.key" "$LOCAL_CONF_DIR/certs/"

  cat >"$LOCAL_CONF_DIR/elasticsearch.yml" <<EOF
discovery.type: single-node
xpack.security.enabled: true
xpack.security.enrollment.enabled: false
xpack.security.http.ssl.enabled: true
xpack.security.http.ssl.key: certs/server.key
xpack.security.http.ssl.certificate: certs/server.crt
xpack.security.transport.ssl.enabled: true
xpack.security.transport.ssl.key: certs/server.key
xpack.security.transport.ssl.certificate: certs/server.crt
xpack.security.transport.ssl.verification_mode: none

# Enable request URI logging to show index names and HTTP details
logger.org.elasticsearch.http.HttpTracer: TRACE

# 启用匿名访问以维持免密连接的后向兼容性
xpack.security.authc.anonymous:
  username: anonymous_user
  roles: [superuser]
  authz_exception: true
EOF
}

if curl -kfsS "$ES_URL" >/dev/null 2>&1; then
  echo "Elasticsearch is already running at $ES_URL"
  exit 0
fi

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${existing_pid:-}" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
    echo "Elasticsearch process already running with PID $existing_pid"
    exit 0
  fi
fi

ES_BINARY="${ELASTICSEARCH_BIN:-}"
if [[ -z "$ES_BINARY" ]]; then
  if [[ -n "${ES_HOME:-}" && -x "$ES_HOME/bin/elasticsearch" ]]; then
    ES_BINARY="$ES_HOME/bin/elasticsearch"
  elif command -v elasticsearch >/dev/null 2>&1; then
    ES_BINARY="$(command -v elasticsearch)"
  elif [[ -d "$HOME/.local/elasticsearch" ]]; then
    for candidate in "$HOME"/.local/elasticsearch/elasticsearch-*/bin/elasticsearch; do
      if [[ -x "$candidate" ]]; then
        ES_BINARY="$candidate"
        break
      fi
    done
  elif command -v brew >/dev/null 2>&1; then
    for candidate in elasticsearch-full elasticsearch; do
      brew_prefix="$(brew --prefix "$candidate" 2>/dev/null || true)"
      if [[ -n "$brew_prefix" && -x "$brew_prefix/bin/elasticsearch" ]]; then
        ES_BINARY="$brew_prefix/bin/elasticsearch"
        break
      fi
    done
  fi
fi

if [[ -z "$ES_BINARY" || ! -x "$ES_BINARY" ]]; then
  cat <<'EOF'
Unable to find the Elasticsearch executable.

Set one of the following before running this script:
  - ES_HOME=/path/to/elasticsearch
  - ELASTICSEARCH_BIN=/path/to/bin/elasticsearch

If you installed via Homebrew, make sure the binary is available in PATH.
EOF
  exit 1
fi

if [[ -z "${ES_HOME:-}" ]]; then
  ES_HOME="$(cd "$(dirname "$ES_BINARY")/.." && pwd)"
fi

prepare_local_config

echo "Starting Elasticsearch with: $ES_BINARY"
echo "Logs: $LOG_FILE"

nohup env ES_PATH_CONF="$LOCAL_CONF_DIR" "$ES_BINARY" "${ES_STARTUP_ARGS[@]}" >"$LOG_FILE" 2>&1 &
es_pid=$!
echo "$es_pid" > "$PID_FILE"

for _ in $(seq 1 "$ES_STARTUP_TIMEOUT"); do
  if curl -kfsS "$ES_URL" >/dev/null 2>&1; then
    echo "Elasticsearch is up at $ES_URL"
    exit 0
  fi
  sleep 1
done

echo "Elasticsearch did not become ready in time. Check $LOG_FILE"
exit 1