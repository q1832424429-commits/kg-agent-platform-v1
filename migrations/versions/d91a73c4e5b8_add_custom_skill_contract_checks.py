"""增加自定义脚本 Skill 发布前契约检查记录。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d91a73c4e5b8"
down_revision: str | Sequence[str] | None = "c8e4b2a1d907"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建与不可变 Skill revision 绑定的检查结果表。"""

    op.create_table(
        "custom_skill_contract_checks",
        sa.Column("check_id", sa.Uuid(), nullable=False),
        sa.Column("skill_revision_id", sa.Uuid(), nullable=False),
        sa.Column("contract_profile_id", sa.String(length=128), nullable=False),
        sa.Column("bundle_checksum", sa.String(length=80), nullable=False),
        sa.Column("runtime_profile_id", sa.String(length=128), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_revision_id"],
            ["custom_skill_revisions.revision_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("check_id"),
    )
    op.create_index(
        "ix_custom_skill_contract_checks_skill_revision_id",
        "custom_skill_contract_checks",
        ["skill_revision_id"],
    )
    op.create_index(
        "ix_custom_skill_contract_checks_status",
        "custom_skill_contract_checks",
        ["status"],
    )
    op.create_index(
        "ix_custom_skill_contract_check_revision",
        "custom_skill_contract_checks",
        ["skill_revision_id", "checked_at"],
    )


def downgrade() -> None:
    """删除脚本契约检查记录，不影响 Skill 定义和资源。"""

    op.drop_index(
        "ix_custom_skill_contract_check_revision",
        table_name="custom_skill_contract_checks",
    )
    op.drop_index(
        "ix_custom_skill_contract_checks_status",
        table_name="custom_skill_contract_checks",
    )
    op.drop_index(
        "ix_custom_skill_contract_checks_skill_revision_id",
        table_name="custom_skill_contract_checks",
    )
    op.drop_table("custom_skill_contract_checks")
