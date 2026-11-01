import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ecomops.core.auth import require_roles
from ecomops.core.errors import ApiError
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
async def ask(body: Ask, request: Request, identity=Depends(read)):
    return await service.ask(body, identity, graph=getattr(request.app.state, "kb_graph", None))


def event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/ask/stream")
async def ask_stream(body: Ask, request: Request, identity=Depends(read)):
    # Finish the graph and commit its side effects before starting the response.
    error = None
    try:
        result = await service.ask(body, identity, graph=getattr(request.app.state, "kb_graph", None))
    except ApiError as exc:
        error = {"code": exc.code}
    except Exception:
        # Never send provider exception details, URLs or credentials to clients.
        error = {"code": "UPSTREAM_ERROR"}

    async def stream():
        if error is not None:
            yield event("error", error)
            return
        yield event("meta", {key: result[key] for key in ("query_type", "top1_score", "hits", "session_id")})
        # complete() returns full text: these are chunks, not live model tokens.
        for offset in range(0, len(result["answer"]), 40):
            yield event("token", {"text": result["answer"][offset:offset + 40]})
        yield event("done", {key: result[key] for key in ("answer", "fallback_used", "enqueued_queue_id")})
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
