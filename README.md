# EcomOps · 智购中台

B 端风控审批与运营实训平台，EcomPilot（C 端交易）的对端服务。FastAPI + LangGraph 1.0.9 + PostgreSQL 15 + Vue 3（CDN 单页）。
规格见 EcomPilot 仓库 `ecompilot_project_roadmap.md`（项目四）。实现深度与未完成项见 [交付报告](docs/交付报告.md)，执行计划见 [开发执行计划](docs/开发执行计划.md)。

## 四条工作流
| 工作流 | 内容 |
|---|---|
| claim_audit | 售后三轨并发审核 → 条件路由（自动驳回/补材料/自动批准/主管复核）→ `interrupt` 挂起（AsyncPostgresSaver）→ 主管批准/改额/驳回/退回 → Outbox 回调 EcomPilot 退款 → 超时对账 |
| kb_qa | 查询分类、检索（RAG 桩）、低分(<0.75)入队、待补录审核发布状态机（确定性 chunk_id，幂等 upsert） |
| listing_compliance_review | PDF → 文本抽取 → 五类检查器并发 → 三层判定（阻断/待核实/建议） |
| cs_training | 客服五阶段抗压对练（SSE，追问≤2，记忆摘要） |

## 运行
前置：Docker、`uv`。EcomPilot 位于同级目录 `../EcomPilot` 并已建好 venv。
```sh
uv venv --python 3.12 .venv && uv pip install -r requirements.txt --python .venv/bin/python
./scripts/start_with_pilot.sh   # EcomPilot 全栈(不含 Ops/RAG 桩) + PostgreSQL(5433) + RAG 桩(8002) + EcomOps(8000)
./scripts/smoke.sh              # 联合冒烟：Pilot 下单→发货→售后→真实 EcomOps 审核→主管批准→回调→退款
.venv/bin/python scripts/hardening_check.py   # 故障注入：Pilot 宕机恢复、SIGKILL 重启、并发批准、降级、发布回写失败
./scripts/stop_all.sh           # 停服务（EcomPilot 另用其 scripts/stop_all.sh）
```
单独启动 EcomOps：`./scripts/start_all.sh`。浏览器打开 http://127.0.0.1:8000/ 即管理端（`ECOMOPS_DEV_TOKENS=1` 时可在页面获取开发令牌）。

| 端口 | 服务 |
|---|---|
| 8000 | EcomOps API + 管理页 |
| 8002 | RAG 桩（含 `/internal/v1/kb/upsert`） |
| 5433 | PostgreSQL 15（docker） |

环境变量（前缀 `ECOMOPS_`）：`DB_HOST/PORT/USER/PASSWORD/NAME`、`JWT_SECRET`、`INTERNAL_TOKEN`、`PILOT_URL`、`PILOT_INTERNAL_TOKEN`、`RAG_URL`、`DEV_TOKENS`、`ENABLE_FAULTS`、`RECONCILE_INTERVAL_SECONDS`、`OUTBOX_MAX_ATTEMPTS`、`AUTO_APPROVE_MAX_CENTS`、`MINIO_ENDPOINT`（未配置或不可达则用本地文件）。

## 测试
```sh
.venv/bin/python -m pytest tests -q   # unit + acceptance（含 AC-TX-04 完整版、AC-KB-01、契约一致性）；需 PostgreSQL，AC-TX-04 还需 ../EcomPilot 与 MySQL(3307)
```
每个测试一库 `ecomops_test_<uuid8>`，结束删除。契约在 `contracts/`（OpenAPI、schema、错误码、模块接口约定）。


## 接入真实 EcomRAG（可选，替代 RAG 桩）

EcomRAG 已提供原生接口（`POST /api/v1/rag/search`、`POST /internal/v1/kb/upsert`，需 `X-Internal-Token`），EcomOps 直接调用，不再经过适配器：

```bash
# 1) ../EcomRAG/backend：INTERNAL_TOKEN=<令牌> uv run uvicorn app.main:create_app --factory --port 8010
# 2) 本仓库：
RAG_INTERNAL_TOKEN=<同一个令牌> scripts/start_with_ecomrag.sh
```

检索返回的 `score` 是 EcomRAG 按余弦校准后的相关度（库外问题低、会进待补充队列）。已实测：库内问题 top1=1.0，库外问题 0.37–0.65 进待补充队列。
**知识发布（upsert）已在 EcomRAG 一侧对临时集合验证过；EcomOps → 真实 `kb_chunks` 的发布没做**，避免向真实知识库写入测试数据。EcomRAG 当前不按 tenant_id / catalog_category_id 过滤。

## 全栈容器（EcomPilot + EcomOps）

```bash
# 需要 EcomPilot 与 EcomOps 是同级目录；密钥见 deploy/.env.example
docker compose -f deploy/docker-compose.full.yml up -d --build
```


## 工作流、评测与存储（补充）

- 四条 LangGraph 工作流（售后审核 / 知识问答 / 合规审查 / 客服对练）共用同一个 `AsyncPostgresSaver`，约定见 `contracts/module_interfaces.md` 第 8 节。
- 可选的真实 LLM：`ECOMOPS_LLM_BASE_URL` / `ECOMOPS_LLM_API_KEY` / `ECOMOPS_LLM_MODEL` 三项都配才联网；联网搜索 `ECOMOPS_WEB_SEARCH=on`（默认关）。两者都只做了 mock 测试。
- 离线评测：`python scripts/offline_eval.py [--rag-url http://127.0.0.1:8010 --rag-token <令牌>] [--out docs/离线评测报告.md]`。
- 超时未补材料自动撤回：`ECOMOPS_EVIDENCE_DEADLINE_HOURS`（默认 72）。
- 测试依赖：`pip install -r requirements-dev.txt`（含 S3 仿真服务 moto，用于验证 MinIO 后端）。
