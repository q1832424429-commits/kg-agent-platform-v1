"""增加多轮消息和创建请求幂等记录

Revision ID: e31b9f62d011
Revises: c72b44fa5e21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e31b9f62d011"
down_revision: str | None = "c72b44fa5e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建公开会话消息和 API 幂等请求表。"""
    op.create_table(
        "conversation_messages",
        sa.Column("message_id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("master_tasks.task_id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"]
    )
    op.create_index("ix_conversation_messages_task_id", "conversation_messages", ["task_id"])
    op.create_table(
        "api_requests",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("request_hash", sa.String(80), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("task_id", sa.Uuid()),
        sa.Column("conversation_id", sa.Uuid()),
        sa.Column("response_payload", sa.JSON()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_requests_status", "api_requests", ["status"])
    op.create_index("ix_api_requests_task_id", "api_requests", ["task_id"])
    op.create_index("ix_api_requests_conversation_id", "api_requests", ["conversation_id"])


def downgrade() -> None:
    """按外键依赖逆序删除本次新增表。"""
    op.drop_table("api_requests")
    op.drop_table("conversation_messages")
