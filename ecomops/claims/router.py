import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ecomops.claims import service
from ecomops.core.auth import Identity, require_internal, require_roles
from ecomops.outbox import worker

router = APIRouter()


class SubmitIn(BaseModel):
    claim_no: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1)
    user_id: str
    order_id: str
    order_item_id: str
    sku_id: str
    catalog_category_id: str | None = None
    item_payable_cents: int | None = None
    max_refundable_cents: int = Field(ge=0)
    days_since_delivery: int = 0
    claim_type: str | None = None
    buyer_reason_text: str | None = None
    evidence_urls: list[str] = []
    nlu_signal: dict | None = None


class ConfirmIn(BaseModel):
    action: str
    amount_cents: int | None = None
    reason: str | None = None


class EvidenceIn(BaseModel):
    evidence_urls: list[str] = Field(min_length=1)


def _kick():
    asyncio.get_running_loop().create_task(worker.process_due())


@router.post("/internal/v1/claims/submit", status_code=202, dependencies=[Depends(require_internal)])
async def submit(body: SubmitIn, request: Request):
    out, replayed = await service.submit(request.app.state.graph, body.model_dump())
    _kick()
    return {**out, "idempotent_replayed": replayed}


@router.post("/internal/v1/claims/{claim_no}/evidence", status_code=202, dependencies=[Depends(require_internal)])
async def evidence(claim_no: str, body: EvidenceIn, request: Request):
    out = await service.add_evidence(request.app.state.graph, claim_no, body.evidence_urls)
    _kick()
    return out


@router.get("/internal/v1/claims/{claim_no}/status", dependencies=[Depends(require_internal)])
async def claim_status(claim_no: str):
    return await service.public_status(claim_no)


@router.post("/internal/v1/claims/{claim_no}/withdraw", dependencies=[Depends(require_internal)])
async def withdraw(claim_no: str):
    out = await service.withdraw(claim_no)
    _kick()
    return out


@router.get("/api/v1/claims")
async def list_claims(status: str | None = None, ident: Identity = Depends(require_roles("supervisor", "operator"))):
    return {"claims": await service.list_claims(ident, status)}


@router.get("/api/v1/claims/{claim_no}")
async def get_claim(claim_no: str, ident: Identity = Depends(require_roles("supervisor", "operator"))):
    return await service.get_detail(ident, claim_no)


@router.post("/api/v1/claims/{claim_no}/confirm")
async def confirm(claim_no: str, body: ConfirmIn, request: Request, ident: Identity = Depends(require_roles("supervisor"))):
    out = await service.confirm(request.app.state.graph, ident, claim_no, body.action, body.amount_cents, body.reason)
    _kick()
    return out


@router.post("/internal/v1/outbox/run-once", dependencies=[Depends(require_internal)])
async def run_once():
    return {"processed": await worker.process_due()}
