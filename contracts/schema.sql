-- EcomOps PostgreSQL 15 schema (SSOT: B 端售后工单/审批/出箱/待补录/合规/对练). 金额整型分。

CREATE TABLE claim_tickets (
  claim_no VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  order_item_id VARCHAR(64) NOT NULL,
  sku_id VARCHAR(64) NOT NULL,
  catalog_category_id VARCHAR(64),
  item_payable_cents BIGINT NOT NULL,
  max_refundable_cents BIGINT NOT NULL,
  approved_refund_cents BIGINT,
  suggested_refund_cents BIGINT,
  restock_policy VARCHAR(32),
  days_since_delivery INT NOT NULL DEFAULT 0,
  claim_type VARCHAR(32) NOT NULL DEFAULT 'QUALITY_REFUND',
  buyer_reason_text TEXT,
  evidence_urls JSONB NOT NULL DEFAULT '[]',
  nlu_signal JSONB,
  claim_status VARCHAR(40) NOT NULL,
  risk_level VARCHAR(16),
  route_reason TEXT,
  fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
  audit_round INT NOT NULL DEFAULT 1,
  thread_id VARCHAR(80) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_claim_status ON claim_tickets (tenant_id, claim_status);

CREATE TABLE claim_track_results (
  id BIGSERIAL PRIMARY KEY,
  claim_no VARCHAR(64) NOT NULL REFERENCES claim_tickets(claim_no),
  audit_round INT NOT NULL,
  track VARCHAR(16) NOT NULL,              -- RULE | EVIDENCE | FEE
  passed BOOLEAN NOT NULL,
  confidence NUMERIC(4,3),
  fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (claim_no, audit_round, track)
);

CREATE TABLE claim_approvals (
  id BIGSERIAL PRIMARY KEY,
  claim_no VARCHAR(64) NOT NULL REFERENCES claim_tickets(claim_no),
  action VARCHAR(24) NOT NULL,             -- APPROVE | MODIFY_AMOUNT | REJECT | REQUEST_EVIDENCE | AUTO_APPROVE
  approver_user_id VARCHAR(64) NOT NULL,
  amount_cents BIGINT,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE outbox_events (
  event_id VARCHAR(64) PRIMARY KEY,
  claim_no VARCHAR(64) NOT NULL REFERENCES claim_tickets(claim_no),
  event_type VARCHAR(32) NOT NULL DEFAULT 'REFUND_CALLBACK',
  refund_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
  payload JSONB NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'PENDING',   -- PENDING | PENDING_RETRY | DONE | FAILED_MANUAL
  attempts INT NOT NULL DEFAULT 0,
  next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_outbox_due ON outbox_events (status, next_retry_at);

-- 节点副作用重入幂等
CREATE TABLE step_executions (
  step_execution_id VARCHAR(160) PRIMARY KEY,
  claim_no VARCHAR(64) NOT NULL,
  node VARCHAR(40) NOT NULL,
  result JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_pending_queue (
  queue_id UUID PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  catalog_category_id VARCHAR(64),
  question_text TEXT NOT NULL,
  standard_answer_draft TEXT,
  rule_source VARCHAR(128),
  effective_from BIGINT,
  effective_to BIGINT,
  version INT NOT NULL DEFAULT 0,
  deterministic_chunk_id VARCHAR(80),
  publish_status VARCHAR(20) NOT NULL DEFAULT 'PENDING_REVIEW',
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uk_pending_chunk ON knowledge_pending_queue (deterministic_chunk_id) WHERE deterministic_chunk_id IS NOT NULL;
CREATE UNIQUE INDEX uk_pending_question ON knowledge_pending_queue (tenant_id, md5(question_text)) WHERE publish_status = 'PENDING_REVIEW';

CREATE TABLE qa_sessions (
  session_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  messages JSONB NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, session_id)
);

CREATE TABLE listing_reviews (
  review_id UUID PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  declared_category VARCHAR(64) NOT NULL,
  pdf_object_path VARCHAR(255) NOT NULL,
  verdict VARCHAR(32) NOT NULL,            -- REJECTED_BLOCKING | PENDING_MANUAL_VERIFY | PASSED_WITH_SUGGESTIONS
  report JSONB NOT NULL,
  fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
  scenario_seed JSONB,                    -- 供 cs_training 使用的对练场景种子卡（阻断项/待核实项的高频争议点）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE training_sessions (
  session_id UUID PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  scenario_seed JSONB NOT NULL,
  stage VARCHAR(24) NOT NULL DEFAULT 'verify_order',
  follow_up_count INT NOT NULL DEFAULT 0,
  messages JSONB NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  report JSONB,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 告警人工处理留痕（重试/标记已处理）；由 ecomops.alerts.service.ensure_schema 在启动时幂等创建，已有库无需手工迁移
CREATE TABLE IF NOT EXISTS alert_actions (
  id BIGSERIAL PRIMARY KEY,
  event_id VARCHAR(64) NOT NULL,
  claim_no VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(64) NOT NULL,
  action VARCHAR(16) NOT NULL,            -- RETRY | RESOLVE
  actor_user_id VARCHAR(64) NOT NULL,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alert_actions_event ON alert_actions (event_id);
