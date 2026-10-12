"""定义第二阶段任务事实、历史、Assignment、事件和快照 ORM 表。

ORM 只负责持久化结构，领域校验继续由 Pydantic 模型和确定性 Service 完成。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from kg_agent.infrastructure.database.base import Base


class ConversationRow(Base):
    """保存用户可见会话的稳定身份与侧栏摘要。

    会话是聊天历史的边界，一个会话可以关联多个 MasterTask；它不替代每个任务
    独立的 SharedContext，也不保存 Agent 的业务执行状态。
    """

    __tablename__ = "conversations"

    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MasterTaskRow(Base):
    """保存 MasterTask 当前状态；历史变化通过 TaskEvent 追溯。"""

    __tablename__ = "master_tasks"

    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    current_task_spec_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_decomposition_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskSpecRevisionRow(Base):
    """不可变保存每个 TaskSpec revision 的完整契约快照。"""

    __tablename__ = "task_spec_revisions"

    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_intent: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DecompositionRevisionRow(Base):
    """记录一次已经通过校验并原子发布的任务拆解版本。"""

    __tablename__ = "decomposition_revisions"

    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_spec_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    task_intent: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSubTaskRow(Base):
    """保存专业 Agent 粒度子任务的当前状态和不可变计划内容。"""

    __tablename__ = "agent_subtasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["master_task_id", "decomposition_revision"],
            ["decomposition_revisions.task_id", "decomposition_revisions.revision"],
        ),
        Index("ix_subtasks_task_status", "master_task_id", "status"),
    )

    subtask_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    master_task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decomposition_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    subtask_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    assigned_agent_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    human_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubTaskDependencyRow(Base):
    """规范化保存 AgentSubTask 依赖关系，便于确定性解锁。"""

    __tablename__ = "subtask_dependencies"

    subtask_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_subtasks.subtask_id"), primary_key=True
    )
    dependency_subtask_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_subtasks.subtask_id"), primary_key=True
    )


class AgentAssignmentRow(Base):
    """保存子任务一次执行尝试及其租约和 fencing token。"""

    __tablename__ = "agent_assignments"
    __table_args__ = (
        UniqueConstraint("subtask_id", "attempt", name="uq_assignment_subtask_attempt"),
        Index("ix_assignment_claim", "assigned_agent_type", "status", "created_at"),
    )

    assignment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subtask_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_subtasks.subtask_id"), nullable=False
    )
    assigned_agent_type: Mapped[str] = mapped_column(String(128), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactRefRow(Base):
    """保存 SharedContext 当前可引用的轻量 Artifact 元数据。"""

    __tablename__ = "artifact_refs"

    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactContentRow(Base):
    """保存 Artifact 在对象存储中的受控定位和容量元数据。"""

    __tablename__ = "artifact_contents"

    artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id"), primary_key=True
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactLineageRow(Base):
    """规范化保存 Artifact 的直接父子血缘边。"""

    __tablename__ = "artifact_lineage"
    __table_args__ = (Index("ix_artifact_lineage_parent", "parent_artifact_id"),)

    child_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id"), primary_key=True
    )
    parent_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id"), primary_key=True
    )


class PublishedGraphRow(Base):
    """保存一个已审核发布图谱版本及其 Artifact、候选版本和 Approval 血缘。"""

    __tablename__ = "published_graphs"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_published_graph_artifact"),
        Index("ix_published_graph_task_version", "task_id", "graph_version"),
    )

    graph_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id")
    )
    graph_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_candidate_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id")
    )
    approval_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublishedGraphNodeRow(Base):
    """把 PublishedGraph 的实体保存为可索引、可连接查询的 PostgreSQL 节点。"""

    __tablename__ = "published_graph_nodes"
    __table_args__ = (
        Index("ix_published_graph_node_class", "graph_id", "class_id"),
        Index("ix_published_graph_node_name", "graph_id", "name"),
    )

    graph_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("published_graphs.graph_id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    class_id: Mapped[str] = mapped_column(String(256), nullable=False)
    aliases: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSON, nullable=False)


class PublishedGraphEdgeRow(Base):
    """把 PublishedGraph 的关系保存为连接同一图版本节点的有向边。"""

    __tablename__ = "published_graph_edges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["graph_id", "source_entity_id"],
            ["published_graph_nodes.graph_id", "published_graph_nodes.entity_id"],
            ondelete="CASCADE",
            name="fk_published_edge_source",
        ),
        ForeignKeyConstraint(
            ["graph_id", "target_entity_id"],
            ["published_graph_nodes.graph_id", "published_graph_nodes.entity_id"],
            ondelete="CASCADE",
            name="fk_published_edge_target",
        ),
        Index("ix_published_graph_edge_type", "graph_id", "relation_type_id"),
        Index("ix_published_graph_edge_source", "graph_id", "source_entity_id"),
        Index("ix_published_graph_edge_target", "graph_id", "target_entity_id"),
    )

    graph_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    relation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    relation_type_id: Mapped[str] = mapped_column(String(256), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSON, nullable=False)


class PublishedGraphPropertyRow(Base):
    """保存节点属性，使属性值可独立查询且仍保留原始 JSON 类型。"""

    __tablename__ = "published_graph_properties"
    __table_args__ = (
        ForeignKeyConstraint(
            ["graph_id", "entity_id"],
            ["published_graph_nodes.graph_id", "published_graph_nodes.entity_id"],
            ondelete="CASCADE",
            name="fk_published_property_entity",
        ),
        Index("ix_published_graph_property_type", "graph_id", "property_type_id"),
        Index("ix_published_graph_property_entity", "graph_id", "entity_id"),
    )

    graph_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    property_id: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    property_type_id: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSON, nullable=False)


class PublishedGraphEvidenceRow(Base):
    """规范化保存已发布图谱的独立证据记录，支持按来源 Artifact 回溯。"""

    __tablename__ = "published_graph_evidence"
    __table_args__ = (
        Index("ix_published_graph_evidence_source", "graph_id", "source_artifact_id"),
    )

    graph_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("published_graphs.graph_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)


class AgentRunResultRow(Base):
    """不可变保存专业 Agent 一次 Assignment 运行结果。"""

    __tablename__ = "agent_run_results"

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), nullable=False, index=True
    )
    subtask_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_subtasks.subtask_id"), nullable=False
    )
    assignment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_assignments.assignment_id"),
        nullable=False,
        unique=True,
    )
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionTraceRow(Base):
    """不可变保存一次专业 Agent Run 的公开执行轨迹。"""

    __tablename__ = "execution_traces"

    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), nullable=False, index=True
    )
    subtask_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    assignment_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run_results.run_id"), unique=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HumanReviewRequestRow(Base):
    """保存审核目标快照、当前状态、用户反馈和决策 Artifact 引用。"""

    __tablename__ = "human_review_requests"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "target_artifact_id",
            "target_artifact_checksum",
            name="uq_human_review_target_version",
        ),
        Index("ix_human_review_task_status", "task_id", "status"),
        Index(
            "ix_human_review_pending_target",
            "task_id",
            "target_artifact_id",
            "target_artifact_checksum",
            "status",
        ),
    )

    review_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), nullable=False
    )
    subtask_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_subtasks.subtask_id")
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id"), nullable=False
    )
    target_artifact_checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("artifact_refs.artifact_id"), unique=True
    )
    reviewer_feedback: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskEventRow(Base):
    """只追加保存任务级审计事件；sequence 是数据库全局顺序。"""

    __tablename__ = "task_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), nullable=False, index=True
    )
    subtask_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    assignment_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SharedContextSnapshotRow(Base):
    """不可变保存 SharedTaskContext 的版本化投影。"""

    __tablename__ = "shared_context_snapshots"

    task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationMessageRow(Base):
    """持久化用户与系统的公开对话消息，供总控多轮理解使用。"""

    __tablename__ = "conversation_messages"
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("master_tasks.task_id"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiRequestRow(Base):
    """保存创建类请求的幂等键、内容摘要和可重建结果。"""

    __tablename__ = "api_requests"
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CustomSkillRow(Base):
    """保存用户自定义 Skill 的稳定身份和当前生效修订指针。

    本表不保存完整 Prompt、脚本或资源；这些不可变内容位于 revision 与 resource 表中。
    ``current_revision_id`` 不设置外键，以避免 Skill 与其首个 revision 的循环外键。
    """

    __tablename__ = "custom_skills"

    skill_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    skill_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CustomSkillRevisionRow(Base):
    """保存一个不可变自定义 Skill 修订的元数据、正文和可选脚本入口。"""

    __tablename__ = "custom_skill_revisions"
    __table_args__ = (
        UniqueConstraint("skill_id", "revision_number", name="uq_custom_skill_revision"),
        Index("ix_custom_skill_revision_status", "skill_id", "status"),
    )

    revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    skill_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("custom_skills.skill_id"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    skill_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    entrypoint: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomSkillResourceRow(Base):
    """保存一个 Skill 修订捆绑的文本、脚本或二进制资源。

    资源内容仅在 Skill 执行需要时读取；Registry 发现阶段只读取 revision 元数据。
    """

    __tablename__ = "custom_skill_resources"
    __table_args__ = (
        UniqueConstraint(
            "skill_revision_id", "relative_path", name="uq_custom_skill_resource_path"
        ),
    )

    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    skill_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("custom_skill_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CustomSkillContractCheckRow(Base):
    """保存脚本 Skill 发布前的依赖与输入输出契约试运行结果。

    检查记录绑定不可变草稿修订、资源包校验和、运行环境和协议版本；任一内容变化
    都会使旧记录自然失效，发布服务不能把历史通过结果套用到新脚本。
    """

    __tablename__ = "custom_skill_contract_checks"
    __table_args__ = (
        Index(
            "ix_custom_skill_contract_check_revision",
            "skill_revision_id",
            "checked_at",
        ),
    )

    check_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    skill_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("custom_skill_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contract_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bundle_checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    runtime_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CustomAgentRow(Base):
    """保存用户自定义专业 Agent 的稳定身份和当前生效修订指针。"""

    __tablename__ = "custom_agents"

    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CustomAgentRevisionRow(Base):
    """保存自定义 Agent 的职责正文、能力元数据及唯一权限 YAML。"""

    __tablename__ = "custom_agent_revisions"
    __table_args__ = (
        UniqueConstraint("agent_id", "revision_number", name="uq_custom_agent_revision"),
        Index("ix_custom_agent_revision_status", "agent_id", "status"),
    )

    revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("custom_agents.agent_id"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    agent_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    permissions_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    routing_examples: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
