"""claim_audit 验收：function 级 ops_env（独立库 + Ops；可选真实 Pilot / 故障代理）。"""
from __future__ import annotations

import time
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
import pytest

from ecomops.core.testdb import test_database
from tests.stack import (
    INTERNAL_TOKEN,
    Proc,
    PilotStack,
    fault_proxy,
    free_port,
    ops_proc,
    pilot_stack,
)

DEFAULT_TENANT = "SHOP_SELF_01"
UNREACHABLE_PILOT = "http://127.0.0.1:1"


@dataclass
class OpsEnv:
    """单测环境：Ops URL、可选 Pilot 栈与故障代理，以及提交/轮询 helper。"""

    url: str
    db_name: str
    ops: Proc
    pilot: PilotStack | None = None
    proxy: Proc | None = None
    kind: str = "ops_only"
    _client_timeout: float = 30.0

    @property
    def internal_headers(self) -> dict[str, str]:
        return {"X-Internal-Token": INTERNAL_TOKEN}

    def ops_token(
        self,
        role: str,
        user: str,
        tenant_id: str = DEFAULT_TENANT,
    ) -> dict[str, str]:
        r = httpx.post(
            f"{self.url}/api/v1/dev/token",
            json={"user_id": user, "role": role, "tenant_id": tenant_id},
            timeout=self._client_timeout,
        )
        assert r.status_code == 200, (
            f"POST /api/v1/dev/token role={role!r} user={user!r} tenant={tenant_id!r}: "
            f"expected 200, got {r.status_code}: {r.text[:400]}"
        )
        token = r.json().get("token")
        assert isinstance(token, str) and token, f"dev/token missing token: {r.text[:400]}"
        return {"Authorization": f"Bearer {token}"}

    def submit_claim(self, payload: dict[str, Any]) -> httpx.Response:
        return httpx.post(
            f"{self.url}/internal/v1/claims/submit",
            headers=self.internal_headers,
            json=payload,
            timeout=60.0,
        )

    def post_evidence(self, claim_no: str, evidence_urls: list[str]) -> httpx.Response:
        return httpx.post(
            f"{self.url}/internal/v1/claims/{claim_no}/evidence",
            headers=self.internal_headers,
            json={"evidence_urls": evidence_urls},
            timeout=60.0,
        )

    def set_faults(self, body: dict[str, Any]) -> httpx.Response:
        return httpx.post(
            f"{self.url}/internal/v1/faults",
            headers=self.internal_headers,
            json=body,
            timeout=self._client_timeout,
        )

    def get_claim(self, headers: dict[str, str], claim_no: str) -> httpx.Response:
        return httpx.get(
            f"{self.url}/api/v1/claims/{claim_no}",
            headers=headers,
            timeout=self._client_timeout,
        )

    def confirm(
        self,
        headers: dict[str, str],
        claim_no: str,
        body: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> httpx.Response:
        return httpx.post(
            f"{self.url}/api/v1/claims/{claim_no}/confirm",
            headers=headers,
            json=body,
            timeout=timeout,
        )

    def wait_status(
        self,
        headers: dict[str, str],
        claim_no: str,
        targets: Iterable[str],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        wanted = set(targets)
        deadline = time.time() + timeout
        last: dict[str, Any] | None = None
        last_http: int | None = None
        while time.time() < deadline:
            r = self.get_claim(headers, claim_no)
            last_http = r.status_code
            if r.status_code == 200:
                last = r.json()
                if last.get("claim_status") in wanted:
                    return last
            time.sleep(0.25)
        raise AssertionError(
            f"wait_status({claim_no!r}): expected claim_status in {sorted(wanted)} "
            f"within {timeout}s; last_http={last_http}; last_body={last!r}"
        )

    def proxy_set_mode(self, mode: dict[str, Any]) -> dict[str, Any]:
        assert self.proxy is not None, "proxy_set_mode requires ops_env kind=proxy"
        r = httpx.post(f"{self.proxy.url}/proxy/mode", json=mode, timeout=self._client_timeout)
        assert r.status_code == 200, f"POST /proxy/mode failed: {r.status_code} {r.text[:300]}"
        return r.json()

    def proxy_get_mode(self) -> dict[str, Any]:
        assert self.proxy is not None, "proxy_get_mode requires ops_env kind=proxy"
        r = httpx.get(f"{self.proxy.url}/proxy/mode", timeout=self._client_timeout)
        assert r.status_code == 200, f"GET /proxy/mode failed: {r.status_code} {r.text[:300]}"
        return r.json()


def base_claim_payload(**over: Any) -> dict[str, Any]:
    """路线图 4.2-1 字段齐全的提交体；调用方覆盖差异字段。"""
    body = {
        "claim_no": "CLM_PLACEHOLDER",
        "tenant_id": DEFAULT_TENANT,
        "user_id": "USR_BUYER_01",
        "order_id": "ORD_TEST_001",
        "order_item_id": "ITEM_01",
        "sku_id": "SKU_HEATER_003",
        "catalog_category_id": "HOME_APPLIANCE_HEATER",
        "item_payable_cents": 112000,
        "max_refundable_cents": 112000,
        "days_since_delivery": 5,
        "claim_type": "QUALITY_REFUND",
        "buyer_reason_text": "漏水",
        "evidence_urls": ["https://storage.example.invalid/evidence/evidence_01.jpg"],
        "nlu_signal": {
            "predicted_label": "热水器",
            "category_confidence": 0.98,
            "sentiment": "NEGATIVE",
            "sentiment_score": 0.96,
            "priority_boost": True,
        },
    }
    body.update(over)
    return body


def err_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code")
    return None


def track_by_name(detail: dict[str, Any], name: str) -> dict[str, Any] | None:
    for t in detail.get("tracks") or []:
        if t.get("track") == name:
            return t
    return None


@contextmanager
def _open_ops_env(kind: str):
    """kind: ops_only | pilot | proxy。"""
    if kind not in {"ops_only", "pilot", "proxy"}:
        raise ValueError(f"unknown ops_env kind: {kind!r}")

    with test_database() as db_name:
        with ExitStack() as stack:
            ops: Proc | None = None
            ps: PilotStack | None = None
            px: Proc | None = None

            if kind == "ops_only":
                ops = ops_proc(db_name, pilot_url=UNREACHABLE_PILOT).start()
                stack.callback(ops.stop)
                yield OpsEnv(url=ops.url, db_name=db_name, ops=ops, kind=kind)
                return

            ops_port = free_port()
            ps = stack.enter_context(
                pilot_stack(ops_url_for_pilot=f"http://127.0.0.1:{ops_port}")
            )
            pilot_url = ps.pilot_url
            if kind == "proxy":
                px = stack.enter_context(fault_proxy(ps.pilot_url))
                pilot_url = px.url

            ops = ops_proc(db_name, port=ops_port, pilot_url=pilot_url).start()
            stack.callback(ops.stop)
            yield OpsEnv(
                url=ops.url,
                db_name=db_name,
                ops=ops,
                pilot=ps,
                proxy=px,
                kind=kind,
            )


@pytest.fixture
def ops_env(request):
    """function 级。默认 ops_only；间接参数 'pilot' / 'proxy' 启用真实 Pilot（及故障代理）。"""
    kind = getattr(request, "param", "ops_only")
    with _open_ops_env(kind) as env:
        yield env


@pytest.fixture
def make_claim_payload():
    """暴露 4.2-1 提交体工厂（acceptance 无 __init__.py，避免跨模块 import conftest）。"""
    return base_claim_payload


@pytest.fixture
def claim_err_code():
    return err_code


@pytest.fixture
def claim_track_by_name():
    return track_by_name
