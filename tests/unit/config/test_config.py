"""验证第一阶段环境配置读取不会写死具体DeepSeek模型。"""

import pytest

from kg_agent.config import Settings
from kg_agent.exceptions import ConfigurationError


def test_settings_allow_fake_model_without_api_key(monkeypatch) -> None:
    """自动测试环境可以不配置真实模型密钥和名称。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.deepseek_api_key is None
    assert settings.deepseek_model is None


def test_real_deepseek_adapter_requires_key_and_model(monkeypatch) -> None:
    """创建真实适配器前必须显式配置密钥和模型名称。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    with pytest.raises(ConfigurationError):
        Settings.from_env().require_deepseek()
