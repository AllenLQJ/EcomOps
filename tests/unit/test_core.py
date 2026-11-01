from sqlalchemy import text

from ecomops.core.db import get_engine


async def test_schema_loaded(db_name):
    async with get_engine().connect() as c:
        n = (await c.execute(text("select count(*) from information_schema.tables where table_schema='public'"))).scalar()
    assert n >= 9


async def test_checkpointer_setup(db_name):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from ecomops.core.db import make_pool
    pool = make_pool()
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        async with pool.connection() as c:
            cur = await c.execute("select count(*) as n from information_schema.tables where table_name='checkpoints'")
            assert (await cur.fetchone())["n"] == 1
    finally:
        await pool.close()
