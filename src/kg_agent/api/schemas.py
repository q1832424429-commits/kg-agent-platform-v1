"""定义第七阶段演示 API 的请求结果视图。

这些模型只负责对外展示，不改变 TaskSpec、AgentSubTask、Artifact 等核心领域契约。
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DemoSubTaskView(BaseModel):
    """展示总控 Agent 拆解并分配给专业 Agent 的一个子任务。"""

    subtask_id: UUID
    decomposition_revision: int = Field(ge=1)
    name: str
    subtask_type: str
    assigned_agent_type: str
    status: str
    dependency_subtask_ids: list[UUID] = Field(default_factory=list)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)


class DemoAssignmentView(BaseModel):
    """展示子任务的一次 Worker 分配、认领和执行终态。"""

    assignment_id: UUID
    subtask_id: UUID
    assigned_agent_type: str
    claimed_by: str | None
    status: str
    attempt: int


class DemoSkillExecutionView(BaseModel):
    """展示专业 Agent 选用的 Skill、耗时及输出 Artifact。"""

    skill_name: str
    duration_ms: float | None = None
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(default_factory=list)


class DemoWorkerRunView(BaseModel):
    """展示 Worker 执行一个专业 Agent Assignment 的结构化结果。"""

    run_id: UUID
    assignment_id: UUID
    agent_type: str
    status: str
    skills: list[DemoSkillExecutionView] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)


class DemoArtifactView(BaseModel):
    """展示正式 Artifact 的类型、血缘、摘要和可选 JSON 内容预览。"""

    artifact_id: UUID
    artifact_type: str
    producer_agent: str
    producer_skill: str | None
    parent_artifact_ids: list[UUID] = Field(default_factory=list)
    content_summary: str
    checksum: str
    is_mock_output: bool
    content_preview: Any | None = None


class DemoRunResponse(BaseModel):
    """返回一次上传、路由、任务分配、Worker 和 Skill 的完整可观察结果。"""

    task_id: UUID
    conversation_id: UUID
    task_intent: str
    requires_clarification: bool
    clarification_question: str | None
    task_spec: dict[str, Any]
    subtasks: list[DemoSubTaskView] = Field(default_factory=list)
    assignments: list[DemoAssignmentView] = Field(default_factory=list)
    worker_runs: list[DemoWorkerRunView] = Field(default_factory=list)
    mock_parsed_text_preview: list[str] = Field(
        default_factory=list,
        description="文档解析 Skill 产生的公开文字预览。",
    )
    artifacts: list[DemoArtifactView] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """统一返回可预期业务错误，不泄露内部堆栈或密钥。"""

    error_code: str
    message: str
