# 模块间接口（冻结）

## 1. 路由挂载
外围模块在 `ecomops/<模块>/router.py` 暴露 `router: fastapi.APIRouter`（含完整路径），由统筹的 `ecomops/main.py` 统一挂载：
`ecomops/claims/router.py`（统筹）、`ecomops/kb/router.py`、`ecomops/listing/router.py`、`ecomops/training/router.py`。

## 2. core 能力（只读使用，不得修改 ecomops/core/）
- `ecomops.core.db.get_engine()` → SQLAlchemy `AsyncEngine`（psycopg3，按事件循环缓存）。用法：`async with get_engine().begin() as conn: await conn.execute(text("... :x"), {"x": 1})`；JSONB 写入用 `CAST(:v AS jsonb)` 并传 `json.dumps(...)`；读取 JSONB 得到已解析对象。
- `ecomops.core.auth`：`current_identity`（→ `Identity(user_id, tenant_id, role)`）、`require_roles("supervisor","operator")`、`require_internal`、`issue_token(user_id, role, tenant_id)`。角色：supervisor / operator / cs_agent。
- `ecomops.core.errors.ApiError(status, code, message, details)`，错误码仅用 `errors.md`。
- `ecomops.core.storage.get_store()` → `.put(name, bytes, content_type) -> path`、`.get(path) -> bytes`。
- `ecomops.core.llm.should_fail(task) -> bool`：故障注入；为 True 时该"LLM 步骤"必须走降级并置 `fallback_used=True`。
- `ecomops.core.config.get_settings()`；所有外部 URL 运行时读取。
- 测试：`tests/conftest.py` 的 `db_name` fixture（独立库+schema）；应用级测试用 `fastapi.testclient.TestClient`，请求头用 `issue_token`。

## 3. RAG 桩（`stubs/rag_stub.py`，:8002，T1-A 实现）
- `POST /api/v1/rag/search` `{query, tenant_id?, catalog_category_id?, top_k?}` → `{hits:[{chunk_id,text,rule_source,is_demo_rule,score}]}`（score 降序）。内置沙箱演示规则（热水器退换、7 天无理由等）+ 通过 upsert 写入的 chunk 也可被检索（关键词重叠度打分，确定性；已发布 chunk 命中同问题时 score ≥ 0.9）。
- `POST /internal/v1/kb/upsert` `{chunk_id, tenant_id, catalog_category_id, text, rule_source, is_demo_rule?, effective_from?, effective_to?, version}` → `{chunk_id, milvus_pk}`：以 chunk_id 为主键**幂等覆盖**（等价 Milvus upsert），重复调用不产生重复向量。
- `GET /stub/admin/chunks?chunk_id=` → `{chunk_id, count}`（count∈{0,1}）；`GET /stub/admin/chunks/count` → `{total}`。
- `POST /stub/admin/fault` `{mode: none|fail_after_write|fail_before_write}`：fail_after_write=写入成功后返回 500（模拟"Milvus 已写入、响应丢失"），fail_before_write=不写入直接 500。
- `GET /health`。

## 4. kb_qa 行为（`ecomops/kb`，T1-A）
- 分类 `classify_query(query) -> GENERAL|PRECISE|VAGUE|BROAD`（规则：含"你好/谢谢/天气"等→GENERAL；含具体条款词"几天/运费/保修/退换/7天"等→PRECISE；含多个并列问号/"以及/还有/顺便"→BROAD；否则 VAGUE）。PRECISE top_k=8；VAGUE 先做 HyDE 式改写再 top_k=10；BROAD 拆≤3 子查询各召回 4 条合并。
- `top1_score >= 0.75` → 用命中片段生成回答并带 `[rule_source/沙盒演示标记]`；否则兜底回答 + **入队** `knowledge_pending_queue`（同租户同问题 PENDING_REVIEW 唯一，重复提问不重复入队，返回已有 queue_id）。GENERAL 不检索不入队。
- RAG 不可用/超时 → 回答兜底文本，`top1_score=0`，仍入队，不抛 500。
- `qa_sessions`：每轮必定 UPSERT；追加 AI 回复**之后**，当 `len(messages) % 20 == 0`（偶数轮精准触发）生成摘要（确定性：取前若干条用户消息拼接截断）。
- 发布状态机：`PENDING_REVIEW|PUBLISH_FAILED --publish--> PUBLISHING`（同一事务保存答案草稿、`version+=1`（仅首次发布递增；retry 不递增）、`deterministic_chunk_id=sha256(f"{queue_id}:{version}")`、提交）→ 调 RAG upsert → 成功回写 `PUBLISHED`；超时/异常回写 `PUBLISH_FAILED` 并记 `last_error`。**回写失败**（`faults.publish_writeback_fail` 注入）→ 条目停留 `PUBLISHING`。`retry` 允许从 `PUBLISHING`/`PUBLISH_FAILED` 重新调用 upsert，chunk_id 不变，成功→`PUBLISHED`。`PUBLISHED`/`DISCARDED` 再 publish → 409 PUBLISH_STATE_CONFLICT。
- 故障注入：`ecomops/kb/faults.py` 暴露模块级 `state = {"publish_writeback_fail": False}`；统筹的 `/internal/v1/faults` 会设置它。

