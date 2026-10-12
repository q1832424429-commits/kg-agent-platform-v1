"""删除会把 Skill 误解为多执行模式的遗留字段。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a6c09d7e31"
down_revision: str | Sequence[str] | None = "d91a73c4e5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """移除运行类型；执行器改由发布修订的实际资源确定。"""

    with op.batch_alter_table("custom_skill_revisions") as batch_op:
        batch_op.drop_column("runtime_type")


def downgrade() -> None:
    """仅为回滚恢复兼容列；该列不再承担执行选择。"""

    with op.batch_alter_table("custom_skill_revisions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "runtime_type",
                sa.String(length=32),
                nullable=False,
                server_default="AUTO",
            )
        )
