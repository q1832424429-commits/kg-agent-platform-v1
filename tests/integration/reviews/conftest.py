"""提供第六阶段人工审核和发布门禁集成测试数据库。"""

import pytest_asyncio

from kg_agent.infrastructure.database.session import Database


@pytest_asyncio.fixture
async def database():
    """为每个审核测试创建并释放完整内存数据库。"""

    instance = Database("sqlite+aiosqlite:///:memory:")
    await instance.create_schema()
    try:
        yield instance
    finally:
        await instance.dispose()
