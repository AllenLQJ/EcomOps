"""测试库工厂：ecomops_test_<uuid8>，导入 schema，结束删除。LangGraph 检查点表由应用启动时 setup() 创建。"""
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg

from ecomops.core import config, db

ROOT = Path(__file__).resolve().parents[2]


def _admin():
    s = config.get_settings()
    return psycopg.connect(f"host={s.db_host} port={s.db_port} user={s.db_user} password={s.db_password} dbname=postgres",
                           autocommit=True)


def create_test_db() -> str:
    name = f"ecomops_test_{uuid.uuid4().hex[:8]}"
    with _admin() as c:
        c.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(db.conninfo(name), autocommit=True) as c:
        c.execute((ROOT / "contracts/schema.sql").read_text(encoding="utf-8"))
    return name


def drop_test_db(name: str) -> None:
    assert name.startswith("ecomops_test_")
    with _admin() as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def test_database():
    name = create_test_db()
    old = os.environ.get("ECOMOPS_DB_NAME")
    os.environ["ECOMOPS_DB_NAME"] = name
    config.get_settings.cache_clear()
    try:
        yield name
    finally:
        if old is None:
            os.environ.pop("ECOMOPS_DB_NAME", None)
        else:
            os.environ["ECOMOPS_DB_NAME"] = old
        config.get_settings.cache_clear()
        drop_test_db(name)
