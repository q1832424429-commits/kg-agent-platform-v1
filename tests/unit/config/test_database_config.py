"""验证第二阶段数据库和 Assignment 租约配置。"""

import pytest

from kg_agent.config import Settings
from kg_agent.exceptions import ConfigurationError


def test_database_configuration_is_optional_for_unit_tests(monkeypatch) -> None:
    """不使用持久化适配器的单元测试可以不配置数据库连接。"""

    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings.from_env()
    assert settings.database_url is None
    with pytest.raises(ConfigurationError):
        settings.require_database()


def test_heartbeat_must_be_shorter_than_lease(monkeypatch) -> None:
    """心跳周期不能等于或超过 Assignment 租约。"""

    monkeypatch.setenv("ASSIGNMENT_LEASE_SECONDS", "20")
    monkeypatch.setenv("ASSIGNMENT_HEARTBEAT_SECONDS", "20")
    with pytest.raises(ConfigurationError):
        Settings.from_env()