## 5. listing（`ecomops/listing`，T1-B）
- `POST /api/v1/listing/reviews`（operator/supervisor）multipart：非 PDF/空文件→400 UNSUPPORTED_FILE。流程：`get_store().put` → PyMuPDF 抽文本（`fitz`，按 block 位置排序处理双栏）→ 结构化抽取（标题、参数表、功效宣称、证书编号）→ 5 个检查器 `asyncio.gather` 并发：①极限词（最、第一、国家级、100%、永久、根治…）②declared_category 必填参数完整度（内置各类目必填属性表，如 HOME_APPLIANCE_HEATER 需「额定功率、容量、能效等级、3C」）③医疗/虚假功效（防脱、生发、治疗、治愈…）④资质编号提取（3C/备案号/质检报告号的格式校验，格式不符→待核实）⑤可读性建议。
- 判定（严禁加权分抵消）：任一阻断项（①命中、③命中、②关键参数缺失）→ `REJECTED_BLOCKING`；否则有待核实项（④格式存疑）**或任一检查器降级（`llm.should_fail(检查器名)`，名：extreme_words/required_params/medical_claims/cert_extract/readability；降级置该项 fallback_used，整体 `fallback_used=True`）**→ `PENDING_MANUAL_VERIFY`；否则 `PASSED_WITH_SUGGESTIONS`。持久化 `listing_reviews`，响应见 `ListingReport`。

## 6. training（`ecomops/training`，T1-C）
- 创建会话（cs_agent/operator/supervisor）：`scenario_seed` 缺省用内置（热水器漏水）；阶段序列 `verify_order → empathy_listen → policy_explain → solution_negotiate → finished`。
- `chat/stream`（SSE：`event: token`（逐段）、`event: stage`（`{"stage","advanced","follow_up_count"}`）、`event: report`（结束时）、`event: done`）：`evaluate_answer` 按当前阶段关键词判定达成（verify：含订单号/手机尾号/姓名等核实词；empathy：含"抱歉/理解/着急"等且不被误杀；policy：引用条款词"7天/15天/质量问题/退换/运费"；negotiate：含具体方案词"退款/换货/补偿/补发"）。未达成且 `follow_up_count<2` → 保持阶段、`follow_up_count+=1`；达成或追问满 2 次 → 推进并清零。第 4 阶段完成 → `finished`，生成报告（规则准确度/情绪安抚力/方案合规度，0~100 整数）写入 `report` 与 `status='FINISHED'`。已结束会话再发消息→422 INVALID_STAGE。会话只能被创建者（同租户同用户）访问，否则 404。
- 记忆：每轮先把本轮 AI 回复追加进完整 `messages`，再判断 `len(messages) % 20 == 0` 触发摘要（偶数轮精准），**无论是否摘要，每轮都 UPSERT 保存最新状态**。

## 7. claim_audit 行为规格（统筹实现；Cursor 验收依据）

**提交**：`POST /internal/v1/claims/submit` 按 `claim_no` 幂等（重复提交返回 202 + `idempotent_replayed=true`，不重复启动审核）。同步跑到图的下一个停靠点（auto 终态 / 自动驳回 / 补材料 / 挂起 interrupt / 已批准待回调），响应 `{claim_no, status}` 为该时刻的 `claim_status`。**退款回调不在 submit 请求内同步执行**，而由出箱 worker 异步完成（立即触发 + 周期扫描）。

**三轨**（asyncio.gather 并发）：
- RULE：`passed = days_since_delivery <= 15`；detail 含 `window_days=15`。
- EVIDENCE：质量关键词（漏水/打不着火/不制热/坏了/破损/故障/质量/变质）命中 `quality_claim`；`evidence_ok = len(evidence_urls) >= 1`；`confidence = 0.5 + 0.3*quality_claim + 0.2*evidence_ok`；`passed = quality_claim and evidence_ok`；`evidence_missing = quality_claim and not evidence_ok`。`llm.should_fail("evidence_track")` → 该轨 `fallback_used=True, confidence=0, passed=false`。
- FEE：`suggested_refund_cents = min(item_payable_cents, max_refundable_cents)`；`restock_policy = DEFECTIVE_WAREHOUSE if quality_claim else SELLABLE_WAREHOUSE`；`passed=true`。
- 工单 `fallback_used` = 任一轨降级；`confidence` 取 EVIDENCE 轨。

