from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ECOMOPS_", env_file=".env", extra="ignore")

    env: str = "dev"                      # prod 时启动自检会拒绝开发默认密钥，见 ecomops/core/hardening.py
    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_user: str = "postgres"
    db_password: str = "ecomops_pw"
    db_name: str = "ecomops"
    jwt_secret: str = "dev-secret-change-me-dev-secret-change-me"
    jwt_alg: str = "HS256"
    internal_token: str = "dev-internal-token"       # 与 EcomPilot 默认内部令牌一致
    pilot_url: str = "http://127.0.0.1:8085"
    pilot_internal_token: str = "dev-internal-token"
    rag_url: str = "http://127.0.0.1:8002"
    rag_search_timeout_s: float = 3.0      # 接真实 EcomRAG（含 LLM 生成）时需调大，见 adapters/
    rag_upsert_timeout_s: float = 3.0      # 真实入库走 ingest 命令行，需调大
    rag_claim_timeout_s: float = 2.0       # 售后审核里的条款检索，尽力而为
    nlu_url: str = "http://127.0.0.1:8001"
    dev_tokens: bool = False
    enable_faults: bool = False
    reconcile_interval_seconds: float = 30.0
    outbox_max_attempts: int = 10
    llm_base_url: str = ""                 # OpenAI 兼容接口（含 /v1 或 /api/paas/v4）；三项都配置才启用真实 LLM
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 8.0
    alert_webhook_url: str = ""            # 非空时，新告警会 POST 到该地址（失败只记录，不影响主流程）
    alert_stuck_minutes: int = 30          # 退款回调停留超过该时长视为"卡住"告警
    outbox_backoff_seconds: float = 30.0
    auto_approve_max_cents: int = 5000
    refund_callback_timeout_seconds: float = 5.0
    minio_endpoint: str = ""
    minio_access_key: str = "ecomops"
    minio_secret_key: str = "ecomops_minio_pw"
    minio_bucket: str = "ecomops-docs"
    object_dir: str = ".data/objects"


@lru_cache
def get_settings() -> Settings:
    return Settings()
