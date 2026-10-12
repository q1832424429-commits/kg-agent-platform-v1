"""增加 PostgreSQL 已发布图谱节点、边、属性与证据表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f73c91e642ab"
down_revision: str | Sequence[str] | None = "e31b9f62d011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建图版本主表以及节点、边、属性和证据明细表。"""

    op.create_table(
        "published_graphs",
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("graph_version", sa.String(length=128), nullable=False),
        sa.Column("source_candidate_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("approval_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approval_artifact_id"], ["artifact_refs.artifact_id"]),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact_refs.artifact_id"]),
        sa.ForeignKeyConstraint(["source_candidate_artifact_id"], ["artifact_refs.artifact_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["master_tasks.task_id"]),
        sa.PrimaryKeyConstraint("graph_id"),
        sa.UniqueConstraint("artifact_id", name="uq_published_graph_artifact"),
    )
    op.create_index(
        "ix_published_graph_task_version",
        "published_graphs",
        ["task_id", "graph_version"],
    )

    op.create_table(
        "published_graph_nodes",
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("class_id", sa.String(length=256), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["published_graphs.graph_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("graph_id", "entity_id"),
    )
    op.create_index(
        "ix_published_graph_node_class", "published_graph_nodes", ["graph_id", "class_id"]
    )
    op.create_index("ix_published_graph_node_name", "published_graph_nodes", ["graph_id", "name"])

    op.create_table(
        "published_graph_edges",
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("relation_id", sa.Text(), nullable=False),
        sa.Column("source_entity_id", sa.Text(), nullable=False),
        sa.Column("relation_type_id", sa.String(length=256), nullable=False),
        sa.Column("target_entity_id", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["graph_id", "source_entity_id"],
            ["published_graph_nodes.graph_id", "published_graph_nodes.entity_id"],
            name="fk_published_edge_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["graph_id", "target_entity_id"],
            ["published_graph_nodes.graph_id", "published_graph_nodes.entity_id"],
            name="fk_published_edge_target",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("graph_id", "relation_id"),
    )
    op.create_index(
        "ix_published_graph_edge_type",
        "published_graph_edges",
        ["graph_id", "relation_type_id"],
    )
    op.create_index(
        "ix_published_graph_edge_source",
        "published_graph_edges",
        ["graph_id", "source_entity_id"],
    )
    op.create_index(
        "ix_published_graph_edge_target",
        "published_graph_edges",
        ["graph_id", "target_entity_id"],
    )

    op.create_table(
        "published_graph_properties",
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("property_type_id", sa.String(length=256), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["graph_id", "entity_id"],
            ["published_graph_nodes.graph_id", "published_graph_nodes.entity_id"],
            name="fk_published_property_entity",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("graph_id", "property_id"),
    )
    op.create_index(
        "ix_published_graph_property_type",
        "published_graph_properties",
        ["graph_id", "property_type_id"],
    )
    op.create_index(
        "ix_published_graph_property_entity",
        "published_graph_properties",
        ["graph_id", "entity_id"],
    )

    op.create_table(
        "published_graph_evidence",
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Text(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["published_graphs.graph_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("graph_id", "evidence_id"),
    )
    op.create_index(
        "ix_published_graph_evidence_source",
        "published_graph_evidence",
        ["graph_id", "source_artifact_id"],
    )


def downgrade() -> None:
    """按依赖逆序删除 PostgreSQL 图存储表。"""

    op.drop_index("ix_published_graph_evidence_source", table_name="published_graph_evidence")
    op.drop_table("published_graph_evidence")
    op.drop_index("ix_published_graph_property_entity", table_name="published_graph_properties")
    op.drop_index("ix_published_graph_property_type", table_name="published_graph_properties")
    op.drop_table("published_graph_properties")
    op.drop_index("ix_published_graph_edge_target", table_name="published_graph_edges")
    op.drop_index("ix_published_graph_edge_source", table_name="published_graph_edges")
    op.drop_index("ix_published_graph_edge_type", table_name="published_graph_edges")
    op.drop_table("published_graph_edges")
    op.drop_index("ix_published_graph_node_name", table_name="published_graph_nodes")
    op.drop_index("ix_published_graph_node_class", table_name="published_graph_nodes")
    op.drop_table("published_graph_nodes")
    op.drop_index("ix_published_graph_task_version", table_name="published_graphs")
    op.drop_table("published_graphs")
