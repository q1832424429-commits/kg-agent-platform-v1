"""提供会话目录、会话级实时查询与彻底删除服务。

本模块管理用户可见聊天会话，不参与总控 Agent 的任务规划。一个会话可以包含多个
MasterTask；每个 MasterTask 的 SharedContext 仍保持独立，避免不同业务目标混写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.infrastructure.database.models import (
    AgentAssignmentRow,
    AgentRunResultRow,
    AgentSubTaskRow,
    ApiRequestRow,
    ArtifactContentRow,
    ArtifactLineageRow,
    ArtifactRefRow,
    ConversationMessageRow,
    ConversationRow,
    DecompositionRevisionRow,
    ExecutionTraceRow,
    HumanReviewRequestRow,
    MasterTaskRow,
    PublishedGraphRow,
    SharedContextSnapshotRow,
    SubTaskDependencyRow,
    TaskEventRow,
    TaskSpecRevisionRow,
)
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.services.run_log_service import RunLogService


@dataclass(frozen=True, slots=True)
class ConversationDeletionResult:
    """返回会话清理的用户可见统计，不暴露内部 SQL 或存储凭据。"""

    conversation_id: UUID
    deleted_task_count: int
    deleted_artifact_count: int
    retained_published_asset_count: int
    deleted_memory_count: int
    cleanup_warnings: tuple[str, ...] = ()


class ConversationService:
    """以会话为入口查询聊天历史，并协调受控的物理数据清理。

    删除仅作用于会话私有的任务、消息、审核、执行轨迹、普通 Artifact 与任务日志。
    PublishedOntology / PublishedGraph 会脱离来源任务并留在资产目录，供其他会话继续复用。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: ObjectStorage,
        memory: LongTermMemoryService,
        run_logs: RunLogService,
    ) -> None:
        """绑定同一环境的数据库、对象存储、长期记忆与诊断日志出口。"""

        self._factory = session_factory
        self._storage = storage
        self._memory = memory
        self._run_logs = run_logs

    async def list_conversations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """按最新活动时间列出会话，不把每个 MasterTask 重复显示到侧栏。"""

        async with self._factory() as session:
            task_counts = (
                select(
                    MasterTaskRow.conversation_id.label("conversation_id"),
                    func.count(MasterTaskRow.task_id).label("task_count"),
                    func.max(MasterTaskRow.updated_at).label("latest_task_at"),
                )
                .group_by(MasterTaskRow.conversation_id)
                .subquery()
            )
            rows = await session.execute(
                select(ConversationRow, task_counts.c.task_count, task_counts.c.latest_task_at)
                .outerjoin(
                    task_counts, task_counts.c.conversation_id == ConversationRow.conversation_id
                )
                .order_by(
                    func.coalesce(task_counts.c.latest_task_at, ConversationRow.updated_at).desc()
                )
                .limit(limit)
            )
            return [
                {
                    "conversation_id": row.conversation_id,
                    "title": row.title,
                    "task_count": int(task_count or 0),
                    "created_at": row.created_at,
                    "updated_at": latest_task_at or row.updated_at,
                }
                for row, task_count, latest_task_at in rows
            ]

    async def conversation_snapshot(self, conversation_id: UUID) -> dict[str, Any] | None:
        """返回一条会话的完整公开消息和所属任务摘要，用于刷新后重建聊天界面。"""

        async with self._factory() as session:
            conversation = await session.get(ConversationRow, conversation_id)
            if conversation is None:
                return None
            messages = list(
                await session.scalars(
                    select(ConversationMessageRow)
                    .where(ConversationMessageRow.conversation_id == conversation_id)
                    .order_by(ConversationMessageRow.created_at, ConversationMessageRow.message_id)
                )
            )
            tasks = list(
                await session.scalars(
                    select(MasterTaskRow)
                    .where(MasterTaskRow.conversation_id == conversation_id)
                    .order_by(MasterTaskRow.updated_at.desc(), MasterTaskRow.task_id)
                )
            )
            last_event_sequence = await session.scalar(
                select(func.max(TaskEventRow.sequence))
                .join(MasterTaskRow, MasterTaskRow.task_id == TaskEventRow.task_id)
                .where(MasterTaskRow.conversation_id == conversation_id)
            )
            # 完成回复由已提交的 TaskSpec 与最终 Artifact 确定性投影，刷新页面、
            # SSE 重放或协调器重复检查都不会多写一条聊天消息。
            deliverable_tasks = [item for item in tasks if item.status != "CANCELLED"]
            task_ids = [item.task_id for item in deliverable_tasks]
            specs = (
                list(
                    await session.scalars(
                        select(TaskSpecRevisionRow).where(TaskSpecRevisionRow.task_id.in_(task_ids))
                    )
                )
                if task_ids
                else []
            )
            refs = (
                list(
                    await session.scalars(
                        select(ArtifactRefRow).where(
                            ArtifactRefRow.task_id.in_(task_ids),
                            ArtifactRefRow.status == "AVAILABLE",
                        )
                    )
                )
                if task_ids
                else []
            )
            spec_by_revision = {(item.task_id, item.revision): item for item in specs}
            refs_by_task: dict[UUID, list[ArtifactRefRow]] = {}
            for ref in refs:
                if ref.task_id is not None:
                    refs_by_task.setdefault(ref.task_id, []).append(ref)
            public_messages: list[dict[str, Any]] = [
                {
                    "message_id": item.message_id,
                    "task_id": item.task_id,
                    "role": item.role,
                    "content": item.content,
                    "created_at": item.created_at,
                    "artifact_refs": [],
                }
                for item in messages
            ]
            type_names = {
                "CandidateOntology": "候选本体",
                "PublishedOntology": "已发布本体",
                "EntityDataset": "实体数据集",
                "RelationDataset": "关系数据集",
                "PropertyDataset": "属性数据集",
                "CandidateGraph": "候选图谱",
                "PublishedGraph": "已发布图谱",
                "TextReport": "文字报告",
            }
            for task in deliverable_tasks:
                spec = spec_by_revision.get((task.task_id, task.current_task_spec_revision))
                target_types = (
                    spec.payload.get("target_output_types", []) if spec is not None else []
                )
                # 候选本体/图谱作为最终目标时无需为了查看结果强迫用户先批准发布；
                # 其他尚未完成的任务不能提前声明成功。
                candidate_only = bool(target_types) and set(target_types) <= {
                    "CandidateOntology",
                    "FusedCandidateOntology",
                    "CandidateGraph",
                }
                if task.status != "SUCCEEDED" and not candidate_only:
                    continue
                selected: dict[str, ArtifactRefRow] = {}
                for ref in sorted(
                    refs_by_task.get(task.task_id, []),
                    key=lambda item: (item.created_at, str(item.artifact_id)),
                ):
                    if ref.artifact_type in target_types:
                        selected[ref.artifact_type] = ref
                final_refs = [
                    {
                        "artifact_id": ref.artifact_id,
                        "artifact_type": ref.artifact_type,
                        "content_summary": ref.payload.get("content_summary", ""),
                    }
                    for ref in selected.values()
                ]
                if task.status != "SUCCEEDED" and set(selected) != set(target_types):
                    continue
                summaries = [
                    f"{type_names.get(ref['artifact_type'], ref['artifact_type'])}："
                    f"{ref['content_summary'] or '已生成'}"
                    for ref in final_refs
                ]
                finished = task.status == "SUCCEEDED"
                public_messages.append(
                    {
                        "message_id": uuid5(NAMESPACE_URL, f"kg-agent:task-result:{task.task_id}"),
                        "task_id": task.task_id,
                        "role": "ASSISTANT",
                        "content": (
                            f"任务「{task.title}」已完成。"
                            if finished
                            else f"任务「{task.title}」的候选结果已生成。"
                        )
                        + ("产物：" + "；".join(summaries) if summaries else "目标已完成。"),
                        "created_at": (task.completed_at or task.updated_at)
                        if finished
                        else max(ref.created_at for ref in selected.values()),
                        "artifact_refs": final_refs,
                    }
                )
            public_messages.sort(key=lambda item: (item["created_at"], str(item["message_id"])))
            return {
                "conversation": {
                    "conversation_id": conversation.conversation_id,
                    "title": conversation.title,
                    "created_at": conversation.created_at,
                    "updated_at": conversation.updated_at,
                },
                "messages": public_messages,
                "last_event_sequence": int(last_event_sequence or 0),
                "tasks": [
                    {
                        "task_id": item.task_id,
                        "title": item.title,
                        "status": item.status,
                        "updated_at": item.updated_at,
                    }
                    for item in tasks
                ],
            }

    async def rename(self, conversation_id: UUID, title: str) -> dict[str, Any]:
        """修改会话侧栏标题，不影响历史消息或任一任务的业务标题。"""

        value = title.strip()
        if not value:
            raise ValueError("会话名称不能为空")
        async with self._factory() as session, session.begin():
            row = await session.get(ConversationRow, conversation_id, with_for_update=True)
            if row is None:
                raise LookupError("会话不存在")
            row.title, row.updated_at = value[:120], datetime.now(UTC)
        return {"conversation_id": conversation_id, "title": value[:120]}

    async def events_after(self, conversation_id: UUID, *, after: int = 0) -> list[dict[str, Any]]:
        """读取会话内全部任务的新增公开事件，作为会话页 SSE 的唯一事实来源。

        ``TaskEventRow.sequence`` 在数据库中全局递增，因此一个会话级游标即可覆盖
        该会话包含的多个 MasterTask。这里不拼接模型原始输出，也不返回其他会话事件。
        """

        async with self._factory() as session:
            rows = await session.scalars(
                select(TaskEventRow)
                .join(MasterTaskRow, MasterTaskRow.task_id == TaskEventRow.task_id)
                .where(
                    MasterTaskRow.conversation_id == conversation_id,
                    TaskEventRow.sequence > max(after, 0),
                )
                .order_by(TaskEventRow.sequence)
            )
            return [
                {
                    "sequence": item.sequence,
                    "event_id": item.event_id,
                    "task_id": item.task_id,
                    "subtask_id": item.subtask_id,
                    "assignment_id": item.assignment_id,
                    "event_type": item.event_type,
                    "actor_type": item.actor_type,
                    "actor_id": item.actor_id,
                    "reason_code": item.reason_code,
                    "payload": item.payload,
                    "occurred_at": item.occurred_at,
                }
                for item in rows
            ]

    async def delete(
        self, conversation_id: UUID, *, actor_user_id: str
    ) -> ConversationDeletionResult:
        """彻底清理会话私有数据，并保留全局正式发布资产。

        数据库删除先于对象与日志文件清理：若后者发生短暂故障，响应会返回告警而不是
        把失败伪装成成功。对象键不可复用，因此残留对象不会重新暴露为业务 Artifact。
        """

        task_ids: list[UUID] = []
        ordinary_object_keys: list[str] = []
        created_memory_ids: list[str] = []
        unresolved_memory_count = 0
        retained_published_count = 0
        deleted_artifact_count = 0

        async with self._factory() as session, session.begin():
            conversation = await session.get(ConversationRow, conversation_id, with_for_update=True)
            if conversation is None:
                raise LookupError("会话不存在")
            tasks = list(
                await session.scalars(
                    select(MasterTaskRow)
                    .where(MasterTaskRow.conversation_id == conversation_id)
                    .with_for_update()
                )
            )
            if any(item.user_id != actor_user_id for item in tasks):
                # V1 没有登录页，但 API 仍必须在服务端固定单用户边界，避免一个
                # 猜中的 conversation_id 越过未来身份接入时的所有权限制。
                raise PermissionError("当前用户无权删除该会话")
            task_ids = [item.task_id for item in tasks]
            if task_ids:
                artifacts = list(
                    await session.execute(
                        select(ArtifactRefRow, ArtifactContentRow)
                        .join(
                            ArtifactContentRow,
                            ArtifactContentRow.artifact_id == ArtifactRefRow.artifact_id,
                        )
                        .where(ArtifactRefRow.task_id.in_(task_ids))
                    )
                )
                retained_ids = {
                    ref.artifact_id
                    for ref, _content in artifacts
                    if ref.artifact_type in {"PublishedOntology", "PublishedGraph"}
                    and ref.payload.get("producer_skill") != "published_asset_import"
                }
                deleted_ids = {
                    ref.artifact_id
                    for ref, _content in artifacts
                    if ref.artifact_id not in retained_ids
                }
                retained_published_count = len(retained_ids)
                deleted_artifact_count = len(deleted_ids)
                ordinary_object_keys = [
                    object_key
                    for ref, content in artifacts
                    if ref.artifact_id in deleted_ids
                    for object_key in (content.object_key,)
                ]

                # 发布图谱需要继续保留节点—边投影；候选与 Approval 属于私有执行证据，
                # 所以删除会话后清空这两个来源外键和来源任务外键。
                if retained_ids:
                    await session.execute(
                        update(PublishedGraphRow)
                        .where(PublishedGraphRow.artifact_id.in_(retained_ids))
                        .values(
                            task_id=None,
                            source_candidate_artifact_id=None,
                            approval_artifact_id=None,
                        )
                    )
                    await session.execute(
                        update(ArtifactRefRow)
                        .where(ArtifactRefRow.artifact_id.in_(retained_ids))
                        .values(task_id=None)
                    )

                write_rows = list(
                    await session.scalars(
                        select(ApiRequestRow).where(
                            ApiRequestRow.task_id.in_(task_ids),
                            ApiRequestRow.operation == "COMPLETED_TASK_MEMORY",
                            ApiRequestRow.status == "COMPLETED",
                        )
                    )
                )
                for row in write_rows:
                    payload = row.response_payload or {}
                    receipt = payload.get("receipt") if isinstance(payload, dict) else None
                    if not isinstance(payload, dict) or payload.get("action") != "CREATED":
                        continue
                    memory_id = receipt.get("event_id") if isinstance(receipt, dict) else None
                    if (
                        isinstance(memory_id, str)
                        and isinstance(receipt, dict)
                        and receipt.get("status") == "SUCCEEDED"
                    ):
                        try:
                            UUID(memory_id)
                        except ValueError:
                            unresolved_memory_count += 1
                        else:
                            created_memory_ids.append(memory_id)
                    else:
                        unresolved_memory_count += 1

                # 任一任务若已合并或复用本会话原先创建的记忆，原记录现在承载共享
                # 事实。只按真正的 provider memory ID 比对，绝不把写入事件号猜成 ID。
                consolidation_writes = list(
                    await session.scalars(
                        select(ApiRequestRow).where(
                            ApiRequestRow.operation == "COMPLETED_TASK_MEMORY",
                            ApiRequestRow.status == "COMPLETED",
                        )
                    )
                )
                shared_memory_ids = {
                    row.response_payload["matched_provider_memory_id"]
                    for row in consolidation_writes
                    if isinstance(row.response_payload, dict)
                    and row.response_payload.get("action") in {"MERGED", "SKIPPED_DUPLICATE"}
                    and isinstance(row.response_payload.get("matched_provider_memory_id"), str)
                }
                created_memory_ids = [
                    memory_id
                    for memory_id in created_memory_ids
                    if memory_id not in shared_memory_ids
                ]

                subtask_ids = list(
                    await session.scalars(
                        select(AgentSubTaskRow.subtask_id).where(
                            AgentSubTaskRow.master_task_id.in_(task_ids)
                        )
                    )
                )
                assignment_ids = (
                    list(
                        await session.scalars(
                            select(AgentAssignmentRow.assignment_id).where(
                                AgentAssignmentRow.subtask_id.in_(subtask_ids)
                            )
                        )
                    )
                    if subtask_ids
                    else []
                )

                # 按外键依赖逆序清除会话私有行。发布资产已被移出 deleted_ids，
                # 因此不会被该批 DELETE 级联移除。
                if assignment_ids:
                    await session.execute(
                        delete(ExecutionTraceRow).where(
                            ExecutionTraceRow.assignment_id.in_(assignment_ids)
                        )
                    )
                    await session.execute(
                        delete(AgentRunResultRow).where(
                            AgentRunResultRow.assignment_id.in_(assignment_ids)
                        )
                    )
                await session.execute(
                    delete(ExecutionTraceRow).where(ExecutionTraceRow.task_id.in_(task_ids))
                )
                await session.execute(
                    delete(HumanReviewRequestRow).where(HumanReviewRequestRow.task_id.in_(task_ids))
                )
                if subtask_ids:
                    await session.execute(
                        delete(SubTaskDependencyRow).where(
                            or_(
                                SubTaskDependencyRow.subtask_id.in_(subtask_ids),
                                SubTaskDependencyRow.dependency_subtask_id.in_(subtask_ids),
                            )
                        )
                    )
                    await session.execute(
                        delete(AgentAssignmentRow).where(
                            AgentAssignmentRow.subtask_id.in_(subtask_ids)
                        )
                    )
                    await session.execute(
                        delete(AgentSubTaskRow).where(AgentSubTaskRow.subtask_id.in_(subtask_ids))
                    )
                if deleted_ids:
                    await session.execute(
                        delete(ArtifactLineageRow).where(
                            or_(
                                ArtifactLineageRow.child_artifact_id.in_(deleted_ids),
                                ArtifactLineageRow.parent_artifact_id.in_(deleted_ids),
                            )
                        )
                    )
                    await session.execute(
                        delete(ArtifactContentRow).where(
                            ArtifactContentRow.artifact_id.in_(deleted_ids)
                        )
                    )
                    await session.execute(
                        delete(ArtifactRefRow).where(ArtifactRefRow.artifact_id.in_(deleted_ids))
                    )
                await session.execute(
                    delete(SharedContextSnapshotRow).where(
                        SharedContextSnapshotRow.task_id.in_(task_ids)
                    )
                )
                await session.execute(
                    delete(TaskEventRow).where(TaskEventRow.task_id.in_(task_ids))
                )
                await session.execute(
                    delete(TaskSpecRevisionRow).where(TaskSpecRevisionRow.task_id.in_(task_ids))
                )
                await session.execute(
                    delete(DecompositionRevisionRow).where(
                        DecompositionRevisionRow.task_id.in_(task_ids)
                    )
                )
                await session.execute(
                    delete(ApiRequestRow).where(ApiRequestRow.task_id.in_(task_ids))
                )
                await session.execute(
                    delete(ConversationMessageRow).where(
                        ConversationMessageRow.task_id.in_(task_ids)
                    )
                )
                await session.execute(
                    delete(MasterTaskRow).where(MasterTaskRow.task_id.in_(task_ids))
                )
            await session.execute(
                delete(ApiRequestRow).where(ApiRequestRow.conversation_id == conversation_id)
            )
            await session.execute(
                delete(ConversationMessageRow).where(
                    ConversationMessageRow.conversation_id == conversation_id
                )
            )
            await session.execute(
                delete(ConversationRow).where(ConversationRow.conversation_id == conversation_id)
            )

        warnings: list[str] = []
        if unresolved_memory_count:
            warnings.append(
                f"{unresolved_memory_count} 条旧经验缺少可确认的 Mem0 记忆 ID，已保留待人工核查。"
            )
        deleted_memory_count = 0
        # 只删除本任务独立创建的经验；MERGED 的事实已经属于另一条共享记忆，按用户
        # 规则保留，不让删除一个会话破坏其他会话可用经验。
        for memory_id in created_memory_ids:
            try:
                receipt = await self._memory.delete_provider_memory(memory_id)
                if receipt.status == "DELETED":
                    deleted_memory_count += 1
            except Exception:
                warnings.append("部分未合并长期记忆暂未删除，可稍后重试清理。")
                break
        for object_key in ordinary_object_keys:
            try:
                await self._storage.delete(object_key)
            except Exception:
                warnings.append("部分对象存储文件暂未删除，不再被平台引用。")
                break
        for task_id in task_ids:
            try:
                await self._run_logs.delete_task_files(task_id)
            except Exception:
                warnings.append("部分任务日志暂未删除，不再可从工作台访问。")
                break
        return ConversationDeletionResult(
            conversation_id=conversation_id,
            deleted_task_count=len(task_ids),
            deleted_artifact_count=deleted_artifact_count,
            retained_published_asset_count=retained_published_count,
            deleted_memory_count=deleted_memory_count,
            cleanup_warnings=tuple(dict.fromkeys(warnings)),
        )
