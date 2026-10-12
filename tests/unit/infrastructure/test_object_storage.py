"""验证内存对象存储遵守不可变写入和幂等删除语义。"""

import pytest

from kg_agent.exceptions import ObjectStorageError
from kg_agent.infrastructure.object_storage.local_file import LocalFileObjectStorage
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage


@pytest.mark.asyncio
async def test_memory_storage_is_idempotent_but_never_overwrites() -> None:
    """相同内容可重试写入，同一键的不同内容必须被拒绝。"""

    storage = InMemoryObjectStorage()
    uri = await storage.put_bytes("task/a", b"same", "application/octet-stream")
    second_uri = await storage.put_bytes("task/a", b"same", "application/octet-stream")

    assert uri == second_uri
    assert await storage.get_bytes("task/a") == b"same"
    with pytest.raises(ObjectStorageError):
        await storage.put_bytes("task/a", b"different", "application/octet-stream")


@pytest.mark.asyncio
async def test_memory_storage_delete_is_idempotent() -> None:
    """事务补偿删除可以安全重试。"""

    storage = InMemoryObjectStorage()
    await storage.put_bytes("task/a", b"value", "application/octet-stream")
    await storage.delete("task/a")
    await storage.delete("task/a")

    assert not await storage.exists("task/a")


@pytest.mark.asyncio
async def test_local_file_storage_persists_and_rejects_overwrite(tmp_path) -> None:
    """本机 uv 存储应跨实例读取同一对象，并拒绝不同内容覆盖不可变键。"""

    storage = LocalFileObjectStorage(tmp_path, bucket="local-tests")
    await storage.ensure_bucket()
    uri = await storage.put_bytes("objects/value.json", b'{"value":1}', "application/json")
    assert uri == "local-artifact://local-tests/objects/value.json"
    restored = LocalFileObjectStorage(tmp_path, bucket="local-tests")
    assert await restored.get_bytes("objects/value.json") == b'{"value":1}'
    with pytest.raises(ObjectStorageError, match="禁止覆盖"):
        await restored.put_bytes("objects/value.json", b'{"value":2}', "application/json")


@pytest.mark.asyncio
async def test_local_file_storage_rejects_path_escape(tmp_path) -> None:
    """本机 uv 存储不得允许 Artifact 对象键逃逸受控数据目录。"""

    storage = LocalFileObjectStorage(tmp_path, bucket="local-tests")
    with pytest.raises(ObjectStorageError, match="受控相对"):
        await storage.put_bytes("../secret", b"x", "application/octet-stream")
