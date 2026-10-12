"""在显式配置的专用 MinIO 测试桶中验证真实对象存储往返。"""

import os
from uuid import uuid4

import pytest

from kg_agent.exceptions import ObjectStorageError
from kg_agent.infrastructure.object_storage.minio import MinioObjectStorage


@pytest.mark.asyncio
async def test_minio_live_content_roundtrip() -> None:
    """只在显式 opt-in 时运行，清理范围限制为本次 UUID 对象而非整个 Bucket。"""

    endpoint = os.getenv("KG_AGENT_TEST_MINIO_ENDPOINT")
    access_key = os.getenv("KG_AGENT_TEST_MINIO_ACCESS_KEY")
    secret_key = os.getenv("KG_AGENT_TEST_MINIO_SECRET_KEY")
    if not all((endpoint, access_key, secret_key)):
        pytest.skip("未配置 KG_AGENT_TEST_MINIO_*，跳过真实 MinIO 联调")
    bucket = os.getenv("KG_AGENT_TEST_MINIO_BUCKET", "kg-agent-test-artifacts")
    if not bucket.startswith("kg-agent-test-"):
        pytest.fail("MinIO 集成测试必须使用 kg-agent-test- 前缀的专用 Bucket")
    secure_value = os.getenv("KG_AGENT_TEST_MINIO_SECURE", "false").lower()
    if secure_value not in {"true", "false"}:
        pytest.fail("KG_AGENT_TEST_MINIO_SECURE 必须为 true 或 false")

    storage = MinioObjectStorage(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        secure=secure_value == "true",
    )
    await storage.ensure_bucket()
    key = f"integration-tests/{uuid4()}/content.bin"
    try:
        uri = await storage.put_bytes(key, b"stage-three-roundtrip", "application/pdf")
        assert uri == f"minio://{bucket}/{key}"
        assert await storage.get_bytes(key) == b"stage-three-roundtrip"
        await storage.put_bytes(key, b"stage-three-roundtrip", "application/pdf")
        with pytest.raises(ObjectStorageError):
            await storage.put_bytes(key, b"conflicting-content", "application/pdf")
    finally:
        # 不删除测试桶，只移除本次测试自己创建的唯一对象。
        await storage.delete(key)
