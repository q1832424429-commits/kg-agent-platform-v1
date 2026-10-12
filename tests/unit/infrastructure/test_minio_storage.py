"""验证 MinIO 适配器的 SDK 调用、资源释放和错误转换；不访问真实服务。"""

from unittest.mock import Mock

import pytest
from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import HTTPError

from kg_agent.exceptions import ObjectStorageError
from kg_agent.infrastructure.object_storage.minio import MinioObjectStorage


def make_storage():
    """创建注入 SDK 替身的适配器，保证测试不依赖网络或真实凭据。"""

    storage = MinioObjectStorage(
        endpoint="localhost:9000",
        access_key="test-key",
        secret_key="test-secret",
        bucket="test-artifacts",
        secure=False,
    )
    client = Mock(spec=Minio)
    storage._client = client
    return storage, client


def missing_object():
    """构造 SDK 的对象不存在异常。"""

    return S3Error(None, "NoSuchKey", "missing", None, None, None)


@pytest.mark.asyncio
async def test_minio_write_uses_fixed_bucket_and_bytes() -> None:
    """上传只使用配置桶，并把字节与介质类型传递给 SDK。"""

    storage, client = make_storage()
    client.stat_object.side_effect = missing_object()

    uri = await storage.put_bytes("tasks/test/content.bin", b"data", "application/pdf")

    assert uri == "minio://test-artifacts/tasks/test/content.bin"
    args = client.put_object.call_args
    assert args.args[0:2] == ("test-artifacts", "tasks/test/content.bin")
    assert args.args[2].read() == b"data"
    assert args.args[3] == 4
    assert args.kwargs["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_minio_read_releases_http_response() -> None:
    """读取成功后必须归还 HTTP 连接。"""

    storage, client = make_storage()
    response = Mock()
    response.read.return_value = b"content"
    client.get_object.return_value = response

    assert await storage.get_bytes("tasks/test/content.bin") == b"content"
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


@pytest.mark.asyncio
async def test_minio_read_failure_still_releases_response() -> None:
    """读取失败时也不能泄漏 HTTP 连接，错误转换为统一业务异常。"""

    storage, client = make_storage()
    response = Mock()
    response.read.side_effect = HTTPError("connection interrupted")
    client.get_object.return_value = response

    with pytest.raises(ObjectStorageError):
        await storage.get_bytes("tasks/test/content.bin")
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


@pytest.mark.asyncio
async def test_minio_existing_content_is_idempotent_and_not_overwritten() -> None:
    """已存在内容可幂等读取，但不同内容不能覆盖。"""

    storage, client = make_storage()
    response = Mock()
    response.read.return_value = b"original"
    client.get_object.return_value = response

    await storage.put_bytes("key", b"original", "application/pdf")
    with pytest.raises(ObjectStorageError):
        await storage.put_bytes("key", b"changed", "application/pdf")
    client.put_object.assert_not_called()


@pytest.mark.asyncio
async def test_minio_bucket_creation_and_delete() -> None:
    """桶初始化和失败补偿删除只针对配置桶。"""

    storage, client = make_storage()
    client.bucket_exists.return_value = False
    await storage.ensure_bucket()
    await storage.delete("tasks/test/orphan.bin")

    client.make_bucket.assert_called_once_with("test-artifacts")
    client.remove_object.assert_called_once_with("test-artifacts", "tasks/test/orphan.bin")


@pytest.mark.asyncio
async def test_minio_transport_error_is_not_reported_as_missing_object() -> None:
    """网络错误必须上抛，不能误判为对象不存在并继续写入。"""

    storage, client = make_storage()
    client.stat_object.side_effect = HTTPError("unreachable")

    with pytest.raises(ObjectStorageError):
        await storage.exists("key")
