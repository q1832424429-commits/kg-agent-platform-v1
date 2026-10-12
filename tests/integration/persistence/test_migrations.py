"""验证 Alembic 初始迁移可以升级、回滚并再次升级。"""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def table_names(database_path: Path) -> set[str]:
    """读取 SQLite 测试数据库中的用户表名。"""

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def artifact_content_columns(database_path: Path) -> set[str]:
    """读取最终迁移版本的 Artifact 内容表字段。"""

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("PRAGMA table_info(artifact_contents)").fetchall()
    return {str(row[1]) for row in rows}


def test_migration_upgrade_downgrade_upgrade(project_root: Path, tmp_path: Path) -> None:
    """初始迁移必须支持可重复的 upgrade、downgrade 和重新 upgrade。"""

    database_path = tmp_path / "migration.db"
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")

    command.upgrade(config, "head")
    assert {
        "master_tasks",
        "agent_assignments",
        "task_events",
        "artifact_contents",
        "artifact_lineage",
    } <= table_names(database_path)
    assert {"object_key", "size_bytes"} <= artifact_content_columns(database_path)
    assert not {"extension_object_key", "extension_size_bytes"} & artifact_content_columns(
        database_path
    )

    command.downgrade(config, "base")
    assert "master_tasks" not in table_names(database_path)

    command.upgrade(config, "head")
    assert "shared_context_snapshots" in table_names(database_path)
