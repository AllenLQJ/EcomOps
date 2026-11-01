from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ecomops.core.auth import require_roles
from ecomops.kb import service

router = APIRouter(prefix="/api/v1/kb", tags=["kb"])
read = require_roles("operator", "supervisor", "cs_agent")
write = require_roles("operator", "supervisor")


class Ask(BaseModel):
    query: str = Field(min_length=1)
    catalog_category_id: str | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    web_search: bool = False


class Publish(BaseModel):
    standard_answer: str = Field(min_length=1)
    catalog_category_id: str = Field(min_length=1, max_length=64)
    rule_source: str = Field(min_length=1, max_length=128)
    effective_from: int | None = None
    effective_to: int | None = None


@router.post("/ask")
async def ask(body: Ask, identity=Depends(read)):
    return await service.ask(body, identity)


@router.get("/pending")
async def pending(status: str | None = None, identity=Depends(read)):
    return {"items": await service.pending(identity.tenant_id, status)}


@router.post("/pending/{queue_id}/publish")
async def publish(queue_id: UUID, body: Publish, identity=Depends(write)):
    return await service.transition(identity.tenant_id, queue_id, "publish", body)


@router.post("/pending/{queue_id}/retry")
async def retry(queue_id: UUID, identity=Depends(write)):
    return await service.transition(identity.tenant_id, queue_id, "retry")


@router.post("/pending/{queue_id}/discard")
async def discard(queue_id: UUID, identity=Depends(write)):
    return await service.transition(identity.tenant_id, queue_id, "discard")
