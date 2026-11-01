"""对象存储：优先 MinIO（配置了 endpoint 且可达），否则本地文件系统回退。接口一致。"""
import io
import uuid
from pathlib import Path

from ecomops.core.config import get_settings


class LocalStore:
    kind = "local"

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        key = f"{uuid.uuid4().hex[:12]}_{Path(name).name}"
        (self.root / key).write_bytes(data)
        return f"local://{key}"

    def get(self, path: str) -> bytes:
        return (self.root / path.removeprefix("local://")).read_bytes()


class MinioStore:
    kind = "minio"

    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        key = f"{uuid.uuid4().hex[:12]}_{Path(name).name}"
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        return f"minio://{self.bucket}/{key}"

    def get(self, path: str) -> bytes:
        key = path.removeprefix(f"minio://{self.bucket}/")
        r = self.client.get_object(self.bucket, key)
        try:
            return r.read()
        finally:
            r.close()
            r.release_conn()


def get_store():
    s = get_settings()
    if s.minio_endpoint:
        try:
            from minio import Minio
            return MinioStore(Minio(s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=False), s.minio_bucket)
        except Exception:
            pass
    return LocalStore(s.object_dir)