**路由**（按序）：
1. RULE 不通过 且 非（quality_claim 且 evidence_ok）→ `AUTO_REJECTED`。
2. `evidence_missing` → `NEED_MORE_EVIDENCE`。
3. `suggested ≤ 5000` 且三轨全 passed 且 `confidence ≥ 0.85` 且 `fallback_used=False` → 自动批准：写 `claim_approvals(action=AUTO_APPROVE)` 与 `outbox_events`（同一事务）→ `APPROVED_PENDING_CALLBACK`，`risk_level=LOW`。
4. 其余 → `PENDING_SUPERVISOR_REVIEW`（LangGraph `interrupt()`，检查点落 PostgreSQL），`risk_level=HIGH`，`route_reason` 写明原因（大额/低置信/降级/规则冲突）。
示例：热水器 112000 分 + 漏水原因 + 1 张凭证 + 签收 5 天 → 三轨通过但金额>5000 → 挂起主管复核。

**主管决定** `POST /api/v1/claims/{claim_no}/confirm`（仅 supervisor，租户内）：
- `APPROVE` → 金额取 `suggested_refund_cents`；`MODIFY_AMOUNT` → 必须 `0 < amount_cents <= max_refundable_cents` 否则 422 AMOUNT_EXCEEDS_LIMIT；二者在**同一事务**写 `claim_approvals` + `outbox_events`（`refund_idempotency_key = "REFUND_"+claim_no+"_V1"`，payload 即 Pilot `refunds/execute` 请求体）并置 `APPROVED_PENDING_CALLBACK`（`approved_refund_cents` 同步写入）。
- `REJECT` → `SUPERVISOR_REJECTED`；`REQUEST_EVIDENCE` → `NEED_MORE_EVIDENCE`。
- 状态非 `PENDING_SUPERVISOR_REVIEW`：若请求决定与已记录的最近一次主管决定**相同**（action 相同，MODIFY_AMOUNT 还需金额相同）→ 200 `idempotent_replayed=true`；否则 409 CLAIM_STATE_CONFLICT。并发两次相同批准：恰好一条 approvals、一条 outbox。
- 非 supervisor → 403 FORBIDDEN_ROLE；他租户工单 → 404。
- 恢复（`Command(resume=...)`）从 AsyncPostgresSaver 读取挂起状态；**EcomOps 进程被 SIGKILL 重启后仍可 confirm**。

**补材料/撤回**（内部接口）：`evidence` 仅在 `NEED_MORE_EVIDENCE` 有效（否则 409）→ 追加 evidence_urls、`audit_round+=1`、重新三轨审核（新线程 `claim_no:r<round>`）并重新路由；`withdraw` 在 `NEED_MORE_EVIDENCE|PENDING_SUPERVISOR_REVIEW` 有效 → `WITHDRAWN`。

**回调与对账（Outbox）**：worker 取 `PENDING|PENDING_RETRY` 且 `next_retry_at<=now` 的事件，`FOR UPDATE SKIP LOCKED`，对**每个事件**：先 `GET {pilot}/internal/v1/refunds/status?refund_idempotency_key=`（`SUCCESS` → 直接 DONE，不再 POST）；`NOT_FOUND` 或首次 → `POST {pilot}/internal/v1/refunds/execute`（超时 `refund_callback_timeout_seconds`）；成功（200 且 `execution_status=SUCCESS`）→ 事件 `DONE`、工单 `COMPLETED`；超时/连接错/5xx → 事件 `PENDING_RETRY`（`attempts+=1`，`next_retry_at=now+backoff`）、工单 `CALLBACK_RETRYING`；`attempts >= outbox_max_attempts` → 事件 `FAILED_MANUAL`、工单 `CALLBACK_FAILED_MANUAL`；Pilot 返回 4xx（业务拒绝）→ 直接 `FAILED_MANUAL`。后台协程每 `reconcile_interval_seconds` 扫描；`POST /internal/v1/outbox/run-once` 立即扫描一次。**无论回调是否超时，Pilot 的 refund_executions 对同一 key 只有 1 条。**

**终态通知（best-effort）**：`AUTO_REJECTED/SUPERVISOR_REJECTED/WITHDRAWN` 时写 `outbox_events(event_type='CLAIM_RESULT', refund_idempotency_key="RESULT_"+claim_no+"_"+status)`，worker 调 `POST {pilot}/internal/v1/aftersales/{claim_no}/result {"result":"REJECTED"|"WITHDRAWN"}`（Pilot 侧由统筹补充实现，使订单恢复 FULFILLED）。

**查询**：`GET /api/v1/claims`（supervisor/operator，`status` 过滤）、`GET /api/v1/claims/{claim_no}`（含 tracks/approvals/outbox）。
**故障注入**：`POST /internal/v1/faults`（`ECOMOPS_ENABLE_FAULTS=1`）`{"llm_fail": ["evidence_track"], "publish_writeback_fail": true}`；`{}` 清空。
**开发令牌**：`ECOMOPS_DEV_TOKENS=1` 时 `POST /api/v1/dev/token {user_id, role, tenant_id?}`。

