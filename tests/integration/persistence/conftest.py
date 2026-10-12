"""提供第二阶段 SQLite 事务适配测试数据库。

SQLite 只验证 Repository 和事务组合；PostgreSQL 锁语义由单独的可选测试验证。
"""

import pytest_asyncio

from kg_agent.infrastructure.database.session import Database


@pytest_asyncio.fixture
async def database():
    """为每个测试创建独立内存数据库并在结束时释放连接池。"""

    instance = Database("sqlite+aiosqlite:///:memory:")
    await instance.create_schema()
    try:
        yield instance
    finally:
        await instance.dispose()
