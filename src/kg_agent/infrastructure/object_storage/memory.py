"""实现测试使用的确定性内存对象存储。

该适配器不用于生产，也不模拟网络、权限或 MinIO 一致性语义。
"""

from kg_agent.exceptions import ObjectStorageError


class InMemoryObjectStorage:
    """在进程内保存对象字节，并拒绝同一键被不同内容覆盖。"""

    def __init__(self, bucket: str = "test-artifacts") -> None:
        """创建指定逻辑桶的空内存存储。"""

        self._bucket = bucket
        self._objects: dict[str, tuple[bytes, str]] = {}

    @property
    def bucket(self) -> str:
        """返回逻辑桶名。"""

        return self._bucket

    async def ensure_bucket(self) -> None:
        """内存 Bucket 在构造时即存在，因此启动检查无需执行操作。"""

    async def put_bytes(self, object_key: str, content: bytes, media_type: str) -> str:
        """幂等写入对象；相同键不同内容违反 Artifact 不可变规则。"""

        current = self._objects.get(object_key)
        if current is not None and current != (content, media_type):
            raise ObjectStorageError("对象键已存在且内容不同，禁止覆盖")
        self._objects[object_key] = (bytes(content), media_type)
        return f"memory://{self._bucket}/{object_key}"

    async def get_bytes(self, object_key: str) -> bytes:
        """返回对象字节；对象不存在时抛出统一异常。"""

        try:
            return self._objects[object_key][0]
        except KeyError as exc:
            raise ObjectStorageError("对象不存在") from exc

    async def exists(self, object_key: str) -> bool:
        """判断对象键是否已经存在。"""

        return object_key in self._objects

    async def delete(self, object_key: str) -> None:
        """删除本次失败写入的对象；键不存在时保持幂等。"""

        self._objects.pop(object_key, None)
