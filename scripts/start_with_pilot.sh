#!/usr/bin/env bash
# 联合启动：EcomPilot 全栈（不含 Ops/RAG 桩）+ 真实 EcomOps + EcomOps 的 RAG 桩。
set -euo pipefail
cd "$(dirname "$0")/.."
PILOT_DIR=${ECOMPILOT_DIR:-../EcomPilot}
SKIP_OPS_STUB=1 SKIP_RAG_STUB=1 "$PILOT_DIR/scripts/start_all.sh"
./scripts/start_all.sh
