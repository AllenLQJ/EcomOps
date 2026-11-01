"""商品详情 PDF 审查 API；所有持久化读取均限定 JWT 租户。"""
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Depends, File, Form, UploadFile
from sqlalchemy import text

from ecomops.core.auth import Identity, require_roles
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError
from ecomops.core.storage import get_store
from ecomops.listing.graph import build_graph

_default_graph = None

router = APIRouter(prefix="/api/v1/listing")
_identity = require_roles("operator", "supervisor")


@router.post("/reviews", status_code=201)
async def create_review(request: Request, file: UploadFile = File(...), declared_category: str = Form(...),
                        identity: Identity = Depends(_identity)):
    category = declared_category.strip()
    if not category or len(category) > 64:
        raise ApiError(400, "INVALID_REQUEST", "invalid declared_category")
    data = await file.read()
    if not data or file.content_type != "application/pdf" or not data.startswith(b"%PDF-"):
        raise ApiError(400, "UNSUPPORTED_FILE", "请上传非空 PDF 文件")
    review_id = str(uuid4())
    global _default_graph
    graph = getattr(request.app.state, "listing_graph", None)
    if graph is None:
        if _default_graph is None:
            _default_graph = build_graph()
        graph = _default_graph
    result = await graph.ainvoke(
        {"review_id": review_id, "tenant_id": identity.tenant_id, "user_id": identity.user_id,
         "declared_category": category, "pdf_bytes": data},
        config={"configurable": {"thread_id": f"listing:{review_id}"}})
    return result["report"]


@router.get("/reviews")
async def list_reviews(identity: Identity = Depends(_identity)):
    async with get_engine().connect() as conn:
        rows = await conn.execute(text("SELECT report, scenario_seed FROM listing_reviews WHERE tenant_id=:tenant ORDER BY created_at DESC, review_id"), {"tenant": identity.tenant_id})
        return {"reviews": [{**row.report, "scenario_seed": row.scenario_seed} for row in rows]}


@router.get("/reviews/{review_id}")
async def get_review(review_id: str, identity: Identity = Depends(_identity)):
    try:
        key = UUID(review_id)
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "review not found") from None
    async with get_engine().connect() as conn:
        report = (await conn.execute(text("SELECT report, scenario_seed FROM listing_reviews WHERE review_id=:id AND tenant_id=:tenant"),
                                     {"id": key, "tenant": identity.tenant_id})).one_or_none()
    if report is None:
        raise ApiError(404, "NOT_FOUND", "review not found")
    return {**report.report, "scenario_seed": report.scenario_seed}
