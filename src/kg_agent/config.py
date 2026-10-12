"""读取已经确认的模型、数据库、Assignment 与对象存储运行配置。

配置值只从环境变量读取；模块不创建外部连接，也不在日志中暴露密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from kg_agent.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class Settings:
    """保存 V1 当前阶段所需的环境配置。

    文件限制采用可覆盖的开发默认值：单文件 50 MiB、单任务 20 个文件、总量
    500 MiB。部署前可以通过环境变量收紧或放宽，不影响领域契约。
    """

    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str | None
    controller_max_repairs: int
    database_url: str | None
    assignment_lease_seconds: int
    assignment_heartbeat_seconds: int
    minio_endpoint: str | None
    minio_access_key: str | None
    minio_secret_key: str | None
    minio_bucket: str
    minio_secure: bool
    artifact_max_file_bytes: int
    artifact_max_files_per_task: int
    artifact_max_total_bytes_per_task: int
    mem0_enabled: bool
    mem0_api_key: str | None
    mem0_base_url: str
    mem0_user_id: str
    mem0_app_id: str
    mem0_top_k: int
    mem0_candidate_top_k: int
    mem0_threshold: float | None
    mem0_dedup_threshold: float
    mem0_merge_threshold: float
    mem0_rerank: bool
    mem0_timeout_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        """从环境变量创建并交叉校验配置对象。

        Returns:
            完成基础类型转换的不可变配置对象。

        Raises:
            ConfigurationError: 任一数字、布尔或组合配置不合法时抛出。
        """

        # 总控与专业 Agent 只允许一次结构化格式修复。重复同一请求不会补充新事实，
        # 只会放大供应商慢响应造成的用户等待时间。
        raw_max_repairs = os.getenv("CONTROLLER_MAX_REPAIRS", "1")
        try:
            max_repairs = int(raw_max_repairs)
        except ValueError as exc:
            raise ConfigurationError("CONTROLLER_MAX_REPAIRS 必须是整数") from exc
        if max_repairs < 0:
            raise ConfigurationError("CONTROLLER_MAX_REPAIRS 不能小于 0")

        lease_seconds = cls._read_positive_int("ASSIGNMENT_LEASE_SECONDS", 60)
        heartbeat_seconds = cls._read_positive_int("ASSIGNMENT_HEARTBEAT_SECONDS", 20)
        if heartbeat_seconds >= lease_seconds:
            raise ConfigurationError("ASSIGNMENT_HEARTBEAT_SECONDS 必须小于租约时长")

        max_file = cls._read_positive_int("ARTIFACT_MAX_FILE_BYTES", 50 * 1024 * 1024)
        max_files = cls._read_positive_int("ARTIFACT_MAX_FILES_PER_TASK", 20)
        max_total = cls._read_positive_int("ARTIFACT_MAX_TOTAL_BYTES_PER_TASK", 500 * 1024 * 1024)
        if max_file > max_total:
            raise ConfigurationError("ARTIFACT_MAX_FILE_BYTES 不能大于单任务总容量")

        mem0_api_key = os.getenv("MEM0_API_KEY") or None
        # 未显式设置开关时，只要配置了 Key 就自动启用，避免旧环境因新增可选能力无法启动。
        raw_mem0_enabled = os.getenv("MEM0_ENABLED")
        mem0_enabled = (
            bool(mem0_api_key)
            if raw_mem0_enabled is None
            else cls._parse_bool("MEM0_ENABLED", raw_mem0_enabled)
        )
        if mem0_enabled and not mem0_api_key:
            raise ConfigurationError("MEM0_ENABLED=true 时必须配置 MEM0_API_KEY")
        mem0_user_id = os.getenv("MEM0_USER_ID", "kg-agent-v1-single-user").strip()
        if not mem0_user_id:
            raise ConfigurationError("MEM0_USER_ID 不能为空")
        mem0_app_id = os.getenv("MEM0_APP_ID", "kg-agent-platform-production").strip()
        if not mem0_app_id:
            raise ConfigurationError("MEM0_APP_ID 不能为空")
        mem0_top_k = cls._read_positive_int("MEM0_TOP_K", 3)
        if mem0_top_k > 3:
            raise ConfigurationError("MEM0_TOP_K 不能大于 3")
        mem0_candidate_top_k = cls._read_positive_int("MEM0_CANDIDATE_TOP_K", 16)
        if mem0_candidate_top_k < mem0_top_k:
            raise ConfigurationError("MEM0_CANDIDATE_TOP_K 不能小于 MEM0_TOP_K")
        mem0_threshold = cls._read_optional_probability("MEM0_THRESHOLD", 0.5)
        mem0_dedup_threshold = cls._read_optional_probability("MEM0_DEDUP_THRESHOLD", 0.92)
        if mem0_dedup_threshold is None:
            raise ConfigurationError("MEM0_DEDUP_THRESHOLD 不能为空")
        mem0_merge_threshold = cls._read_optional_probability("MEM0_MERGE_THRESHOLD", 0.8)
        if mem0_merge_threshold is None:
            raise ConfigurationError("MEM0_MERGE_THRESHOLD 不能为空")
        if mem0_merge_threshold >= mem0_dedup_threshold:
            raise ConfigurationError("MEM0_MERGE_THRESHOLD 必须小于 MEM0_DEDUP_THRESHOLD")

        return cls(
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL") or None,
            controller_max_repairs=max_repairs,
            database_url=os.getenv("DATABASE_URL") or None,
            assignment_lease_seconds=lease_seconds,
            assignment_heartbeat_seconds=heartbeat_seconds,
            minio_endpoint=os.getenv("MINIO_ENDPOINT") or None,
            minio_access_key=os.getenv("MINIO_ACCESS_KEY") or None,
            minio_secret_key=os.getenv("MINIO_SECRET_KEY") or None,
            minio_bucket=os.getenv("MINIO_BUCKET", "kg-agent-artifacts"),
            minio_secure=cls._read_bool("MINIO_SECURE", False),
            artifact_max_file_bytes=max_file,
            artifact_max_files_per_task=max_files,
            artifact_max_total_bytes_per_task=max_total,
            mem0_enabled=mem0_enabled,
            mem0_api_key=mem0_api_key,
            mem0_base_url=os.getenv("MEM0_BASE_URL", "https://api.mem0.ai"),
            mem0_user_id=mem0_user_id,
            mem0_app_id=mem0_app_id,
            mem0_top_k=mem0_top_k,
            mem0_candidate_top_k=mem0_candidate_top_k,
            mem0_threshold=mem0_threshold,
            mem0_dedup_threshold=mem0_dedup_threshold,
            mem0_merge_threshold=mem0_merge_threshold,
            mem0_rerank=cls._read_bool("MEM0_RERANK", True),
            mem0_timeout_seconds=cls._read_positive_float("MEM0_TIMEOUT_SECONDS", 20.0),
        )

    @staticmethod
    def _read_positive_int(name: str, default: int) -> int:
        """读取正整数环境变量，非法时抛出统一配置异常。"""

        raw_value = os.getenv(name, str(default))
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConfigurationError(f"{name} 必须是整数") from exc
        if value <= 0:
            raise ConfigurationError(f"{name} 必须大于 0")
        return value

    @staticmethod
    def _read_bool(name: str, default: bool) -> bool:
        """读取 true/false 布尔环境变量，拒绝含糊值。"""

        raw_value = os.getenv(name)
        if raw_value is None:
            return default
        return Settings._parse_bool(name, raw_value)

    @staticmethod
    def _parse_bool(name: str, raw_value: str) -> bool:
        """解析调用方已经取得的布尔字符串。"""

        normalized = raw_value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise ConfigurationError(f"{name} 必须是 true 或 false")

    @staticmethod
    def _read_positive_float(name: str, default: float) -> float:
        """读取正浮点环境变量，供外部服务超时等配置使用。"""

        raw_value = os.getenv(name, str(default))
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ConfigurationError(f"{name} 必须是数字") from exc
        if value <= 0:
            raise ConfigurationError(f"{name} 必须大于 0")
        return value

    @staticmethod
    def _read_optional_probability(name: str, default: float | None) -> float | None:
        """读取 0 到 1 的可选概率；空字符串表示不使用阈值。"""

        raw_value = os.getenv(name)
        if raw_value is None:
            return default
        if not raw_value.strip():
            return None
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ConfigurationError(f"{name} 必须是数字或空字符串") from exc
        if not 0 <= value <= 1:
            raise ConfigurationError(f"{name} 必须在 0 到 1 之间")
        return value

    def require_database(self) -> str:
        """返回数据库连接串；未配置持久化环境时抛出配置异常。"""

        if not self.database_url:
            raise ConfigurationError("使用 PostgreSQL 持久化前必须配置 DATABASE_URL")
        return self.database_url

    def require_minio(self) -> tuple[str, str, str, str, bool]:
        """返回 MinIO 连接参数；缺少任一凭据时拒绝创建真实适配器。"""

        if not self.minio_endpoint:
            raise ConfigurationError("使用 MinIO 前必须配置 MINIO_ENDPOINT")
        if not self.minio_access_key:
            raise ConfigurationError("使用 MinIO 前必须配置 MINIO_ACCESS_KEY")
        if not self.minio_secret_key:
            raise ConfigurationError("使用 MinIO 前必须配置 MINIO_SECRET_KEY")
        return (
            self.minio_endpoint,
            self.minio_access_key,
            self.minio_secret_key,
            self.minio_bucket,
            self.minio_secure,
        )

    def require_deepseek(self) -> tuple[str, str, str]:
        """返回真实 DeepSeek 调用需要的完整配置。"""

        if not self.deepseek_api_key:
            raise ConfigurationError("使用 DeepSeekModelAdapter 前必须配置 DEEPSEEK_API_KEY")
        if not self.deepseek_model:
            raise ConfigurationError("使用 DeepSeekModelAdapter 前必须配置 DEEPSEEK_MODEL")
        return self.deepseek_api_key, self.deepseek_base_url, self.deepseek_model

    def require_mem0(self) -> tuple[str, str, str, str, float]:
        """返回真实 Mem0 Cloud 调用配置；禁用或缺少 Key 时拒绝创建适配器。"""

        if not self.mem0_enabled:
            raise ConfigurationError("Mem0 长期记忆当前未启用")
        if not self.mem0_api_key:
            raise ConfigurationError("使用 Mem0 Cloud 前必须配置 MEM0_API_KEY")
        return (
            self.mem0_api_key,
            self.mem0_base_url,
            self.mem0_user_id,
            self.mem0_app_id,
            self.mem0_timeout_seconds,
        )
