"""测试栈辅助（统筹维护）：起 EcomOps 进程（可重启）、真实 EcomPilot 栈（MySQL 测试库 + NLU 桩 + Pilot）、故障代理。

EcomPilot 目录由环境变量 ECOMPILOT_DIR 指定（默认同级 ../EcomPilot），使用它自己的 .venv。
"""
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymysql

OPS_DIR = Path(__file__).resolve().parents[1]
PILOT_DIR = Path(os.environ.get("ECOMPILOT_DIR", OPS_DIR.parent / "EcomPilot"))
PILOT_PY = str(PILOT_DIR / ".venv/bin/python")
INTERNAL_TOKEN = "dev-internal-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"not ready: {url}")


@dataclass
class Proc:
    module: str
    port: int
    env: dict
    cwd: Path
    python: str
    popen: subprocess.Popen | None = None
    log: Path | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "Proc":
        full = os.environ.copy()
        full.update(self.env)
        full["PYTHONPATH"] = str(self.cwd)
        out = open(self.log, "ab") if self.log else subprocess.DEVNULL
        self.popen = subprocess.Popen([self.python, "-m", "uvicorn", self.module, "--host", "127.0.0.1",
                                       "--port", str(self.port), "--log-level", "warning"],
                                      cwd=str(self.cwd), env=full, stdout=out, stderr=out)
        wait_http(self.url + "/health")
        return self

    def stop(self, sig_kill: bool = False) -> None:
        if self.popen and self.popen.poll() is None:
            self.popen.kill() if sig_kill else self.popen.terminate()
            try:
                self.popen.wait(10)
            except subprocess.TimeoutExpired:
                self.popen.kill()
        self.popen = None

    def restart(self, sig_kill: bool = True) -> "Proc":
        """模拟进程重启（默认 SIGKILL，模拟崩溃）。"""
        self.stop(sig_kill=sig_kill)
        return self.start()


def ops_proc(db_name: str, *, port: int | None = None, pilot_url: str, extra_env: dict | None = None) -> Proc:
    env = {"ECOMOPS_DB_NAME": db_name, "ECOMOPS_PILOT_URL": pilot_url, "ECOMOPS_DEV_TOKENS": "1",
           "ECOMOPS_ENABLE_FAULTS": "1", "ECOMOPS_RECONCILE_INTERVAL_SECONDS": "1",
           "ECOMOPS_OUTBOX_BACKOFF_SECONDS": "1", "ECOMOPS_REFUND_CALLBACK_TIMEOUT_SECONDS": "2",
           "ECOMOPS_INTERNAL_TOKEN": INTERNAL_TOKEN, "ECOMOPS_PILOT_INTERNAL_TOKEN": INTERNAL_TOKEN}
    env.update(extra_env or {})
    return Proc("ecomops.main:app", port or free_port(), env, OPS_DIR, sys.executable)


@dataclass
class PilotStack:
    mysql_db: str
    pilot: Proc
    nlu: Proc
    proxy: Proc | None = None
    ops_port: int = 0

    @property
    def pilot_url(self) -> str:
        return self.pilot.url

    def mysql(self):
        return pymysql.connect(host="127.0.0.1", port=3307, user="root", password="ecompilot_root",
                               database=self.mysql_db, autocommit=True, cursorclass=pymysql.cursors.DictCursor)

    def query(self, sql: str, *args):
        with self.mysql() as c, c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def dev_token(self, user_id: str) -> dict:
        t = httpx.post(self.pilot.url + "/api/v1/dev/token", json={"user_id": user_id}).json()["token"]
        return {"Authorization": f"Bearer {t}"}

    def make_fulfilled_order(self, user_id: str, sku_id: str = "SKU_HEATER_003") -> dict:
        """走 Pilot API：报价→下单→支付→发货（promo_rules 已清空，热水器实付恰为 112000 分）。返回含 headers/order_id/order_item_id。"""
        h = self.dev_token(user_id)
        with httpx.Client(base_url=self.pilot.url, timeout=30) as c:
            q = c.post("/api/v1/quotes", headers=h, json={"items": [{"sku_id": sku_id, "qty": 1}]}).json()
            o = c.post("/api/v1/orders/create", headers=h,
                       json={"idempotency_key": f"k-{user_id}-{time.time_ns()}", "quote_id": q["quote_id"]}).json()
            c.post(f"/api/v1/orders/{o['order_id']}/pay", headers=h)
            c.post(f"/internal/v1/orders/{o['order_id']}/fulfill", headers={"X-Internal-Token": INTERNAL_TOKEN})
        return {"headers": h, "order_id": o["order_id"], "order_item_id": o["items"][0]["order_item_id"],
                "payable_cents": o["total_payable_cents"]}


def _pilot_py(code: str) -> str:
    r = subprocess.run([PILOT_PY, "-c", code], cwd=str(PILOT_DIR), capture_output=True, text=True, check=True)
    return r.stdout.strip().splitlines()[-1]


@contextmanager
def pilot_stack(ops_url_for_pilot: str, with_proxy: bool = False, proxy_upstream_ops: str | None = None):
    """起真实 EcomPilot：独立 MySQL 测试库（清空 promo_rules）+ NLU 桩 + Pilot。ops_url_for_pilot 为 Pilot 提交工单的 Ops 地址。"""
    name = _pilot_py("from pilot.core.testdb import create_test_db; print(create_test_db())")
    nlu = pilot = None
    try:
        with pymysql.connect(host="127.0.0.1", port=3307, user="root", password="ecompilot_root", database=name,
                             autocommit=True) as c, c.cursor() as cur:
            cur.execute("DELETE FROM promo_rules")
        nlu = Proc("stubs.nlu_stub:app", free_port(), {}, PILOT_DIR, PILOT_PY).start()
        pilot = Proc("pilot.main:app", free_port(), {
            "ECOMPILOT_DB_NAME": name, "ECOMPILOT_DEV_TOKENS": "1", "ECOMPILOT_INTERNAL_TOKEN": INTERNAL_TOKEN,
            "ECOMPILOT_OPS_URL": ops_url_for_pilot, "ECOMPILOT_NLU_URL": nlu.url}, PILOT_DIR, PILOT_PY).start()
        yield PilotStack(name, pilot, nlu)
    finally:
        for p in (pilot, nlu):
            if p:
                p.stop()
        _pilot_py(f"from pilot.core.testdb import drop_test_db; drop_test_db('{name}'); print('dropped')")


@contextmanager
def fault_proxy(upstream: str):
    """故障代理：转发到 upstream（真实 Pilot）。POST /proxy/mode {"drop_response_first_n": N}：
    前 N 个 /internal/v1/refunds/execute 请求**先转发给 Pilot（Pilot 实际执行并提交）**，再丢弃响应并拖延 sleep_seconds（默认 5）秒，让调用方读超时。"""
    p = Proc("tests.fault_proxy_app:app", free_port(), {"PROXY_UPSTREAM": upstream}, OPS_DIR, sys.executable).start()
    try:
        yield p
    finally:
        p.stop()
