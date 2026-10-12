"""验证 Mem0 Cloud 长期记忆环境配置。"""

import pytest

from kg_agent.config import Settings
from kg_agent.exceptions import ConfigurationError


def test_mem0_is_enabled_automatically_when_api_key_exists(monkeypatch) -> None:
    """只配置 API Key 时应启用 Mem0，减少部署时的重复开关。"""

    monkeypatch.delenv("MEM0_ENABLED", raising=False)
    monkeypatch.setenv("MEM0_API_KEY", "test-key")

    settings = Settings.from_env()

    assert settings.mem0_enabled is True
    assert settings.require_mem0()[0] == "test-key"


def test_mem0_enabled_requires_server_api_key(monkeypatch) -> None:
    """显式启用但缺少 Key 时必须在启动前失败。"""

    monkeypatch.setenv("MEM0_ENABLED", "true")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)

    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_mem0_recall_pipeline_reads_candidate_and_dedup_configuration(monkeypatch) -> None:
    """候选池、最终数量、重排与去重阈值应由服务端配置统一控制。"""

    monkeypatch.setenv("MEM0_ENABLED", "false")
    monkeypatch.setenv("MEM0_TOP_K", "3")
    monkeypatch.setenv("MEM0_CANDIDATE_TOP_K", "12")
    monkeypatch.setenv("MEM0_THRESHOLD", "0.50")
    monkeypatch.setenv("MEM0_DEDUP_THRESHOLD", "0.92")
    monkeypatch.setenv("MEM0_MERGE_THRESHOLD", "0.80")
    monkeypatch.setenv("MEM0_RERANK", "true")

    settings = Settings.from_env()

    assert settings.mem0_top_k == 3
    assert settings.mem0_candidate_top_k == 12
    assert settings.mem0_threshold == 0.5
    assert settings.mem0_dedup_threshold == 0.92
    assert settings.mem0_merge_threshold == 0.8
    assert settings.mem0_rerank is True


def test_mem0_candidate_pool_cannot_be_smaller_than_final_count(monkeypatch) -> None:
    """候选池小于最终召回条数时应在启动前拒绝错误配置。"""

    monkeypatch.setenv("MEM0_ENABLED", "false")
    monkeypatch.setenv("MEM0_TOP_K", "3")
    monkeypatch.setenv("MEM0_CANDIDATE_TOP_K", "2")

    with pytest.raises(ConfigurationError, match="MEM0_CANDIDATE_TOP_K"):
        Settings.from_env()


def test_mem0_final_recall_count_cannot_exceed_three(monkeypatch) -> None:
    """Agent 长期记忆输入必须固定在最多三条，避免配置扩大 Prompt。"""

    monkeypatch.setenv("MEM0_ENABLED", "false")
    monkeypatch.setenv("MEM0_TOP_K", "4")
    monkeypatch.setenv("MEM0_CANDIDATE_TOP_K", "12")

    with pytest.raises(ConfigurationError, match="MEM0_TOP_K"):
        Settings.from_env()


def test_mem0_merge_threshold_must_be_lower_than_dedup_threshold(monkeypatch) -> None:
    """合并区间上界不能覆盖去重区间，否则三档策略会产生歧义。"""

    monkeypatch.setenv("MEM0_ENABLED", "false")
    monkeypatch.setenv("MEM0_MERGE_THRESHOLD", "0.92")
    monkeypatch.setenv("MEM0_DEDUP_THRESHOLD", "0.92")

    with pytest.raises(ConfigurationError, match="MEM0_MERGE_THRESHOLD"):
        Settings.from_env()
