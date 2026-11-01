"""请求 ID + 结构化访问日志 + Prometheus 指标。纯 ASGI 中间件，不缓冲 SSE 流式响应。"""
import json
import logging
import re
import time
import uuid

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("ecomops.access")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
REGISTRY = CollectorRegistry()
REQUESTS = Counter("ecomops_http_requests_total", "HTTP 请求数", ["method", "route", "status"], registry=REGISTRY)
LATENCY = Histogram("ecomops_http_request_seconds", "HTTP 请求耗时", ["method", "route"], registry=REGISTRY,
                    buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10))


class ObservabilityMiddleware:
    def __init__(self, app, access_log: bool = True):
        self.app, self.access_log = app, access_log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex   # 不信任任意长度/字符的外部值
        scope.setdefault("state", {})["request_id"] = request_id
        started, status = time.perf_counter(), 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", None) or "unmatched"   # 路由模板，避免 order_id 等造成高基数
            REQUESTS.labels(scope["method"], route, str(status)).inc()
            LATENCY.labels(scope["method"], route).observe(elapsed)
            if self.access_log:
                logger.info(json.dumps({"request_id": request_id, "method": scope["method"], "route": route,
                                        "status": status, "ms": round(elapsed * 1000, 1)}, ensure_ascii=False))


OUTBOX = Gauge("ecomops_outbox_events", "出箱事件数（按状态）", ["status"], registry=REGISTRY)
OPEN_ALERTS = Gauge("ecomops_open_alerts", "待人工处理的告警数（FAILED_MANUAL 出箱事件）", registry=REGISTRY)
OUTBOX_STATUSES = ("PENDING", "PENDING_RETRY", "DONE", "FAILED_MANUAL", "RESOLVED_MANUAL")


async def refresh_gauges() -> None:
    """抓取指标时从库里读出箱积压；查询失败不影响其他指标。"""
    from sqlalchemy import text

    from ecomops.core.db import get_engine
    try:
        async with get_engine().connect() as c:
            rows = (await c.execute(text("SELECT status, count(*) AS n FROM outbox_events GROUP BY status"))).all()
    except Exception:    # noqa: BLE001
        return
    counts = {status: n for status, n in rows}
    for status in OUTBOX_STATUSES:
        OUTBOX.labels(status).set(counts.get(status, 0))
    OPEN_ALERTS.set(counts.get("FAILED_MANUAL", 0))


def metrics_response() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def install(app) -> None:
    if not logger.handlers:                      # 访问日志每行一个 JSON，直接写 stderr，由部署侧收集
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(_: Request):
        await refresh_gauges()
        return metrics_response()
