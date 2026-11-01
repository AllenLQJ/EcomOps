"""Training API with JSON-framed server-sent events."""
import json
from contextlib import aclosing
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ecomops.core import llm
from ecomops.core.auth import Identity, require_roles
from ecomops.training import service

router = APIRouter(prefix="/api/v1/training", tags=["training"])
identity_dependency = require_roles("cs_agent", "operator", "supervisor")


class CreateSession(BaseModel):
    scenario_seed: dict | None = None
    from_listing_review_id: UUID | None = None


class ChatMessage(BaseModel):
    message: str = Field(min_length=1)


@router.post("/sessions", status_code=201)
async def create_session(body: CreateSession = Body(default_factory=CreateSession),
                         identity: Identity = Depends(identity_dependency)):
    return await service.create_session(identity, body.scenario_seed, body.from_listing_review_id)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, identity: Identity = Depends(identity_dependency)):
    return await service.get_session(session_id, identity)


def event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, body: ChatMessage, request: Request, identity: Identity = Depends(identity_dependency)):
    if llm.configured():
        await service.check_chat_access(session_id, identity)

        async def live():
            try:
                async with aclosing(service.stream_chat(session_id, identity, body.message,
                        getattr(request.app.state, "training_graph", None))) as events:
                    async for name, data in events:
                        yield event(name, data)
            except Exception:
                yield event("error", {"code": "UPSTREAM_ERROR"})
        return StreamingResponse(live(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # Resolve errors and commit state before sending HTTP/SSE headers.
    state, reply, advanced = await service.chat(session_id, identity, body.message,
                                                getattr(request.app.state, "training_graph", None))

    async def stream():
        for segment in service.reply_segments(reply):
            yield event("token", {"text": segment})
        yield event("stage", {"stage": state["stage"], "advanced": advanced,
                              "follow_up_count": state["follow_up_count"]})
        if state["report"] is not None:
            yield event("report", state["report"])
        yield event("done", {"session_id": state["session_id"]})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