## 8. LangGraph 工作流约定（kb_qa / listing_compliance_review / cs_training）

四条工作流都是 `StateGraph`，与 `claim_audit` 共用 `main.py` lifespan 里的同一个 `AsyncPostgresSaver`：模块暴露 `build_graph(checkpointer)`，`main.py` 把编译好的图挂到 `app.state.<name>_graph`。对外 HTTP 契约不因改图而变化。

| 图 | 模块 | thread_id | 节点副作用幂等 |
|---|---|---|---|
| claim_audit | `ecomops/claims/graph.py` | `<claim_no>:r<round>` | `step_executions`（既有） |
| kb_qa | `ecomops/kb/graph.py` | `kb:<tenant_id>:<session_id>:<turn>` | 入队用 `ON CONFLICT`；`save_memory` 每轮 UPSERT |
| listing_compliance_review | `ecomops/listing/graph.py` | `listing:<review_id>` | `review_id` 唯一；对象存储键 `<review_id>.pdf` |
| cs_training | `ecomops/training/graph.py` | `train:<tenant_id>:<session_id>` | 每轮 UPSERT `training_sessions`；`finished` 后拒绝再追加 |

公共规则：
- 外部副作用（写库、写对象存储、调 EcomRAG/LLM/联网搜索）只在节点里发生；LLM、联网搜索失败一律降级（`fallback_used=true`），**不得以默认分或默认通过代替**。
- 买家/客服/PDF 原文进提示词时必须用 `llm.untrusted(label, text)` 包进定界标签。
- 共享件：`ecomops/core/llm.py` 的 `complete()`、`ecomops/core/rag.py` 的 `search()`（失败返回 `None`，调用方决定降级）、`ecomops/core/websearch.py` 的 `search()`（默认关闭，仅低置信且开关为 on 时调用）。
- 流式接口（SSE）统一事件格式：`event: <name>\ndata: <json>\n\n`，与 `training` 路由的 `event()` 一致。

### 7.x 增补：条款参与三轨、超时撤回、驳回原因、撤回后重新申请
- **条款参与三轨**：`load_order_and_policy` 用 `rag.search` 检索条款（有 Pilot 商品快照时把品类名拼进检索词）。≥0.75 分的条款里，只有"明确讲质量问题的句子"（含"质量"，且不含"非质量""无理由"，并含退换/退货/退款与 `N 天`，N∈[1,90]）才能改变 RULE 轨的时效窗口，解析不到沿用 15 天；RULE、FEE 两轨的 `detail.policy_clauses` 记录引用的条款（来源/版本/分数/是否沙盒）。检索失败不影响审核。
- **超时未补材料自动撤回**：`ECOMOPS_EVIDENCE_DEADLINE_HOURS`（默认 72）。`NEED_MORE_EVIDENCE` 超期且仍未补 → `WITHDRAWN`（`route_reason` 写明原因）+ `CLAIM_RESULT{result:WITHDRAWN, reason}` 出箱事件回写 Pilot。每张工单在 `claim_lock` 内复核，和买家补材料/撤回不会竞争。
- **驳回原因**：`CLAIM_RESULT` 载荷增加可选 `reason`（≤200 字）：自动驳回为路由原因，主管驳回为主管填写的原因。
- **撤回后重新申请**：同一订单子项的 `claim_no` 是确定性的，所以重新申请会撞上已撤回的旧工单。`POST /internal/v1/claims/submit` 遇到 `WITHDRAWN` 的同号工单时重置为新一轮审核（`audit_round+1`，清掉上一轮的路由/金额/风险），其它状态仍是幂等重放。结果出箱事件的唯一键第 2 轮起带 `_r<round>` 后缀，避免第二次撤回/驳回的回写被第一次的键吞掉。

### 7.y 增补：execute_refund_callback 节点与加急排序
- 图 3-4 里的 `execute_refund_callback` 节点现在在 `claim_audit` 图中：`auto_approve` 与"批准类主管决定"之后进入该节点，驳回/退回补材料直接结束。节点以 `step_execution_id`（`<claim_no>:r<round>:execute_refund_callback`）防重入，**只登记并唤醒 outbox worker，不同步调用 Pilot**；真正的 HTTP 调用、超时后先查 `refunds/status`、重试到上限转人工，仍由 outbox worker 完成（原因见决策记录第 5 条）。
- `GET /api/v1/claims` 排序：`nlu_signal.priority_boost === true` 的工单排前面，其余按创建时间倒序；每个工单对象带布尔字段 `priority_boost`。加急只是排序辅助，不改变任何审核结论。
