"""创建异步数据库引擎、Session 工厂和测试 Schema。"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kg_agent.infrastructure.database.base import Base


class Database:
    """管理 SQLAlchemy 异步引擎及短生命周期 Session。"""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        """根据连接串初始化引擎，但不在构造时执行网络连接。"""

        self.engine: AsyncEngine = create_async_engine(database_url, echo=echo)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def create_schema(self) -> None:
        """创建全部表；只供测试使用，部署环境必须使用 Alembic。"""

        from kg_agent.infrastructure.database import models  # noqa: F401

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_schema(self) -> None:
        """删除全部测试表；生产代码不得调用该方法。"""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        """关闭连接池并释放数据库资源。"""

        await self.engine.dispose()
