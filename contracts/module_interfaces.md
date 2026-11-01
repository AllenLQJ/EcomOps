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
