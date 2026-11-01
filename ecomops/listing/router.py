"""商品详情 PDF 审查 API；所有持久化读取均限定 JWT 租户。"""
import asyncio
import json
from uuid import UUID, uuid4

import pymupdf
from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import text

from ecomops.core.auth import Identity, require_roles
from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError
from ecomops.core.storage import get_store
from ecomops.listing.checkers import extract_pdf, extract_structure, review_document

router = APIRouter(prefix="/api/v1/listing")
_identity = require_roles("operator", "supervisor")


@router.post("/reviews", status_code=201)
async def create_review(file: UploadFile = File(...), declared_category: str = Form(...),
                        identity: Identity = Depends(_identity)):
    category = declared_category.strip()
    if not category or len(category) > 64:
        raise ApiError(400, "INVALID_REQUEST", "invalid declared_category")
    data = await file.read()
    if not data or file.content_type != "application/pdf" or not data.startswith(b"%PDF-"):
        raise ApiError(400, "UNSUPPORTED_FILE", "请上传非空 PDF 文件")
    review_id = str(uuid4())
    path = await asyncio.to_thread(get_store().put, f"{review_id}.pdf", data, "application/pdf")
    try:
        extracted = await asyncio.to_thread(extract_pdf, data)
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise ApiError(400, "UNSUPPORTED_FILE", "无法读取 PDF") from exc
    document = extract_structure(extracted)
    report = await review_document(document, category)
    if not extracted.strip():
        report["manual_verify_items"].append({"checker": "text_extract", "message": "PDF 无可提取文本，请人工核实或提供可检索 PDF"})
        if report["verdict"] != "REJECTED_BLOCKING":
            report["verdict"] = "PENDING_MANUAL_VERIFY"
    report.update(review_id=review_id, declared_category=category, pdf_object_path=path,
                  extracted_fields={k: v for k, v in document.items() if k != "text"})
    async with get_engine().begin() as conn:
        await conn.execute(text("""INSERT INTO listing_reviews
            (review_id, tenant_id, created_by, declared_category, pdf_object_path, verdict, report, fallback_used)
            VALUES (:id, :tenant, :user, :category, :path, :verdict, CAST(:report AS jsonb), :fallback)"""),
            {"id": UUID(review_id), "tenant": identity.tenant_id, "user": identity.user_id,
             "category": category, "path": path, "verdict": report["verdict"],
             "report": json.dumps(report, ensure_ascii=False), "fallback": report["fallback_used"]})
    return report


@router.get("/reviews")
async def list_reviews(identity: Identity = Depends(_identity)):
    async with get_engine().connect() as conn:
        rows = await conn.execute(text("SELECT report FROM listing_reviews WHERE tenant_id=:tenant ORDER BY created_at DESC, review_id"), {"tenant": identity.tenant_id})
        return {"reviews": list(rows.scalars())}


@router.get("/reviews/{review_id}")
async def get_review(review_id: str, identity: Identity = Depends(_identity)):
    try:
        key = UUID(review_id)
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "review not found") from None
    async with get_engine().connect() as conn:
        report = (await conn.execute(text("SELECT report FROM listing_reviews WHERE review_id=:id AND tenant_id=:tenant"),
                                     {"id": key, "tenant": identity.tenant_id})).scalar_one_or_none()
    if report is None:
        raise ApiError(404, "NOT_FOUND", "review not found")
    return report
