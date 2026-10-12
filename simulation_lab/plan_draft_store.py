"""持久化测试端尚未发布的总控候选计划和真实输入文件。

候选计划只写入隔离 MinIO 暂存区，并通过公开 JSONL 保存轻量索引；用户批准前不创建
MasterTask、AgentSubTask 或 AgentAssignment。批准后由应用服务原子发布，并标记草稿完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.services.demo_service import UploadedFile
from kg_agent.services.run_log_service import RunLogService


class StoredPlanInput(BaseModel):
    """描述一个保存在 MinIO 暂存区、尚未成为 Artifact 的真实输入。"""

    filename: str
    media_type: str
    artifact_type: str
    source_artifact_id: UUID | None = None
    object_key: str


class StoredSimulationPlan(BaseModel):
    """保存用户审核所需的完整候选计划及其发布上下文。"""

    task_id: UUID
    conversation_id: UUID
    request_id: UUID
    question: str
    decomposition: TaskDecompositionResult
    inputs: list[StoredPlanInput]


@dataclass(frozen=True, slots=True)
class LoadedSimulationPlan:
    """关联已校验候选计划与从暂存区恢复的真实输入字节。"""

    stored: StoredSimulationPlan
    files: list[UploadedFile]


class SimulationPlanDraftStore:
    """使用 MinIO 与任务 JSONL 管理隔离、可恢复的未发布计划。"""

    def __init__(self, storage: ObjectStorage, run_logs: RunLogService) -> None:
        """绑定隔离对象存储和公开日志索引。"""

        self._storage = storage
        self._run_logs = run_logs

    async def save(
        self,
        *,
        task_id: UUID,
        conversation_id: UUID,
        request_id: UUID,
        question: str,
        decomposition: TaskDecompositionResult,
        files: list[UploadedFile],
    ) -> StoredSimulationPlan:
        """保存候选计划及输入；该操作不会写入任何正式任务或子任务表。"""

        inputs: list[StoredPlanInput] = []
        prefix = f"plan-drafts/{task_id}/{request_id}"
        for index, item in enumerate(files):
            object_key = f"{prefix}/inputs/{index}"
            await self._storage.put_bytes(object_key, item.content, item.media_type)
            inputs.append(
                StoredPlanInput(
                    filename=item.filename,
                    media_type=item.media_type,
                    artifact_type=item.artifact_type,
                    source_artifact_id=item.source_artifact_id,
                    object_key=object_key,
                )
            )
        plan = StoredSimulationPlan(
            task_id=task_id,
            conversation_id=conversation_id,
            request_id=request_id,
            question=question,
            decomposition=decomposition,
            inputs=inputs,
        )
        manifest_key = f"{prefix}/manifest.json"
        await self._storage.put_bytes(
            manifest_key,
            plan.model_dump_json().encode("utf-8"),
            "application/json",
        )
        await self._run_logs.append(
            task_id=task_id,
            record_type="PLAN_DRAFT_STORED",
            actor="simulation_plan_review",
            payload={
                "request_id": str(request_id),
                "manifest_key": manifest_key,
                "decomposition_revision": (
                    decomposition.subtasks[0].decomposition_revision
                    if decomposition.subtasks
                    else decomposition.task_spec.revision
                ),
            },
        )
        return plan

    async def load_current(self, task_id: UUID) -> LoadedSimulationPlan | None:
        """恢复最新仍处于待审核状态的候选计划和真实输入。"""

        records, _ = await self._run_logs.read_after(task_id)
        closed: set[str] = set()
        for _, record in records:
            if record.get("record_type") in {"PLAN_DRAFT_PUBLISHED", "PLAN_DRAFT_SUPERSEDED"}:
                request_id = str((record.get("payload") or {}).get("request_id", ""))
                if request_id:
                    closed.add(request_id)
        for _, record in reversed(records):
            if record.get("record_type") != "PLAN_DRAFT_STORED":
                continue
            payload: dict[str, Any] = record.get("payload") or {}
            request_id = str(payload.get("request_id", ""))
            if not request_id or request_id in closed:
                continue
            manifest_key = str(payload.get("manifest_key", ""))
            raw = await self._storage.get_bytes(manifest_key)
            stored = StoredSimulationPlan.model_validate_json(raw)
            files = [
                UploadedFile(
                    filename=item.filename,
                    media_type=item.media_type,
                    content=await self._storage.get_bytes(item.object_key),
                    artifact_type=item.artifact_type,  # type: ignore[arg-type]
                    source_artifact_id=item.source_artifact_id,
                )
                for item in stored.inputs
            ]
            return LoadedSimulationPlan(stored=stored, files=files)
        return None

    async def mark(self, task_id: UUID, request_id: UUID, status: str) -> None:
        """把草稿标记为已发布或已被新草稿替代。"""

        if status not in {"PUBLISHED", "SUPERSEDED"}:
            raise ValueError("候选计划状态只允许 PUBLISHED 或 SUPERSEDED")
        await self._run_logs.append(
            task_id=task_id,
            record_type=f"PLAN_DRAFT_{status}",
            actor="simulation_plan_review",
            payload={"request_id": str(request_id)},
        )
