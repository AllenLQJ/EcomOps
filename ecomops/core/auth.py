"""JWT + RBAC。身份只来自 Token；内部调用用 X-Internal-Token。"""
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header

from ecomops.core.config import get_settings
from ecomops.core.errors import ApiError

ROLES = ("supervisor", "operator", "cs_agent")


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant_id: str
    role: str


def issue_token(user_id: str, role: str = "operator", tenant_id: str = "SHOP_SELF_01", ttl: int = 3600) -> str:
    s = get_settings()
    now = int(time.time())
    return jwt.encode({"sub": user_id, "tenant_id": tenant_id, "role": role, "iat": now, "exp": now + ttl},
                      s.jwt_secret, algorithm=s.jwt_alg)


def current_identity(authorization: str | None = Header(default=None)) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token")
    s = get_settings()
    try:
        p = jwt.decode(authorization[7:].strip(), s.jwt_secret, algorithms=[s.jwt_alg])
    except jwt.PyJWTError:
        raise ApiError(401, "UNAUTHORIZED", "invalid or expired token")
    if not p.get("sub") or not p.get("tenant_id") or p.get("role") not in ROLES:
        raise ApiError(401, "UNAUTHORIZED", "token missing identity")
    return Identity(p["sub"], p["tenant_id"], p["role"])


def require_roles(*roles: str):
    def dep(ident: Identity = Depends(current_identity)) -> Identity:
        if ident.role not in roles:
            raise ApiError(403, "FORBIDDEN_ROLE", f"role {ident.role} not allowed")
        return ident
    return dep


def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != get_settings().internal_token:
        raise ApiError(401, "INTERNAL_AUTH_FAILED", "bad internal token")
