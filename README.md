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

```bash
# 先在 ../EcomRAG 起好依赖与后端 :8010，并调高游客配额（其 .env 里设 GUEST_DAILY_LIMIT，默认每天仅 5 次）
scripts/start_with_ecomrag.sh
```

适配器见 `adapters/ecomrag_adapter.py`。已用真实 EcomRAG 实测知识问答（命中得分 0.99；库外问题进待补充队列）；
**知识发布（入库）只有单元测试，未对真实 Milvus 实测**，避免向 EcomRAG 知识库写入测试数据。

## 全栈容器（EcomPilot + EcomOps）

```bash
# 需要 EcomPilot 与 EcomOps 是同级目录；密钥见 deploy/.env.example
docker compose -f deploy/docker-compose.full.yml up -d --build
```
