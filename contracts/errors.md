# 错误码（统一，禁止自造）

响应体：`{"error": {"code": "<CODE>", "message": "...", "details": {...}}}`

| HTTP | code | 含义 |
|---|---|---|
| 400 | INVALID_REQUEST | 参数缺失/格式错误 |
| 400 | UNSUPPORTED_FILE | 上传文件类型不支持或为空（仅 PDF） |
| 401 | UNAUTHORIZED | JWT 缺失/无效/过期 |
| 401 | INTERNAL_AUTH_FAILED | `X-Internal-Token` 校验失败 |
| 403 | FORBIDDEN_ROLE | 角色无权执行该操作 |
| 404 | NOT_FOUND | 资源不存在或不属于当前租户 |
| 409 | ALERT_STATE_CONFLICT | 告警已不处于待人工处理状态（已被重试/标记处理） |
| 409 | CLAIM_STATE_CONFLICT | 工单当前状态不允许该操作（含已被另一决定处理且决定不同） |
| 409 | PUBLISH_STATE_CONFLICT | 待补录条目状态不允许该操作 |
| 422 | AMOUNT_EXCEEDS_LIMIT | 批准/修改金额超过 `max_refundable_cents` 或 ≤0 |
| 422 | INVALID_STAGE | 对练会话已结束或阶段非法 |
| 502 | UPSTREAM_ERROR | EcomPilot / RAG / NLU 返回错误 |
| 504 | UPSTREAM_TIMEOUT | 下游超时 |

幂等重放（重复批准同一决定、重复提交同一 claim_no）：HTTP 200/202 + `idempotent_replayed=true`，不是错误。
