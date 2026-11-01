"""W3 故障注入实测（对运行中的联合栈）：Pilot 宕机后恢复、EcomOps SIGKILL 重启后审批、重复/并发批准、
LLM 降级、KB 发布回写失败重试。需先 scripts/start_with_pilot.sh。"""
import os, signal, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
import httpx

P, O, TOK = "http://127.0.0.1:8085", "http://127.0.0.1:8000", "dev-internal-token"
PILOT_DIR = os.environ.get("ECOMPILOT_DIR", "../EcomPilot")
INT = {"X-Internal-Token": TOK}
ok = True


def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, info)


def tok(url, **body):
    return {"Authorization": "Bearer " + httpx.post(url + "/api/v1/dev/token", json=body).json()["token"]}


SUP = tok(O, user_id="SUP_H", role="supervisor")
OPR = tok(O, user_id="OP_H", role="operator")


def new_claim(reason="漏水打不着火"):
    h = tok(P, user_id=f"HB_{time.time_ns()}")
    c = httpx.Client(base_url=P, headers=h, timeout=30)
    q = c.post("/api/v1/quotes", json={"items": [{"sku_id": "SKU_HEATER_003", "qty": 1}]}).json()
    o = c.post("/api/v1/orders/create", json={"idempotency_key": f"h{time.time_ns()}", "quote_id": q["quote_id"]}).json()
    c.post(f"/api/v1/orders/{o['order_id']}/pay")
    c.post(f"/internal/v1/orders/{o['order_id']}/fulfill", headers=INT)
    r = c.post("/api/v1/aftersales/confirm", json={"order_id": o["order_id"], "order_item_id": o["items"][0]["order_item_id"],
                                                  "buyer_reason_text": reason, "evidence_urls": ["https://x.invalid/e.jpg"]})
    return r.json()["claim_no"], o["order_id"], c, o["total_payable_cents"]


def status(no):
    return httpx.get(f"{O}/api/v1/claims/{no}", headers=SUP).json()


def wait(no, targets, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        d = status(no)
        if d["claim_status"] in targets:
            return d
        time.sleep(0.5)
    return status(no)


def pid_of(path):
    return int(open(path).read())


# 1. 回调时 Pilot 宕机 → CALLBACK_RETRYING，恢复后 COMPLETED，Pilot 侧只 1 次退款
no, oid, pc, pay = new_claim()
check("claim pending supervisor", wait(no, {"PENDING_SUPERVISOR_REVIEW"})["claim_status"] == "PENDING_SUPERVISOR_REVIEW")
os.kill(pid_of(f"{PILOT_DIR}/.run/pilot.pid"), signal.SIGKILL); time.sleep(1)
r = httpx.post(f"{O}/api/v1/claims/{no}/confirm", json={"action": "APPROVE"}, headers=SUP)
check("approve while Pilot down -> 200", r.status_code == 200, r.status_code)
d = wait(no, {"CALLBACK_RETRYING"}, 20)
check("Pilot down -> CALLBACK_RETRYING", d["claim_status"] == "CALLBACK_RETRYING", d["claim_status"])
env = dict(os.environ, SKIP_OPS_STUB="1", SKIP_RAG_STUB="1")
subprocess.run([f"{PILOT_DIR}/scripts/start_all.sh"], cwd=PILOT_DIR, env=env, capture_output=True)
d = wait(no, {"COMPLETED"}, 60)
check("Pilot back -> COMPLETED", d["claim_status"] == "COMPLETED", d["claim_status"])
st = httpx.get(f"{P}/internal/v1/refunds/status", params={"refund_idempotency_key": f"REFUND_{no}_V1"}, headers=INT).json()
check("single refund of full amount", st["execution_status"] == "SUCCESS" and st["refunded_cents"] == pay, st)

# 2. EcomOps SIGKILL 重启后仍可审批（检查点持久）
no2, oid2, pc2, pay2 = new_claim()
wait(no2, {"PENDING_SUPERVISOR_REVIEW"})
os.kill(pid_of(".run/ecomops.pid"), signal.SIGKILL); time.sleep(1)
subprocess.run(["./scripts/start_all.sh"], env=dict(os.environ, SKIP_RAG_STUB="1"), capture_output=True)
SUP = tok(O, user_id="SUP_H", role="supervisor")
check("after SIGKILL restart still pending", status(no2)["claim_status"] == "PENDING_SUPERVISOR_REVIEW")
with ThreadPoolExecutor(2) as ex:
    rs = list(ex.map(lambda _: httpx.post(f"{O}/api/v1/claims/{no2}/confirm", json={"action": "APPROVE"}, headers=SUP), range(2)))
check("two concurrent approves -> both 200", [r.status_code for r in rs] == [200, 200], [r.text[:80] for r in rs])
d = wait(no2, {"COMPLETED"})
check("COMPLETED with exactly one approval", d["claim_status"] == "COMPLETED" and len(d["approvals"]) == 1, d["claim_status"])

# 3. 主管驳回 → Pilot 订单恢复 FULFILLED
no3, oid3, pc3, _ = new_claim()
wait(no3, {"PENDING_SUPERVISOR_REVIEW"})
httpx.post(f"{O}/api/v1/claims/{no3}/confirm", json={"action": "REJECT", "reason": "不符合"}, headers=SUP)
time.sleep(3)
check("reject -> order back to FULFILLED", pc3.get(f"/api/v1/orders/{oid3}").json()["order_status"] == "FULFILLED")

# 4. LLM 降级 → 不放行，转人工
httpx.post(f"{O}/internal/v1/faults", json={"llm_fail": ["evidence_track"]}, headers=INT)
no4, *_ = new_claim("坏了")
d = wait(no4, {"PENDING_SUPERVISOR_REVIEW", "COMPLETED", "APPROVED_PENDING_CALLBACK"})
check("llm degraded -> manual review, fallback_used", d["claim_status"] == "PENDING_SUPERVISOR_REVIEW" and d["fallback_used"], d["claim_status"])
httpx.post(f"{O}/internal/v1/faults", json={}, headers=INT)

# 5. KB 发布：回写失败 → 停留 PUBLISHING → retry → PUBLISHED，RAG 中仅 1 条
k = httpx.post(f"{O}/api/v1/kb/ask", json={"query": f"请问海外仓补发时效是多久 {time.time_ns()}"}, headers=OPR).json()
qid = k["enqueued_queue_id"]
httpx.post(f"{O}/internal/v1/faults", json={"publish_writeback_fail": True}, headers=INT)
pub = {"standard_answer": "海外仓补发 7 个工作日内发出", "catalog_category_id": "DAILY_GENERAL", "rule_source": "沙箱规则"}
r = httpx.post(f"{O}/api/v1/kb/pending/{qid}/publish", json=pub, headers=OPR).json()
check("writeback fail -> PUBLISHING", r["publish_status"] == "PUBLISHING", r["publish_status"])
httpx.post(f"{O}/internal/v1/faults", json={}, headers=INT)
r2 = httpx.post(f"{O}/api/v1/kb/pending/{qid}/retry", headers=OPR).json()
cnt = httpx.get("http://127.0.0.1:8002/stub/admin/chunks", params={"chunk_id": r2["deterministic_chunk_id"]}).json()
check("retry -> PUBLISHED, one chunk, same chunk_id", r2["publish_status"] == "PUBLISHED" and cnt["count"] == 1
      and r2["deterministic_chunk_id"] == r["deterministic_chunk_id"], (r2["publish_status"], cnt))
sys.exit(0 if ok else 1)
