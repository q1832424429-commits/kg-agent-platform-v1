"""验证 MinIO 凭据与 Artifact 容量限制配置。"""

import pytest

from kg_agent.config import Settings
from kg_agent.exceptions import ConfigurationError


def test_artifact_limits_have_conservative_development_defaults(monkeypatch) -> None:
    """未配置时采用 50 MiB、20 个原始文件和 500 MiB 总量。"""

    for name in (
        "ARTIFACT_MAX_FILE_BYTES",
        "ARTIFACT_MAX_FILES_PER_TASK",
        "ARTIFACT_MAX_TOTAL_BYTES_PER_TASK",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.artifact_max_file_bytes == 50 * 1024 * 1024
    assert settings.artifact_max_files_per_task == 20
    assert settings.artifact_max_total_bytes_per_task == 500 * 1024 * 1024


def test_minio_requires_endpoint_and_both_credentials(monkeypatch) -> None:
    """真实 MinIO 适配器不能在凭据不完整时启动。"""

    for name in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError):
        Settings.from_env().require_minio()


def test_minio_secure_rejects_ambiguous_boolean(monkeypatch) -> None:
    """MINIO_SECURE 只接受明确布尔值。"""

    monkeypatch.setenv("MINIO_SECURE", "sometimes")

    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_single_artifact_limit_cannot_exceed_task_total(monkeypatch) -> None:
    """单 Artifact 限额不能比单任务总容量更大。"""

    monkeypatch.setenv("ARTIFACT_MAX_FILE_BYTES", "11")
    monkeypatch.setenv("ARTIFACT_MAX_TOTAL_BYTES_PER_TASK", "10")

    with pytest.raises(ConfigurationError):
        Settings.from_env()
