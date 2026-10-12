"""提供正式的用户任务接入、总控路由、多轮消息和幂等保护。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from kg_agent.agents.controller.state import ControllerRunResult
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import TaskEventType
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.exceptions import PersistenceError
from kg_agent.infrastructure.database.models import (
    ApiRequestRow,
    ConversationMessageRow,
    ConversationRow,
)
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
)
from kg_agent.services.completed_task_memory_service import record_task_memory_consent
from kg_agent.services.demo_service import DemoOrchestrationService, DemoTaskSnapshot, UploadedFile
from kg_agent.services.postgres_context_service import PostgresContextService


class TaskOrchestrationService(DemoOrchestrationService):
    """复用已验证的原子文件接入逻辑，并增加生产级多轮与幂等语义。"""

    async def submit_and_run(
        self,
        *,
        question: str,
        files: list[UploadedFile],
        task_id: UUID | None = None,
        conversation_id: UUID | None = None,
        request_id: UUID | None = None,
        execute_workers: bool = False,
        max_worker_runs: int = 0,
        remember_long_term: bool = False,
        internal_continuation: bool = False,
        **kwargs: Any,
    ) -> DemoTaskSnapshot:
        """幂等提交一轮用户消息，或执行不写用户会话的最终目标检查。

        正式 API 不在请求进程内执行 Worker。``internal_continuation`` 只能由
        TaskCoordinator 使用，其内容是系统推进命令，不能伪装成用户消息或长期记忆。
        """

        if internal_continuation:
            resolved_conversation_id = await self._conversation_for_task(task_id, conversation_id)
            history = await self._history(resolved_conversation_id)
            return await super().submit_and_run(
                question=question,
                files=files,
                task_id=task_id,
                conversation_id=resolved_conversation_id,
                request_id=request_id,
                execute_workers=False,
                max_worker_runs=0,
                conversation_history=self._history_payload(history),
                internal_continuation=True,
                **kwargs,
            )

        resolved_request_id = request_id or uuid4()
        digest = self._request_hash(
            question,
            files,
            task_id,
            conversation_id,
            remember_long_term=remember_long_term,
        )
        cached = await self._find_request(resolved_request_id)
        # 新任务在发布 Assignment 前先固定 task_id，确保长期记忆授权能够先于后台
        # Worker 持久化。否则极快任务可能已经成功收口，协调器却尚未看见授权记录。
        resolved_task_id = task_id or (
            cached.task_id if cached is not None and cached.task_id is not None else uuid4()
        )
        resolved_conversation_id = await self._conversation_for_task(
            resolved_task_id,
            conversation_id or (cached.conversation_id if cached is not None else None),
        )
        await self._ensure_conversation(resolved_conversation_id, question)
        if cached is not None:
            if cached.request_hash != digest:
                raise PersistenceError("同一 request_id 不能提交不同内容")
            if cached.status == "COMPLETED" and cached.response_payload:
                result = ControllerRunResult.model_validate(cached.response_payload)
                assert cached.task_id is not None and cached.conversation_id is not None
                snapshot = await self._snapshot(cached.task_id, cached.conversation_id, result)
                if kwargs.get("publish_decomposition") is False:
                    # 候选计划尚未正式落库；幂等重放仍必须把已缓存候选子任务返回给
                    # 审核窗口，而不是只返回 Repository 中的空列表或上一 revision。
                    snapshot = DemoTaskSnapshot(
                        task_id=snapshot.task_id,
                        conversation_id=snapshot.conversation_id,
                        controller_result=snapshot.controller_result,
                        subtasks=result.decomposition.subtasks,
                        assignments=snapshot.assignments,
                        runs=snapshot.runs,
                        artifacts=snapshot.artifacts,
                        master_task_status=snapshot.master_task_status,
                        recalled_memories=snapshot.recalled_memories,
                        memory_write_receipt=snapshot.memory_write_receipt,
                        memory_enabled=snapshot.memory_enabled,
                        memory_provider=snapshot.memory_provider,
                        memory_app_id=snapshot.memory_app_id,
                        memory_recall_status=snapshot.memory_recall_status,
                        coordination_actions=snapshot.coordination_actions,
                    )
                return snapshot
            if cached.status == "IN_PROGRESS":
                raise PersistenceError("相同请求正在处理中")
        await self._mark_started(
            resolved_request_id,
            digest,
            resolved_task_id,
            resolved_conversation_id,
        )
        await record_task_memory_consent(
            self._database.session_factory,
            request_id=resolved_request_id,
            task_id=resolved_task_id,
            conversation_id=resolved_conversation_id,
            authorized=remember_long_term,
        )
        history = await self._history(resolved_conversation_id)
        try:
            snapshot = await super().submit_and_run(
                question=question,
                files=files,
                task_id=resolved_task_id,
                conversation_id=resolved_conversation_id,
                request_id=resolved_request_id,
                execute_workers=execute_workers,
                max_worker_runs=max_worker_runs,
                conversation_history=self._history_payload(history),
                internal_continuation=False,
                **kwargs,
            )
            await self._mark_completed(resolved_request_id, snapshot, question)
            if snapshot.controller_result.shared_context is not None:
                # 正式端只记录用户本轮授权选择；真正正文必须等整个 MasterTask 成功
                # 后由协调器聚合写入，不能在计划刚发布时保存“未完成经验”。
                await record_task_memory_consent(
                    self._database.session_factory,
                    request_id=resolved_request_id,
                    task_id=snapshot.task_id,
                    conversation_id=snapshot.conversation_id,
                    authorized=remember_long_term,
                )
                if (
                    snapshot.master_task_status is not None
                    and snapshot.master_task_status.value == "SUCCEEDED"
                ):
                    await self.remember_completed_task_experience(snapshot.task_id)
            return snapshot
        except Exception as exc:
            await self._mark_failed(resolved_request_id, exc)
            raise

    async def _find_request(self, request_id: UUID) -> ApiRequestRow | None:
        """读取幂等记录。"""
        async with self._database.session_factory() as session:
            return await session.get(ApiRequestRow, request_id)

    async def _mark_started(
        self, request_id: UUID, digest: str, task_id: UUID | None, conversation_id: UUID | None
    ) -> None:
        """在模型调用前记录 IN_PROGRESS，阻止并发重复执行。"""
        now = datetime.now(UTC)
        async with self._database.session_factory() as session, session.begin():
            row = await session.get(ApiRequestRow, request_id, with_for_update=True)
            if row is None:
                session.add(
                    ApiRequestRow(
                        request_id=request_id,
                        request_hash=digest,
                        operation="TASK_MESSAGE",
                        status="IN_PROGRESS",
                        task_id=task_id,
                        conversation_id=conversation_id,
                        response_payload=None,
                        error_message=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.status, row.error_message, row.updated_at = "IN_PROGRESS", None, now

    async def _mark_completed(
        self, request_id: UUID, snapshot: DemoTaskSnapshot, original_message: str
    ) -> None:
        """保存可重建总控结果和一轮完整的用户—总控公开对话。"""
        now = datetime.now(UTC)
        async with self._database.session_factory() as session, session.begin():
            row = await session.get(ApiRequestRow, request_id, with_for_update=True)
            if row is None:
                raise PersistenceError("幂等请求记录丢失")
            row.status, row.task_id, row.conversation_id = (
                "COMPLETED",
                snapshot.task_id,
                snapshot.conversation_id,
            )
            row.response_payload = snapshot.controller_result.model_dump(mode="json")
            row.updated_at = now
            # 请求澄清时 MasterTask 尚未创建，不能写入带外键的消息表。
            if snapshot.controller_result.shared_context is not None:
                contexts = SqlAlchemyContextRepository(session)
                current = await contexts.get(snapshot.task_id)
                if current is not None:
                    assistant_message = self._assistant_turn_summary(
                        snapshot.controller_result.decomposition,
                        plan_published=True,
                    )
                    updated, user_message_id, assistant_message_id = self._conversation_round(
                        current=current,
                        conversation_id=snapshot.conversation_id,
                        user_message=original_message,
                        assistant_message=assistant_message,
                        created_at=now,
                    )
                    session.add_all(
                        [
                            ConversationMessageRow(
                                message_id=user_message_id,
                                conversation_id=snapshot.conversation_id,
                                task_id=snapshot.task_id,
                                role="USER",
                                content=original_message,
                                created_at=now,
                            ),
                            ConversationMessageRow(
                                message_id=assistant_message_id,
                                conversation_id=snapshot.conversation_id,
                                task_id=snapshot.task_id,
                                role="ASSISTANT",
                                content=assistant_message,
                                created_at=now + timedelta(microseconds=1),
                            ),
                        ]
                    )
                    await contexts.add_snapshot(updated)
                    await SqlAlchemyEventRepository(session).append(
                        task_id=snapshot.task_id,
                        event_type=TaskEventType.CONTEXT_VERSION_CREATED,
                        actor_type="USER",
                        actor_id="api_user",
                        occurred_at=now,
                        payload={
                            "version": updated.version,
                            "user_message_id": str(user_message_id),
                            "assistant_message_id": str(assistant_message_id),
                        },
                    )
            else:
                # 澄清轮次尚未创建 MasterTask，仍属于可刷新、可续接的用户会话。
                assistant_message = self._assistant_turn_summary(
                    snapshot.controller_result.decomposition,
                    plan_published=False,
                )
                session.add_all(
                    [
                        ConversationMessageRow(
                            message_id=uuid4(),
                            conversation_id=snapshot.conversation_id,
                            task_id=None,
                            role="USER",
                            content=original_message,
                            created_at=now,
                        ),
                        ConversationMessageRow(
                            message_id=uuid4(),
                            conversation_id=snapshot.conversation_id,
                            task_id=None,
                            role="ASSISTANT",
                            content=assistant_message,
                            created_at=now + timedelta(microseconds=1),
                        ),
                    ]
                )

    async def _mark_failed(self, request_id: UUID, exc: Exception) -> None:
        """记录失败类型但不保存堆栈或密钥；同一幂等键之后允许重试。"""
        async with self._database.session_factory() as session, session.begin():
            row = await session.get(ApiRequestRow, request_id, with_for_update=True)
            if row:
                row.status, row.error_message, row.updated_at = (
                    "FAILED",
                    type(exc).__name__,
                    datetime.now(UTC),
                )

    async def record_approved_plan_message(
        self,
        *,
        task_id: UUID,
        conversation_id: UUID,
        message: str,
        decomposition: TaskDecompositionResult | None = None,
    ) -> None:
        """候选计划获批后补写用户消息、总控公开回复和 SharedContext 轻量引用。"""

        now = datetime.now(UTC)
        async with self._database.session_factory() as session, session.begin():
            contexts = SqlAlchemyContextRepository(session)
            current = await contexts.get(task_id)
            if current is None:
                raise PersistenceError("计划发布后 SharedContext 不存在")
            assistant_message = (
                self._assistant_turn_summary(decomposition, plan_published=True)
                if decomposition is not None
                else "候选任务计划已经人工审核并发布，专业 Agent 可以开始认领子任务。"
            )
            updated, user_message_id, assistant_message_id = self._conversation_round(
                current=current,
                conversation_id=conversation_id,
                user_message=message,
                assistant_message=assistant_message,
                created_at=now,
            )
            session.add_all(
                [
                    ConversationMessageRow(
                        message_id=user_message_id,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        role="USER",
                        content=message,
                        created_at=now,
                    ),
                    ConversationMessageRow(
                        message_id=assistant_message_id,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        role="ASSISTANT",
                        content=assistant_message,
                        created_at=now + timedelta(microseconds=1),
                    ),
                ]
            )
            await contexts.add_snapshot(updated)
            await SqlAlchemyEventRepository(session).append(
                task_id=task_id,
                event_type=TaskEventType.CONTEXT_VERSION_CREATED,
                actor_type="USER",
                actor_id="simulation_plan_review",
                occurred_at=now,
                payload={
                    "version": updated.version,
                    "user_message_id": str(user_message_id),
                    "assistant_message_id": str(assistant_message_id),
                },
            )

    async def _conversation_for_task(
        self, task_id: UUID | None, requested_conversation_id: UUID | None
    ) -> UUID:
        """解析当前任务已有会话，或为同一聊天中的新任务确定稳定会话 ID。"""

        if task_id is not None:
            context = await PostgresContextService(self._database.session_factory).get(task_id)
            if context is not None:
                if (
                    requested_conversation_id is not None
                    and requested_conversation_id != context.conversation.conversation_id
                ):
                    raise PersistenceError("conversation_id 与现有 MasterTask 不一致")
                return context.conversation.conversation_id
        return requested_conversation_id or uuid4()

    async def _ensure_conversation(self, conversation_id: UUID, initial_message: str) -> None:
        """创建或轻量更新用户可见会话，不能修改其关联任务事实。"""

        now = datetime.now(UTC)
        async with self._database.session_factory() as session, session.begin():
            row = await session.get(ConversationRow, conversation_id, with_for_update=True)
            if row is None:
                session.add(
                    ConversationRow(
                        conversation_id=conversation_id,
                        title=initial_message.strip()[:120] or "新会话",
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.updated_at = now

    async def _history(self, conversation_id: UUID | None) -> list[ConversationMessageRow]:
        """按时间读取同一会话最近五轮公开用户—总控消息。"""
        if conversation_id is None:
            return []
        async with self._database.session_factory() as session:
            rows = await session.scalars(
                select(ConversationMessageRow)
                .where(ConversationMessageRow.conversation_id == conversation_id)
                .order_by(ConversationMessageRow.created_at.desc())
                .limit(10)
            )
            return list(reversed(list(rows)))

    @staticmethod
    def _history_payload(rows: list[ConversationMessageRow]) -> list[dict[str, object]]:
        """把持久化角色标准化为模型可直接使用的最近五轮消息。"""

        return [{"role": item.role.lower(), "content": item.content} for item in rows[-10:]]

    @staticmethod
    def _assistant_turn_summary(
        decomposition: TaskDecompositionResult,
        *,
        plan_published: bool,
    ) -> str:
        """从已校验总控结果生成可公开、可持久化且不含隐藏推理的 Agent 回复。"""

        if decomposition.requires_clarification:
            return decomposition.clarification_question or "需要用户补充任务信息。"
        action = "已发布" if plan_published else "已生成"
        return (
            f"已识别任务意图为 {decomposition.task_intent.value}；"
            f"业务目标：{decomposition.task_spec.business_goal}；"
            f"{action} {len(decomposition.subtasks)} 个专业 Agent 子任务。"
        )

    @staticmethod
    def _conversation_round(
        *,
        current: SharedTaskContext,
        conversation_id: UUID,
        user_message: str,
        assistant_message: str,
        created_at: datetime,
    ) -> tuple[SharedTaskContext, UUID, UUID]:
        """向 SharedContext 投影一轮轻量对话，并只保留最近五轮对话引用。"""

        user_message_id = uuid4()
        assistant_message_id = uuid4()
        new_memories = [
            MemoryRef(
                memory_id=user_message_id,
                memory_type="SHORT_TERM",
                summary=user_message[:2000],
                source_refs=[
                    f"conversation://message/{user_message_id}",
                    "conversation-role:user",
                ],
            ),
            MemoryRef(
                memory_id=assistant_message_id,
                memory_type="SHORT_TERM",
                summary=assistant_message[:2000],
                source_refs=[
                    f"conversation://message/{assistant_message_id}",
                    "conversation-role:assistant",
                ],
            ),
        ]
        conversation_memories = [
            item
            for item in current.short_term_memories
            if any(ref.startswith("conversation-role:") for ref in item.source_refs)
        ]
        task_notes = [
            item
            for item in current.short_term_memories
            if not any(ref.startswith("conversation-role:") for ref in item.source_refs)
        ]
        conversation = current.conversation.model_copy(
            update={
                "conversation_id": conversation_id,
                "latest_message_id": assistant_message_id,
                "summary": assistant_message[:500],
            }
        )
        updated = current.model_copy(
            update={
                "version": current.version + 1,
                "conversation": conversation,
                "short_term_memories": [
                    *task_notes,
                    *([*conversation_memories, *new_memories][-10:]),
                ],
                "updated_at": created_at,
            }
        )
        return updated, user_message_id, assistant_message_id

    @staticmethod
    def _request_hash(
        question: str,
        files: list[UploadedFile],
        task_id: UUID | None,
        conversation_id: UUID | None,
        *,
        remember_long_term: bool = False,
    ) -> str:
        """对影响语义的请求字段和文件内容生成稳定摘要。"""
        payload = {
            "question": question,
            "task_id": str(task_id) if task_id else None,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "remember_long_term": remember_long_term,
            "files": [
                {
                    "filename": item.filename,
                    "media_type": item.media_type,
                    "artifact_type": item.artifact_type,
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                }
                for item in files
            ],
        }
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        )


IncomingFile = UploadedFile
