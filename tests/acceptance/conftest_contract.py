"""OpenAPI 契约测试共用：ops_only fixture、schema 校验器、错误码表。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator

from ecomops.core.testdb import test_database
from tests.stack import INTERNAL_TOKEN, free_port, ops_proc, wait_http

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = ROOT / "contracts" / "openapi" / "ecomops.yaml"
ERRORS_PATH = ROOT / "contracts" / "errors.md"

_STRIP_KEYS = frozenset({
    "description", "example", "examples", "title", "format", "xml",
    "externalDocs", "deprecated", "readOnly", "writeOnly", "summary",
    "discriminator", "nullable",
})


def load_openapi() -> dict:
    with OPENAPI_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_error_codes() -> set[str]:
    """从 contracts/errors.md 表格提取 code 集合。"""
    text = ERRORS_PATH.read_text(encoding="utf-8")
    codes: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"\|\s*\d+\s*\|\s*([A-Z][A-Z0-9_]*)\s*\|", line.strip())
        if m:
            codes.add(m.group(1))
    if not codes:
        raise RuntimeError(f"failed to parse error codes from {ERRORS_PATH}")
    return codes


def _resolve_ref(doc: dict, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref (only local): {ref}")
    node: Any = doc
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"$ref not found: {ref}")
        node = node[part]
    return node


def openapi_schema_to_jsonschema(doc: dict, schema: Any) -> Any:
    """OpenAPI 3.0 schema → JSON Schema：$ref 内联、nullable→类型联合、剔除非校验关键字。"""
    if schema is True or schema is False:
        return schema
    if not isinstance(schema, dict):
        return schema

    if "$ref" in schema:
        return openapi_schema_to_jsonschema(doc, _resolve_ref(doc, schema["$ref"]))

    nullable = bool(schema.get("nullable"))
    out: dict[str, Any] = {}

    for key, value in schema.items():
        if key in _STRIP_KEYS or key == "$ref":
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {pk: openapi_schema_to_jsonschema(doc, pv) for pk, pv in value.items()}
        elif key == "items":
            out[key] = openapi_schema_to_jsonschema(doc, value)
        elif key == "additionalProperties":
            out[key] = openapi_schema_to_jsonschema(doc, value) if isinstance(value, dict) else value
        elif key in ("allOf", "oneOf", "anyOf") and isinstance(value, list):
            out[key] = [openapi_schema_to_jsonschema(doc, x) for x in value]
        else:
            out[key] = value

    if nullable:
        t = out.get("type")
        if t is None:
            return {"anyOf": [out, {"type": "null"}]}
        if isinstance(t, list):
            if "null" not in t:
                out["type"] = [*t, "null"]
        else:
            out["type"] = [t, "null"]
    return out


def match_path_template(doc: dict, concrete: str) -> str:
    paths = doc.get("paths") or {}
    if concrete in paths:
        return concrete
    concrete_parts = concrete.strip("/").split("/")
    for template in paths:
        t_parts = template.strip("/").split("/")
        if len(t_parts) != len(concrete_parts):
            continue
        if all(
            (tp.startswith("{") and tp.endswith("}")) or tp == cp
            for tp, cp in zip(t_parts, concrete_parts)
        ):
            return template
    raise AssertionError(f"path {concrete!r} not found in OpenAPI paths")


def declared_status_codes(doc: dict, concrete_path: str, method: str) -> set[str]:
    template = match_path_template(doc, concrete_path)
    op = (doc["paths"][template] or {}).get(method.lower())
    if not op:
        raise AssertionError(f"OpenAPI has no {method.upper()} {template}")
    return {str(code) for code in (op.get("responses") or {})}


def response_body_schema(doc: dict, concrete_path: str, method: str, status: int) -> Any | None:
    template = match_path_template(doc, concrete_path)
    op = doc["paths"][template][method.lower()]
    resp = (op.get("responses") or {}).get(str(status))
    if resp is None:
        return None
    if isinstance(resp, dict) and "$ref" in resp:
        resp = _resolve_ref(doc, resp["$ref"])
    content = (resp or {}).get("content") or {}
    app_json = content.get("application/json") or {}
    schema = app_json.get("schema")
    if schema is None:
        return None
    return openapi_schema_to_jsonschema(doc, schema)


def validate_json(schema: Any, data: Any, *, where: str) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return
    parts = []
    for err in errors[:8]:
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        parts.append(f"{path}: {err.message}")
    raise AssertionError(f"schema validation failed for {where}:\n  " + "\n  ".join(parts))


def assert_status_declared(doc: dict, concrete_path: str, method: str, status: int) -> None:
    allowed = declared_status_codes(doc, concrete_path, method)
    assert str(status) in allowed, (
        f"{method.upper()} {concrete_path} returned {status}, "
        f"not in OpenAPI responses {sorted(allowed)}"
    )


def assert_error_body(doc: dict, codes: set[str], body: Any, *, where: str) -> None:
    schema = openapi_schema_to_jsonschema(doc, {"$ref": "#/components/schemas/Error"})
    validate_json(schema, body, where=where)
    code = body["error"]["code"]
    assert code in codes, f"{where}: error.code={code!r} not in errors.md {sorted(codes)}"


@dataclass
class OpsHarness:
    url: str
    db_name: str
    rag_url: str

    @property
    def internal(self) -> dict[str, str]:
        return {"X-Internal-Token": INTERNAL_TOKEN}

    def auth(self, user_id: str, role: str, tenant_id: str = "SHOP_SELF_01") -> dict[str, str]:
        r = httpx.post(
            f"{self.url}/api/v1/dev/token",
            json={"user_id": user_id, "role": role, "tenant_id": tenant_id},
            timeout=30,
        )
        assert r.status_code == 200, f"dev/token failed: {r.status_code} {r.text[:300]}"
        return {"Authorization": f"Bearer {r.json()['token']}"}


def _start_rag_stub() -> tuple[subprocess.Popen, str]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "stubs.rag_stub:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_http(url + "/health", timeout=30.0)
    except Exception:
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return proc, url


@pytest.fixture
def openapi_doc() -> dict:
    return load_openapi()


@pytest.fixture
def error_codes() -> set[str]:
    return load_error_codes()


@pytest.fixture
def ops_only():
    """独立库 + RAG 桩 + Ops（Pilot 指向不可达地址）。function 级。"""
    with test_database() as db_name:
        rag_proc, rag_url = _start_rag_stub()
        ops = None
        try:
            ops = ops_proc(
                db_name,
                pilot_url="http://127.0.0.1:1",
                extra_env={"ECOMOPS_RAG_URL": rag_url},
            ).start()
            yield OpsHarness(url=ops.url, db_name=db_name, rag_url=rag_url)
        finally:
            if ops is not None:
                ops.stop()
            if rag_proc.poll() is None:
                rag_proc.terminate()
                try:
                    rag_proc.wait(10)
                except subprocess.TimeoutExpired:
                    rag_proc.kill()
                    rag_proc.wait(5)
