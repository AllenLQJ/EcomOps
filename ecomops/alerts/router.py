import asyncio

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ecomops.alerts import service
from ecomops.core.auth import Identity, require_roles
from ecomops.outbox import worker

router = APIRouter()


class RetryIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ResolveIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.get("/api/v1/alerts")
async def list_alerts(state: str | None = Query(default=None, pattern="^(OPEN|RESOLVED)$"),
                      ident: Identity = Depends(require_roles("supervisor"))):
    return {"alerts": await service.list_alerts(ident, state)}


@router.post("/api/v1/alerts/{alert_id}/retry", status_code=202)
async def retry(alert_id: str, body: RetryIn | None = None, ident: Identity = Depends(require_roles("supervisor"))):
    out = await service.retry(ident, alert_id, body.reason if body else None)
    asyncio.get_running_loop().create_task(worker.process_due())
    return out


@router.post("/api/v1/alerts/{alert_id}/resolve")
async def resolve(alert_id: str, body: ResolveIn, ident: Identity = Depends(require_roles("supervisor"))):
    return await service.resolve(ident, alert_id, body.reason)
