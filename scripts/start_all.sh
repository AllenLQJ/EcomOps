#!/usr/bin/env bash
# 启动 EcomOps：PostgreSQL(docker) + 建库导入 schema + RAG 桩(:8002) + EcomOps(:8000)。日志与 pid 在 .run/。
# 联合 EcomPilot：先 `SKIP_OPS_STUB=1 SKIP_RAG_STUB=1 ../EcomPilot/scripts/start_all.sh`，再运行本脚本；或直接 scripts/start_with_pilot.sh。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
mkdir -p .run
export ECOMOPS_DB_NAME=${ECOMOPS_DB_NAME:-ecomops}
export ECOMOPS_DEV_TOKENS=1
export ECOMOPS_ENABLE_FAULTS=${ECOMOPS_ENABLE_FAULTS:-1}

docker compose -f deploy/docker-compose.yml up -d postgres >/dev/null 2>&1
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' ecomops-postgres)" = healthy ] && break; sleep 2
done
if [ "${RESET_DB:-0}" = 1 ]; then
  docker exec ecomops-postgres psql -U postgres -c "DROP DATABASE IF EXISTS \"$ECOMOPS_DB_NAME\" WITH (FORCE)" >/dev/null
fi
if ! docker exec ecomops-postgres psql -U postgres -tAc "select 1 from pg_database where datname='$ECOMOPS_DB_NAME'" | grep -q 1; then
  docker exec ecomops-postgres psql -U postgres -c "CREATE DATABASE \"$ECOMOPS_DB_NAME\"" >/dev/null
  docker exec -i ecomops-postgres psql -U postgres -d "$ECOMOPS_DB_NAME" -q < contracts/schema.sql >/dev/null
  echo "db initialised: $ECOMOPS_DB_NAME"
fi

start() { # name module port
  [ -f .run/$1.pid ] && kill -0 "$(cat .run/$1.pid)" 2>/dev/null && { echo "$1 already running"; return; }
  nohup $PY -m uvicorn "$2" --port "$3" --log-level warning > .run/$1.log 2>&1 &
  echo $! > .run/$1.pid
}
[ "${SKIP_RAG_STUB:-0}" = 1 ] || start rag_stub stubs.rag_stub:app 8002
start ecomops ecomops.main:app 8000
PORTS="8000"; [ "${SKIP_RAG_STUB:-0}" = 1 ] || PORTS="8002 8000"
for p in $PORTS; do
  for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$p/health" >/dev/null && break; sleep 1; done
  curl -sf "http://127.0.0.1:$p/health" >/dev/null && echo "up :$p" || { echo "FAILED :$p (see .run/*.log)"; exit 1; }
done
