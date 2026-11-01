"""MinIO 后端走 S3 协议的路径，用 moto 的 S3 仿真服务验证（真实 MinIO 镜像本机拉不下来：Docker Hub 拒绝、quay 401）。
这证明 MinioStore / get_store 的客户端逻辑和 S3 交互是对的，但**不等于真实 MinIO 服务**已经实测。"""
import socket

import pytest

moto_server = pytest.importorskip("moto.server")
from minio import Minio  # noqa: E402

from ecomops.core import config  # noqa: E402
from ecomops.core.storage import LocalStore, MinioStore, get_store  # noqa: E402


@pytest.fixture()
def s3():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop()


def client(endpoint):
    return Minio(endpoint, "ak", "sk", secure=False)


def test_put_get_roundtrip_creates_bucket_and_binary_safe(s3):
    store = MinioStore(client(s3), "ecomops-docs")
    data = bytes(range(256)) * 100
    path = store.put("../../evil/商品 详情.pdf", data, "application/pdf")
    assert path.startswith("minio://ecomops-docs/") and path.endswith("_商品 详情.pdf")
    assert ".." not in path and "evil" not in path                      # 文件名里的路径片段被剥掉
    assert store.get(path) == data
    assert MinioStore(client(s3), "ecomops-docs").get(path) == data      # 第二个实例复用已存在的桶，仍能读到


def test_same_name_uploads_do_not_overwrite_each_other(s3):
    store = MinioStore(client(s3), "dup-check")
    first, second = store.put("a.pdf", b"one"), store.put("a.pdf", b"two")
    assert first != second and store.get(first) == b"one" and store.get(second) == b"two"


def test_get_store_prefers_minio_when_configured_and_reachable(s3, monkeypatch, tmp_path):
    for key, value in (("ENDPOINT", s3), ("ACCESS_KEY", "ak"), ("SECRET_KEY", "sk"), ("BUCKET", "listing-docs")):
        monkeypatch.setenv("ECOMOPS_MINIO_" + key, value)
    monkeypatch.setenv("ECOMOPS_OBJECT_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    try:
        store = get_store()
        assert store.kind == "minio" and store.bucket == "listing-docs"
        assert store.get(store.put("x.pdf", b"%PDF")) == b"%PDF"
    finally:
        config.get_settings.cache_clear()


def test_get_store_falls_back_to_local_files_when_minio_is_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("ECOMOPS_MINIO_ENDPOINT", "127.0.0.1:9")
    monkeypatch.setenv("ECOMOPS_OBJECT_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    try:
        store = get_store()
        assert isinstance(store, LocalStore) and store.get(store.put("x.pdf", b"data")) == b"data"
    finally:
        config.get_settings.cache_clear()
