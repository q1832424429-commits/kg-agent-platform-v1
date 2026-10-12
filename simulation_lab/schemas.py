"""定义 Agent 验证实验室专用的执行日志和进度契约。

这些模型只属于测试实验室，不修改项目已经冻结的 TaskSpec、AgentSubTask、
AgentAssignment、Artifact 或 AgentRunResult 公共契约。
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.api.schemas import DemoRunResponse
from kg_agent.domain.memory_models import MemoryRef, MemoryWriteReceipt


class SimulationLogEntry(BaseModel):
    """描述一个按发生顺序展示、供用户人工检查的公开执行事实。"""

    sequence: int = Field(ge=1)
    phase: Literal[
        "INPUT",
        "MEMORY",
        "INTENT",
        "TASK_SPEC",
        "COORDINATION",
        "DECOMPOSITION",
        "ASSIGNMENT",
        "CLAIM",
        "AGENT_RUN",
        "SKILL",
        "TIMING",
        "ARTIFACT",
        "PLAN_REVIEW",
        "COMPLETE",
    ]
    actor: str
    action: str
    status: Literal["INFO", "RUNNING", "SUCCESS", "WARNING", "FAILED"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SimulationProgress(BaseModel):
    """描述虚拟任务执行后的完成百分比和当前阶段。"""

    percent: int = Field(ge=0, le=100)
    stage: str
    completed_subtasks: int = Field(ge=0)
    total_subtasks: int = Field(ge=0)


class SimulationPlanReview(BaseModel):
    """描述测试端对当前意图和子任务计划的人工执行许可。"""

    decomposition_revision: int = Field(ge=1)
    status: Literal["PENDING", "APPROVED"]
    task_intent: str
    business_goal: str
    subtask_count: int = Field(ge=0)


class SimulationPlanRevisionRequest(BaseModel):
    """描述用户在计划审核窗口提交给总控 Agent 的修改意见。"""

    reviewer_id: str = Field(default="simulation-user", min_length=1, max_length=128)
    feedback: str = Field(min_length=1, max_length=4000)


class SimulationPlanApprovalRequest(BaseModel):
    """描述用户批准草稿时确认的完整拓扑顺序及同阶段执行优先级。"""

    reviewer_id: str = Field(default="simulation-user", min_length=1, max_length=128)
    ordered_subtask_ids: list[UUID]


class SimulationMemoryReport(BaseModel):
    """公开测试端本轮召回和写入状态，不包含 Mem0 凭据或完整历史会话。"""

    enabled: bool
    provider: str
    app_id: str
    recall_status: Literal["SUCCEEDED", "FAILED", "DISABLED"]
    recalled_memories: list[MemoryRef] = Field(default_factory=list)
    write_receipt: MemoryWriteReceipt | None = None


class SimulationTimingEntry(BaseModel):
    """记录测试任务中一个公开阶段、Agent 或 Skill 的墙钟耗时。"""

    sequence: int = Field(ge=1)
    # 模型调用是 Agent 与 Skill 墙钟耗时的组成部分，但需要单独展示，便于区分
    # 供应商响应、结构化输出校验和业务 Skill 本身的耗时。
    category: Literal[
        "MEMORY",
        "CONTROLLER",
        "MODEL",
        "AGENT",
        "SKILL",
        "COORDINATION",
        "TOTAL",
    ]
    actor: str
    operation: str
    duration_ms: float = Field(ge=0)
    subtask_id: str | None = None
    run_id: str | None = None


class SimulationRunReport(BaseModel):
    """汇总真实 Agent 决策、Skill 执行和人工检查日志。"""

    observed: DemoRunResponse
    memory: SimulationMemoryReport
    timings: list[SimulationTimingEntry] = Field(default_factory=list)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    plan_review: SimulationPlanReview | None = None
    logs: list[SimulationLogEntry]
    progress: SimulationProgress
