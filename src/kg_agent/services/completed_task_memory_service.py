"""在 MasterTask 成功收口后构建并幂等写入一条完整长期经验。

本服务只聚合 PostgreSQL 中已经确认的任务、子任务、AgentRunResult 和 Artifact 事实，
不调用新的 Agent，也不保存文档正文。Mem0 负责语义检索与正文存储；写入幂等状态复用
现有 ``api_requests`` 表，避免协调器重试产生重复经验。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.enums import AgentRunStatus, ArtifactStatus, MasterTaskStatus
from kg_agent.domain.memory_models import (
    CompletedTaskExperience,
    MemoryConsolidationResult,
    MemoryWriteReceipt,
)
from kg_agent.exceptions import MemoryProviderError
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.models import ApiRequestRow, ConversationMessageRow
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.services.run_log_service import RunLogService

LOGGER = logging.getLogger(__name__)

_CONSENT_OPERATION = "TASK_MEMORY_CONSENT"
_WRITE_OPERATION = "COMPLETED_TASK_MEMORY"


class CompletedTaskMemoryService:
    """聚合一轮完整成功经验，并协调授权、幂等、Mem0 写入和公开日志。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        memory: LongTermMemoryService,
        run_logs: RunLogService,
        auto_authorized: bool = False,
    ) -> None:
        """绑定数据库、长期记忆服务与日志出口。

        Args:
            session_factory: 创建短事务数据库会话的工厂。
            memory: 当前环境隔离后的长期记忆服务。
            run_logs: 任务公开 JSONL 日志服务。
            auto_authorized: 验证实验室是否自动授权写入；正式端必须使用持久化授权。
        """

        self._factory = session_factory
        self._memory = memory
        self._run_logs = run_logs
        self._auto_authorized = auto_authorized

    async def remember(self, task_id: UUID) -> MemoryConsolidationResult | None:
        """当任务已成功且获得授权时，幂等写入一条聚合经验。

        Mem0 网络或响应错误只会把幂等记录标为失败并写公开日志，不会回滚已经成功的
        MasterTask。后续协调器再次触发时可以使用相同 ``experience_key`` 安全重试。
        """

        experience = await self._build_experience(task_id)
        if experience is None:
            return None
        if not self._auto_authorized and not await self._is_authorized(task_id):
            await self._run_logs.append(
                task_id=task_id,
                record_type="COMPLETED_EXPERIENCE_SKIPPED",
                actor="memory_service",
                payload={
                    "experience_key": experience.experience_key,
                    "reason": "LONG_TERM_MEMORY_NOT_AUTHORIZED",
                },
            )
            return None

        request_id = uuid5(
            NAMESPACE_URL,
            f"kg-agent:{_WRITE_OPERATION}:{experience.experience_key}",
        )
        cached = await self._claim_write(request_id, experience)
        if cached is not None:
            return cached

        await self._run_logs.append(
            task_id=task_id,
            record_type="COMPLETED_EXPERIENCE_WRITE_STARTED",
            actor="memory_service",
            payload={
                "experience_key": experience.experience_key,
                "task_spec_revision": experience.task_spec_revision,
                "decomposition_revision": experience.decomposition_revision,
            },
        )
        try:
            result = await self._memory.remember_completed_task_experience(experience)
        except MemoryProviderError as exc:
            await self._finish_write(request_id, error=type(exc).__name__)
            await self._run_logs.append(
                task_id=task_id,
                record_type="COMPLETED_EXPERIENCE_WRITE_FAILED",
                actor="memory_service",
                payload={
                    "experience_key": experience.experience_key,
                    "error_type": type(exc).__name__,
                },
            )
            LOGGER.warning("任务 %s 的完整长期经验写入失败", task_id, exc_info=True)
            return None

        await self._finish_write(request_id, result=result)
        await self._run_logs.append(
            task_id=task_id,
            record_type="COMPLETED_EXPERIENCE_WRITE",
            actor="memory_service",
            payload={
                "experience_key": experience.experience_key,
                "action": result.action,
                "similarity": result.similarity,
                "matched_provider_memory_id": result.matched_provider_memory_id,
                "provider_status": result.receipt.status,
            },
        )
        return result

    async def _build_experience(self, task_id: UUID) -> CompletedTaskExperience | None:
        """从当前成功修订的结构化事实生成公开、紧凑的整轮经验摘要。"""

        async with self._factory() as session:
            tasks = SqlAlchemyTaskRepository(session)
            master = await tasks.get_master(task_id)
            context = await SqlAlchemyContextRepository(session).get(task_id)
            if master is None or context is None or master.status is not MasterTaskStatus.SUCCEEDED:
                return None
            # 最终验收会发布一个“空拆解 revision”表示没有剩余工作，因此不能只读取
            # current_decomposition_revision。完整经验应汇总该 MasterTask 历次修订中
            # 实际成功的运行；失败、取消或仅规划未执行的子任务不会被收入。
            subtasks = await tasks.list_subtasks(task_id)
            subtask_ids = {item.subtask_id for item in subtasks}
            runs = [
                item
                for item in await SqlAlchemyAgentRunRepository(session).list_results(task_id)
                if item.subtask_id in subtask_ids and item.status is AgentRunStatus.SUCCESS
            ]
            artifacts = [
                item
                for item in await SqlAlchemyArtifactRepository(session).list_for_task(task_id)
                if item.ref.status is ArtifactStatus.AVAILABLE
            ]
            latest_user_message = await session.scalar(
                select(ConversationMessageRow)
                .where(
                    ConversationMessageRow.task_id == task_id,
                    ConversationMessageRow.role == "USER",
                )
                .order_by(ConversationMessageRow.created_at.desc())
                .limit(1)
            )

        task_spec = context.current_task_spec
        experience_key = (
            f"task:{task_id}:task-spec:{task_spec.revision}:"
            f"decomposition:{master.current_decomposition_revision}"
        )
        run_by_subtask = {item.subtask_id: item for item in runs}
        path = list(
            dict.fromkeys(
                f"{item.name}（{item.assigned_agent_type}）"
                for item in subtasks
                if item.subtask_id in run_by_subtask
            )
        )
        skills = list(
            dict.fromkeys(skill for run in runs for skill in run.skills_used if skill.strip())
        )
        target_types = set(task_spec.target_output_types)
        final_artifacts = [
            item.ref
            for item in artifacts
            if not target_types or item.ref.artifact_type in target_types
        ]
        lessons = list(
            dict.fromkeys(
                memory.summary.strip()
                for run in runs
                for memory in run.memory_entries
                if memory.memory_type == "EXPERIENCE" and memory.summary.strip()
            )
        )
        lines = [
            "完整任务成功经验",
            f"业务目标：{task_spec.business_goal}",
            f"领域：{task_spec.domain}",
            f"任务意图：{task_spec.task_intent.value}",
            f"执行路径：{' → '.join(path) if path else '由当前成功修订直接满足'}",
            f"使用技能：{'、'.join(skills) if skills else '无'}",
            (
                "最终产物："
                + (
                    "、".join(
                        f"{item.artifact_type}({item.artifact_id})" for item in final_artifacts
                    )
                    if final_artifacts
                    else "目标由已有可复用产物满足"
                )
            ),
        ]
        if lessons:
            lines.append(f"可复用经验：{'；'.join(lessons)}")
        source_refs = [
            *(f"agent-run://{item.run_id}" for item in runs),
            *(f"artifact://{item.artifact_id}" for item in final_artifacts),
        ]
        if latest_user_message is not None:
            source_refs.append(f"conversation://message/{latest_user_message.message_id}")
        return CompletedTaskExperience(
            experience_key=experience_key,
            task_id=task_id,
            task_spec_revision=task_spec.revision,
            decomposition_revision=master.current_decomposition_revision,
            summary="\n".join(lines),
            source_refs=source_refs,
        )

    async def _is_authorized(self, task_id: UUID) -> bool:
        """读取正式端当前任务最近一次长期记忆授权选择。"""

        async with self._factory() as session:
            row = await session.scalar(
                select(ApiRequestRow)
                .where(
                    ApiRequestRow.task_id == task_id,
                    ApiRequestRow.operation == _CONSENT_OPERATION,
                    ApiRequestRow.status == "COMPLETED",
                )
                .order_by(ApiRequestRow.updated_at.desc())
                .limit(1)
            )
        return bool(row and row.response_payload and row.response_payload.get("authorized"))

    async def _claim_write(
        self,
        request_id: UUID,
        experience: CompletedTaskExperience,
    ) -> MemoryConsolidationResult | None:
        """以确定性请求 ID 认领一次写入；已完成记录直接返回缓存结果。"""

        now = datetime.now(UTC)
        digest = hashlib.sha256(experience.summary.encode("utf-8")).hexdigest()
        async with self._factory() as session, session.begin():
            row = await session.get(ApiRequestRow, request_id, with_for_update=True)
            if row is not None and row.status == "COMPLETED" and row.response_payload:
                return MemoryConsolidationResult.model_validate(row.response_payload)
            if row is not None and row.status == "IN_PROGRESS":
                return MemoryConsolidationResult(
                    action="SKIPPED_DUPLICATE",
                    receipt=MemoryWriteReceipt(
                        status="IN_PROGRESS",
                        message="同一完整任务经验正在写入",
                    ),
                )
            if row is None:
                session.add(
                    ApiRequestRow(
                        request_id=request_id,
                        request_hash=digest,
                        operation=_WRITE_OPERATION,
                        status="IN_PROGRESS",
                        task_id=experience.task_id,
                        conversation_id=None,
                        response_payload=None,
                        error_message=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.request_hash = digest
                row.status = "IN_PROGRESS"
                row.response_payload = None
                row.error_message = None
                row.updated_at = now
        return None

    async def _finish_write(
        self,
        request_id: UUID,
        *,
        result: MemoryConsolidationResult | None = None,
        error: str | None = None,
    ) -> None:
        """持久化 Mem0 写入终态，使进程重启后仍可安全判断是否需要重试。"""

        async with self._factory() as session, session.begin():
            row = await session.get(ApiRequestRow, request_id, with_for_update=True)
            if row is None:
                return
            row.status = "COMPLETED" if result is not None else "FAILED"
            row.response_payload = result.model_dump(mode="json") if result is not None else None
            row.error_message = error
            row.updated_at = datetime.now(UTC)


async def record_task_memory_consent(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request_id: UUID,
    task_id: UUID,
    conversation_id: UUID | None,
    authorized: bool,
) -> None:
    """持久化正式端本轮长期记忆授权，供异步任务成功收口时读取。"""

    consent_id = uuid5(NAMESPACE_URL, f"kg-agent:{_CONSENT_OPERATION}:{request_id}")
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        row = await session.get(ApiRequestRow, consent_id, with_for_update=True)
        payload = {"authorized": authorized, "source_request_id": str(request_id)}
        if row is None:
            session.add(
                ApiRequestRow(
                    request_id=consent_id,
                    request_hash=hashlib.sha256(str(authorized).encode()).hexdigest(),
                    operation=_CONSENT_OPERATION,
                    status="COMPLETED",
                    task_id=task_id,
                    conversation_id=conversation_id,
                    response_payload=payload,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.task_id = task_id
            row.conversation_id = conversation_id
            row.status = "COMPLETED"
            row.response_payload = payload
            row.error_message = None
            row.updated_at = now
