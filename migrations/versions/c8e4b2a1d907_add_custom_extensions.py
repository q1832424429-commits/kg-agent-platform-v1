"""增加用户自定义 Agent、Skill、修订与捆绑资源表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e4b2a1d907"
down_revision: str | Sequence[str] | None = "f73c91e642ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建扩展定义的稳定身份、不可变修订和资源存储表。"""

    op.create_table(
        "custom_skills",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_revision_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("skill_id"),
        sa.UniqueConstraint("skill_key"),
    )
    op.create_index("ix_custom_skills_skill_key", "custom_skills", ["skill_key"])
    op.create_index("ix_custom_skills_status", "custom_skills", ["status"])
    op.create_table(
        "custom_skill_revisions",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("skill_markdown", sa.Text(), nullable=False),
        sa.Column("runtime_type", sa.String(length=32), nullable=False),
        sa.Column("entrypoint", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["skill_id"], ["custom_skills.skill_id"]),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("skill_id", "revision_number", name="uq_custom_skill_revision"),
    )
    op.create_index(
        "ix_custom_skill_revision_status", "custom_skill_revisions", ["skill_id", "status"]
    )
    op.create_table(
        "custom_skill_resources",
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("skill_revision_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_revision_id"],
            ["custom_skill_revisions.revision_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("resource_id"),
        sa.UniqueConstraint(
            "skill_revision_id", "relative_path", name="uq_custom_skill_resource_path"
        ),
    )
    op.create_index(
        "ix_custom_skill_resources_skill_revision_id",
        "custom_skill_resources",
        ["skill_revision_id"],
    )
    op.create_table(
        "custom_agents",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_revision_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
        sa.UniqueConstraint("agent_key"),
    )
    op.create_index("ix_custom_agents_agent_key", "custom_agents", ["agent_key"])
    op.create_index("ix_custom_agents_status", "custom_agents", ["status"])
    op.create_table(
        "custom_agent_revisions",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("agent_markdown", sa.Text(), nullable=False),
        sa.Column("permissions_yaml", sa.Text(), nullable=False),
        sa.Column("routing_examples", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["custom_agents.agent_id"]),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("agent_id", "revision_number", name="uq_custom_agent_revision"),
    )
    op.create_index(
        "ix_custom_agent_revision_status", "custom_agent_revisions", ["agent_id", "status"]
    )


def downgrade() -> None:
    """按依赖逆序删除扩展定义相关表。"""

    op.drop_index("ix_custom_agent_revision_status", table_name="custom_agent_revisions")
    op.drop_table("custom_agent_revisions")
    op.drop_index("ix_custom_agents_status", table_name="custom_agents")
    op.drop_index("ix_custom_agents_agent_key", table_name="custom_agents")
    op.drop_table("custom_agents")
    op.drop_index(
        "ix_custom_skill_resources_skill_revision_id",
        table_name="custom_skill_resources",
    )
    op.drop_table("custom_skill_resources")
    op.drop_index("ix_custom_skill_revision_status", table_name="custom_skill_revisions")
    op.drop_table("custom_skill_revisions")
    op.drop_index("ix_custom_skills_status", table_name="custom_skills")
    op.drop_index("ix_custom_skills_skill_key", table_name="custom_skills")
    op.drop_table("custom_skills")
