"""管理已发布资产在目录中的显示名称和收录状态。

发布内容、checksum、Artifact 生命周期和血缘保持不可变；删除操作只退出
新任务可选目录，避免破坏已引用该版本的任务。内容修订仍须生成新的候选产物。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.infrastructure.database.models import ArtifactRefRow, MasterTaskRow


class PublishedAssetManagementService:
    """对正式发布版本执行单用户受控目录操作。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """绑定应用数据库，不接触不可变资产正文和对象存储。"""

        self._factory = session_factory

    async def _editable(self, session: AsyncSession, artifact_id: UUID) -> ArtifactRefRow:
        """锁定正式发布资产，排除任务内导入副本及其他用户资产。"""

        row = await session.get(ArtifactRefRow, artifact_id, with_for_update=True)
        if (
            row is None
            or row.artifact_type not in {"PublishedOntology", "PublishedGraph"}
            or row.payload.get("producer_skill") == "published_asset_import"
        ):
            raise LookupError("已发布资产不存在")
        if row.task_id is not None:
            owner = await session.get(MasterTaskRow, row.task_id)
            if owner is None or owner.user_id != "default-user":
                raise PermissionError("无权管理该资产")
        return row

    async def is_listed(self, artifact_id: UUID) -> bool:
        """检查资产仍在目录中；历史 Artifact 的普通读取由原有权限链路管理。"""

        async with self._factory() as session:
            try:
                row = await self._editable(session, artifact_id)
            except (LookupError, PermissionError):
                return False
            return row.status == "AVAILABLE" and row.payload.get("catalog_status") != "REMOVED"

    async def rename(self, artifact_id: UUID, title: str) -> dict[str, str]:
        """仅修改资产目录显示名称，不伪造已审核内容或 checksum。"""

        value = title.strip()
        if not value or len(value) > 120:
            raise ValueError("显示名称须为 1～120 个字符")
        async with self._factory() as session, session.begin():
            row = await self._editable(session, artifact_id)
            if row.status != "AVAILABLE" or row.payload.get("catalog_status") == "REMOVED":
                raise ValueError("已移出目录的资产不能再修改")
            payload = dict(row.payload)
            payload["catalog_title"] = value
            row.payload = payload
        return {"artifact_id": str(artifact_id), "catalog_title": value}

    async def retire(self, artifact_id: UUID) -> dict[str, str]:
        """从新任务可选目录移除版本；历史引用的 Artifact 仍为 AVAILABLE。"""

        async with self._factory() as session, session.begin():
            row = await self._editable(session, artifact_id)
            if row.status != "AVAILABLE" or row.payload.get("catalog_status") == "REMOVED":
                raise ValueError("该资产已移出目录")
            payload = dict(row.payload)
            payload["catalog_status"] = "REMOVED"
            row.payload = payload
        return {"artifact_id": str(artifact_id), "catalog_status": "REMOVED"}
