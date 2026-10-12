"""提供第三阶段 Artifact 集成测试使用的独立内存数据库。"""

import pytest_asyncio

from kg_agent.infrastructure.database.session import Database


@pytest_asyncio.fixture
async def database():
    """为每个 Artifact 测试创建并释放完整 ORM Schema。"""

    instance = Database("sqlite+aiosqlite:///:memory:")
    await instance.create_schema()
    try:
        yield instance
    finally:
        await instance.dispose()
