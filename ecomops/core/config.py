from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ECOMOPS_", env_file=".env", extra="ignore")

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
    nlu_url: str = "http://127.0.0.1:8001"
    dev_tokens: bool = False
    enable_faults: bool = False
    reconcile_interval_seconds: float = 30.0
    outbox_max_attempts: int = 10
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
