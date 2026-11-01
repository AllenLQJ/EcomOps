import pytest

from ecomops.core.testdb import test_database


@pytest.fixture()
def db_name():
    """每个测试一个独立库（含 schema）。"""
    with test_database() as name:
        yield name
