"""为 Artifact 内容记录增加独立附加数据对象定位与容量。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e7c1a0926d"
down_revision: str | Sequence[str] | None = "9a4c2e8b7d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为旧记录保留空附加数据并对新侧车对象加唯一定位约束。"""

    with op.batch_alter_table("artifact_contents") as batch_op:
        batch_op.add_column(sa.Column("extension_object_key", sa.String(length=1024)))
        batch_op.add_column(
            sa.Column("extension_size_bytes", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.create_unique_constraint(
            "uq_artifact_extension_object_key", ["extension_object_key"]
        )


def downgrade() -> None:
    """只撤销附加对象元数据列，不改写历史标准 Artifact 内容。"""

    with op.batch_alter_table("artifact_contents") as batch_op:
        batch_op.drop_constraint("uq_artifact_extension_object_key", type_="unique")
        batch_op.drop_column("extension_size_bytes")
        batch_op.drop_column("extension_object_key")
