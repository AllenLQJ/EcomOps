"""生产环境启动自检：ECOMOPS_ENV=prod 时，仍在使用开发默认密钥/口令或开着开发入口就拒绝启动。

报错只列出"哪一项不合格"，不回显任何值。"""
from ecomops.core.config import Settings

DEV_DEFAULTS = {
    "jwt_secret": "dev-secret-change-me-dev-secret-change-me",
    "internal_token": "dev-internal-token",
    "pilot_internal_token": "dev-internal-token",
    "db_password": "ecomops_pw",
}


def production_problems(s: Settings) -> list[str]:
    problems = [f"{name} 仍是开发默认值" for name, default in DEV_DEFAULTS.items() if getattr(s, name) == default]
    if len(s.jwt_secret) < 32:
        problems.append("jwt_secret 少于 32 个字符")
    if len(s.internal_token) < 16:
        problems.append("internal_token 少于 16 个字符")
    if s.dev_tokens:
        problems.append("ECOMOPS_DEV_TOKENS：开发令牌入口不能在生产启用")
    if s.enable_faults:
        problems.append("ECOMOPS_ENABLE_FAULTS：故障注入入口不能在生产启用")
    if s.minio_endpoint and s.minio_secret_key == "ecomops_minio_pw":
        problems.append("minio_secret_key 仍是开发默认值")
    return problems


def enforce(s: Settings) -> None:
    if s.env.lower() not in ("prod", "production"):
        return
    problems = production_problems(s)
    if problems:
        raise RuntimeError("拒绝以生产模式启动：" + "；".join(problems))
