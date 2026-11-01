import asyncio
import importlib
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel

from ecomops.alerts import service as alert_service
from ecomops.alerts.router import router as alerts_router
from ecomops.claims import graph as claim_graph
from ecomops.claims.router import router as claims_router
from ecomops.core import hardening, llm, observability
from ecomops.core.auth import issue_token, require_internal
from ecomops.core.config import get_settings
from ecomops.core.db import dispose_engines, make_pool
from ecomops.core.errors import ApiError, install_error_handlers
from ecomops.outbox import worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    hardening.enforce(get_settings())
    pool = make_pool()
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    await alert_service.ensure_schema()
    app.state.graph = claim_graph.build_graph(saver)
    # 其余三条工作流（约定见 contracts/module_interfaces.md 第 8 节）：共用同一个 AsyncPostgresSaver
    for name, module in (("kb_graph", "ecomops.kb.graph"), ("listing_graph", "ecomops.listing.graph"),
                         ("training_graph", "ecomops.training.graph")):
        setattr(app.state, name, importlib.import_module(module).build_graph(saver))
    task = asyncio.create_task(worker.run_loop())
    yield
    task.cancel()
    await pool.close()
    await dispose_engines()


app = FastAPI(title="EcomOps", lifespan=lifespan)
install_error_handlers(app)
observability.install(app)
app.include_router(claims_router)
app.include_router(alerts_router)
for _mod in ("ecomops.kb.router", "ecomops.listing.router", "ecomops.training.router"):
    try:
        app.include_router(importlib.import_module(_mod).router)
    except ModuleNotFoundError as e:
        if e.name != _mod:
            raise


@app.get("/health")
def health():
    return {"status": "ok"}


class _DevTokenIn(BaseModel):
    user_id: str
    role: str = "operator"
    tenant_id: str = "SHOP_SELF_01"


if get_settings().dev_tokens:
    @app.post("/api/v1/dev/token")
    def dev_token(body: _DevTokenIn):
        return {"token": issue_token(body.user_id, body.role, body.tenant_id)}


if get_settings().enable_faults:
    @app.post("/internal/v1/faults", dependencies=[Depends(require_internal)])
    def faults(body: dict):
        llm.set_failures(list(body.get("llm_fail") or []))
        try:
            from ecomops.kb import faults as kb_faults
            kb_faults.state["publish_writeback_fail"] = bool(body.get("publish_writeback_fail", False))
        except ImportError:
            pass
        return {"llm_fail": body.get("llm_fail") or [], "publish_writeback_fail": bool(body.get("publish_writeback_fail"))}


_INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    if not _INDEX.exists():
        return {"status": "ok", "ui": "static/index.html not built yet"}
    return FileResponse(_INDEX)
