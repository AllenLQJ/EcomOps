#!/usr/bin/env bash
# 用真实 EcomRAG 替代 RAG 桩：先在 ../EcomRAG 起好依赖与后端(:8010)，再运行本脚本。
# 适配器监听 :8002（与桩同端口），EcomOps 的超时放宽到能等 LLM 生成与 ingest 命令行。
# 注意：EcomRAG 游客每天仅 5 次问答，联调前请在其环境里调高 GUEST_DAILY_LIMIT（见 docs/决策记录.md D12）。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
export ECOMRAG_ROOT=${ECOMRAG_ROOT:-$(cd ../EcomRAG && pwd)}
curl -sf "${ECOMRAG_URL:-http://127.0.0.1:8010}/health" >/dev/null || { echo "EcomRAG 未运行（${ECOMRAG_URL:-http://127.0.0.1:8010}）"; exit 1; }
mkdir -p .run
if ! { [ -f .run/ecomrag_adapter.pid ] && kill -0 "$(cat .run/ecomrag_adapter.pid)" 2>/dev/null; }; then
  nohup $PY -m uvicorn adapters.ecomrag_adapter:app --port 8002 --log-level warning > .run/ecomrag_adapter.log 2>&1 &
  echo $! > .run/ecomrag_adapter.pid
fi
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8002/health >/dev/null && break; sleep 1; done
curl -sf http://127.0.0.1:8002/health >/dev/null && echo "up :8002 (ecomrag adapter)" || { echo "adapter FAILED (.run/ecomrag_adapter.log)"; exit 1; }
SKIP_RAG_STUB=1 ECOMOPS_RAG_SEARCH_TIMEOUT_S=${ECOMOPS_RAG_SEARCH_TIMEOUT_S:-60} ECOMOPS_RAG_CLAIM_TIMEOUT_S=${ECOMOPS_RAG_CLAIM_TIMEOUT_S:-20} \
  ECOMOPS_RAG_UPSERT_TIMEOUT_S=${ECOMOPS_RAG_UPSERT_TIMEOUT_S:-300} scripts/start_all.sh
