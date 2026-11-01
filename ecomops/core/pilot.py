"""EcomPilot 只读接口客户端（商品快照）。失败返回 None，调用方尽力而为。"""
import httpx

from ecomops.core.config import get_settings


async def sku_snapshot(tenant_id: str, sku_id: str, timeout: float = 2.0) -> dict | None:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{s.pilot_url.rstrip('/')}/internal/v1/catalog/skus/{sku_id}",
                                 params={"tenant_id": tenant_id}, headers={"X-Internal-Token": s.pilot_internal_token})
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, dict) and isinstance(data.get("category_name"), str) else None
    except (httpx.HTTPError, ValueError):
        return None
