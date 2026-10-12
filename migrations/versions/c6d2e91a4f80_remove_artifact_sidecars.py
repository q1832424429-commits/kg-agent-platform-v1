"""移除不再作为正式 Skill 产物使用的 Artifact 附加对象字段。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d2e91a4f80"
down_revision: str | Sequence[str] | None = "b4e7c1a0926d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """仅在没有附加对象时删列，避免静默丢弃历史 Skill 结果。"""

    remaining = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM artifact_contents "
            "WHERE extension_object_key IS NOT NULL OR extension_size_bytes <> 0"
        )
    ).scalar_one()
    if remaining:
        raise RuntimeError("存在历史 Artifact 附加对象，必须先归档或处理后才能删除字段")
    with op.batch_alter_table("artifact_contents") as batch_op:
        batch_op.drop_constraint("uq_artifact_extension_object_key", type_="unique")
        batch_op.drop_column("extension_size_bytes")
        batch_op.drop_column("extension_object_key")


def downgrade() -> None:
    """仅恢复旧表结构；已经删除的附加对象内容不会自动重建。"""

    with op.batch_alter_table("artifact_contents") as batch_op:
        batch_op.add_column(sa.Column("extension_object_key", sa.String(length=1024)))
        batch_op.add_column(
            sa.Column("extension_size_bytes", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.create_unique_constraint(
            "uq_artifact_extension_object_key", ["extension_object_key"]
        )
