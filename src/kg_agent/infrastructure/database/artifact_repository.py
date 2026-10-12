"""实现 Artifact 元数据、内容定位和规范化血缘的 SQLAlchemy Repository。

本模块不校验业务 Schema、不访问对象存储，也不决定 Artifact 是否可复用。
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kg_agent.domain.artifact_models import ArtifactRecord, ArtifactRef
from kg_agent.domain.enums import ArtifactStatus
from kg_agent.exceptions import PersistenceError
from kg_agent.infrastructure.database.mappers import to_artifact_ref
from kg_agent.infrastructure.database.models import (
    ArtifactContentRow,
    ArtifactLineageRow,
    ArtifactRefRow,
)


class SqlAlchemyArtifactRepository:
    """在调用方事务中保存 ArtifactRef、对象定位和父级血缘。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定一个由应用服务管理事务边界的异步 Session。"""

        self._session = session

    async def add(self, record: ArtifactRecord) -> None:
        """原子插入 Artifact 元数据、内容定位和全部父级血缘。"""

        ref = record.ref
        self._session.add(
            ArtifactRefRow(
                artifact_id=ref.artifact_id,
                task_id=ref.task_id,
                artifact_type=ref.artifact_type,
                status=ref.status.value,
                payload=ref.model_dump(mode="json"),
                created_at=ref.created_at,
            )
        )
        # 三张表没有 ORM relationship，SQLAlchemy 无法据此推导插入依赖顺序。
        # 先显式落下父级 ArtifactRef，确保 PostgreSQL 立即外键检查不会先插入内容行。
        await self._session.flush()
        self._session.add(
            ArtifactContentRow(
                artifact_id=ref.artifact_id,
                object_key=record.object_key,
                media_type=record.media_type,
                size_bytes=record.size_bytes,
                created_at=ref.created_at,
            )
        )
        self._session.add_all(
            [
                ArtifactLineageRow(
                    child_artifact_id=ref.artifact_id,
                    parent_artifact_id=parent_id,
                )
                for parent_id in ref.parent_artifact_ids
            ]
        )
        await self._session.flush()

    async def get(self, artifact_id: UUID, *, for_update: bool = False) -> ArtifactRecord | None:
        """读取 Artifact 完整存储记录，可选择锁定元数据行。"""

        query = (
            select(ArtifactRefRow, ArtifactContentRow)
            .join(
                ArtifactContentRow,
                ArtifactContentRow.artifact_id == ArtifactRefRow.artifact_id,
            )
            .where(ArtifactRefRow.artifact_id == artifact_id)
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).one_or_none()
        if row is None:
            return None
        ref_row, content_row = row
        return ArtifactRecord(
            ref=to_artifact_ref(ref_row),
            object_key=content_row.object_key,
            media_type=content_row.media_type,
            size_bytes=content_row.size_bytes,
        )

    async def list_for_task(self, task_id: UUID) -> list[ArtifactRecord]:
        """按创建时间稳定返回任务的全部 Artifact 记录。"""

        rows = await self._session.execute(
            select(ArtifactRefRow, ArtifactContentRow)
            .join(
                ArtifactContentRow,
                ArtifactContentRow.artifact_id == ArtifactRefRow.artifact_id,
            )
            .where(ArtifactRefRow.task_id == task_id)
            .order_by(ArtifactRefRow.created_at, ArtifactRefRow.artifact_id)
        )
        return [
            ArtifactRecord(
                ref=to_artifact_ref(ref_row),
                object_key=content_row.object_key,
                media_type=content_row.media_type,
                size_bytes=content_row.size_bytes,
            )
            for ref_row, content_row in rows
        ]

    async def get_parent_refs(self, artifact_id: UUID) -> list[ArtifactRef]:
        """按父 Artifact ID 返回规范化血缘引用。"""

        rows = await self._session.scalars(
            select(ArtifactRefRow)
            .join(
                ArtifactLineageRow,
                ArtifactLineageRow.parent_artifact_id == ArtifactRefRow.artifact_id,
            )
            .where(ArtifactLineageRow.child_artifact_id == artifact_id)
            .order_by(ArtifactRefRow.artifact_id)
        )
        return [to_artifact_ref(row) for row in rows]

    async def update_status(self, artifact_id: UUID, status: ArtifactStatus) -> None:
        """仅更新生命周期状态，并同步 ArtifactRef JSON 快照。"""

        row = await self._session.get(ArtifactRefRow, artifact_id)
        if row is None:
            raise PersistenceError("Artifact 不存在")
        payload = dict(row.payload)
        payload["status"] = status.value
        row.status = status.value
        row.payload = payload
        await self._session.flush()

    async def task_usage(self, task_id: UUID, artifact_type: str | None = None) -> tuple[int, int]:
        """返回任务中指定类型或全部 Artifact 的数量和总字节数。"""

        query = (
            select(
                func.count(ArtifactRefRow.artifact_id),
                func.sum(ArtifactContentRow.size_bytes),
            )
            .join(
                ArtifactContentRow,
                ArtifactContentRow.artifact_id == ArtifactRefRow.artifact_id,
            )
            .where(ArtifactRefRow.task_id == task_id)
        )
        if artifact_type is not None:
            query = query.where(ArtifactRefRow.artifact_type == artifact_type)
        values = await self._session.execute(query)
        count, total = values.one()
        return int(count or 0), int(total or 0)
