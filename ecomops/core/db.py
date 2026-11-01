"""PostgreSQL 连接：业务表走 SQLAlchemy 异步引擎(psycopg3)，LangGraph 检查点走 psycopg 连接池。"""
import asyncio

from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ecomops.core.config import get_settings

_engines: dict[tuple, AsyncEngine] = {}


def make_url(db_name: str | None = None, driver: str = "postgresql+psycopg") -> URL:
    s = get_settings()
    return URL.create(driver, s.db_user, s.db_password, s.db_host, s.db_port, db_name or s.db_name)


def conninfo(db_name: str | None = None) -> str:
    s = get_settings()
    return f"host={s.db_host} port={s.db_port} user={s.db_user} password={s.db_password} dbname={db_name or s.db_name}"


def get_engine() -> AsyncEngine:
    """按 (库名, 事件循环) 缓存，避免跨循环复用连接。"""
    key = (get_settings().db_name, id(asyncio.get_running_loop()))
    if key not in _engines:
        _engines[key] = create_async_engine(make_url(), pool_size=20, max_overflow=20, pool_pre_ping=True)
    return _engines[key]


async def dispose_engines() -> None:
    for e in list(_engines.values()):
        await e.dispose()
    _engines.clear()


def make_pool() -> AsyncConnectionPool:
    """LangGraph 检查点用：autocommit + dict_row（AsyncPostgresSaver 要求）。调用方负责 open()/close()。"""
    from psycopg.rows import dict_row
    return AsyncConnectionPool(conninfo(), min_size=1, max_size=10, open=False,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
