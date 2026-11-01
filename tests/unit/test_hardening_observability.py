import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecomops.core import hardening, observability
from ecomops.core.config import Settings

GOOD = dict(jwt_secret="x" * 40, internal_token="t" * 24, pilot_internal_token="p" * 24, db_password="not-the-default")


def test_dev_mode_never_blocks():
    hardening.enforce(Settings(env="dev", dev_tokens=True, enable_faults=True))


def test_prod_with_defaults_is_refused_without_echoing_values():
    with pytest.raises(RuntimeError) as info:
        hardening.enforce(Settings(env="prod"))
    msg = str(info.value)
    for name in ("jwt_secret", "internal_token", "pilot_internal_token", "db_password"):
        assert name in msg
    assert "dev-secret-change-me" not in msg and "ecomops_pw" not in msg


def test_prod_with_strong_secrets_starts():
    hardening.enforce(Settings(env="prod", **GOOD))


def test_prod_refuses_dev_entrances_short_secrets_and_default_minio_key():
    problems = hardening.production_problems(Settings(env="prod", jwt_secret="short", internal_token="short",
                                                      pilot_internal_token="p" * 24, db_password="x", dev_tokens=True,
                                                      enable_faults=True, minio_endpoint="minio:9000"))
    joined = "；".join(problems)
    for needle in ("jwt_secret 少于", "internal_token 少于", "DEV_TOKENS", "ENABLE_FAULTS", "minio_secret_key"):
        assert needle in joined


def _app():
    app = FastAPI()
    observability.install(app)

    @app.get("/claims/{claim_no}")
    def claim(claim_no: str):
        return {"id": claim_no}
    return app


def test_request_id_echoed_generated_and_untrusted_values_replaced():
    client = TestClient(_app())
    assert client.get("/claims/A", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    assert len(client.get("/claims/A").headers["x-request-id"]) == 32
    bad = client.get("/claims/A", headers={"X-Request-ID": "x y\t<script>"}).headers["x-request-id"]
    assert bad != "x y\t<script>" and len(bad) == 32


def test_access_log_is_json_with_route_template(caplog):
    logger = logging.getLogger("ecomops.access")
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="ecomops.access")
    TestClient(_app()).get("/claims/CLM_SECRET_1", headers={"X-Request-ID": "rid-1"})
    record = json.loads(caplog.records[-1].message)
    assert record["request_id"] == "rid-1" and record["route"] == "/claims/{claim_no}" and record["status"] == 200
    assert "CLM_SECRET_1" not in caplog.records[-1].message


def test_metrics_use_route_templates_and_survive_db_down():
    client = TestClient(_app())      # 没有数据库：出箱指标读不到也不能让 /metrics 失败
    for no in ("A", "B"):
        client.get(f"/claims/{no}")
    text = client.get("/metrics").text
    assert 'ecomops_http_requests_total{method="GET",route="/claims/{claim_no}",status="200"}' in text
    assert "/claims/A" not in text
