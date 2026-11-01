"""KB/listing/training 验收辅助：独立库 + RAG 桩 + Ops 进程（function 级 kb_env）。"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from ecomops.core.testdb import test_database
from tests.stack import INTERNAL_TOKEN, OPS_DIR, Proc, free_port, ops_proc


def _ops_url(ops: Proc | str) -> str:
    return ops if isinstance(ops, str) else ops.url


def token(ops: Proc | str, role: str, user: str, tenant: str) -> dict[str, str]:
    """POST {ops}/api/v1/dev/token → Authorization Bearer 头。"""
    r = httpx.post(
        f"{_ops_url(ops)}/api/v1/dev/token",
        json={"user_id": user, "role": role, "tenant_id": tenant},
        timeout=30.0,
    )
    assert r.status_code == 200, f"dev/token 失败 status={r.status_code} body={r.text}"
    body = r.json()
    assert "token" in body, f"dev/token 响应缺 token: {body}"
    return {"Authorization": f"Bearer {body['token']}"}


def faults(ops: Proc | str, body: dict[str, Any]) -> None:
    """POST /internal/v1/faults，带 X-Internal-Token。"""
    r = httpx.post(
        f"{_ops_url(ops)}/internal/v1/faults",
        json=body,
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=30.0,
    )
    assert r.status_code == 200, f"faults 失败 status={r.status_code} body={r.text}"


@dataclass
class KbEnv:
    db_name: str
    ops: Proc
    rag: Proc

    @property
    def ops_url(self) -> str:
        return self.ops.url

    @property
    def rag_url(self) -> str:
        return self.rag.url

    def token(self, role: str, user: str, tenant: str) -> dict[str, str]:
        return token(self.ops, role, user, tenant)

    def faults(self, body: dict[str, Any]) -> None:
        faults(self.ops, body)

    def rag_fault(self, mode: str) -> None:
        r = httpx.post(
            f"{self.rag_url}/stub/admin/fault",
            json={"mode": mode},
            timeout=30.0,
        )
        assert r.status_code == 200, f"RAG fault 设置失败 status={r.status_code} body={r.text}"

    def rag_chunk_count(self, chunk_id: str) -> int:
        r = httpx.get(
            f"{self.rag_url}/stub/admin/chunks",
            params={"chunk_id": chunk_id},
            timeout=30.0,
        )
        assert r.status_code == 200, f"chunks 查询失败 status={r.status_code} body={r.text}"
        data = r.json()
        assert "count" in data, f"chunks 响应缺 count: {data}"
        return int(data["count"])

    def rag_total_chunks(self) -> int:
        r = httpx.get(f"{self.rag_url}/stub/admin/chunks/count", timeout=30.0)
        assert r.status_code == 200, f"chunks/count 失败 status={r.status_code} body={r.text}"
        data = r.json()
        assert "total" in data, f"chunks/count 响应缺 total: {data}"
        return int(data["total"])


@pytest.fixture
def kb_env():
    """function 级：独立测试库 + RAG 桩 + Ops（Pilot 指向不可达地址）。"""
    with test_database() as db_name:
        rag = Proc("stubs.rag_stub:app", free_port(), {}, OPS_DIR, sys.executable).start()
        ops = ops_proc(
            db_name,
            pilot_url="http://127.0.0.1:1",
            extra_env={"ECOMOPS_RAG_URL": rag.url},
        ).start()
        env = KbEnv(db_name=db_name, ops=ops, rag=rag)
        try:
            yield env
        finally:
            for p in (ops, rag):
                try:
                    p.stop()
                except Exception:
                    try:
                        p.stop(sig_kill=True)
                    except Exception:
                        pass
