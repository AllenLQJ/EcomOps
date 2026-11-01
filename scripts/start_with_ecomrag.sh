#!/usr/bin/env bash
# 用真实 EcomRAG（原生接口 /api/v1/rag/search、/internal/v1/kb/upsert）替代 RAG 桩。
# 先在 ../EcomRAG/backend 用 INTERNAL_TOKEN=<令牌> 起好后端(:8010)，再运行本脚本：
#   RAG_INTERNAL_TOKEN=<同一个令牌> scripts/start_with_ecomrag.sh
set -euo pipefail
cd "$(dirname "$0")/.."
RAG_URL=${ECOMOPS_RAG_URL:-http://127.0.0.1:8010}
: "${RAG_INTERNAL_TOKEN:?请设置 RAG_INTERNAL_TOKEN（与 EcomRAG 的 INTERNAL_TOKEN 相同）}"
curl -sf "$RAG_URL/health" >/dev/null || { echo "EcomRAG 未运行（$RAG_URL/health）"; exit 1; }
SKIP_RAG_STUB=1 ECOMOPS_RAG_URL="$RAG_URL" ECOMOPS_RAG_INTERNAL_TOKEN="$RAG_INTERNAL_TOKEN" \
  ECOMOPS_RAG_SEARCH_TIMEOUT_S=${ECOMOPS_RAG_SEARCH_TIMEOUT_S:-15} ECOMOPS_RAG_CLAIM_TIMEOUT_S=${ECOMOPS_RAG_CLAIM_TIMEOUT_S:-8} \
  ECOMOPS_RAG_UPSERT_TIMEOUT_S=${ECOMOPS_RAG_UPSERT_TIMEOUT_S:-120} scripts/start_all.sh
