"""定义领域服务使用的对象存储协议和存储结果。

上层只依赖该协议，不直接依赖 MinIO SDK，从而可以在单元测试中使用内存适配器。
"""

from typing import Protocol


class ObjectStorage(Protocol):
    """约束不可变 Artifact 内容所需的最小对象存储能力。"""

    @property
    def bucket(self) -> str:
        """返回受控存储桶名称。"""
        ...

    async def ensure_bucket(self) -> None:
        """确保当前逻辑 Bucket 已可写；本地或内存实现可以是轻量操作。"""
        ...

    async def put_bytes(self, object_key: str, content: bytes, media_type: str) -> str:
        """写入完整字节并返回不含临时凭证的受控 URI。"""
        ...

    async def get_bytes(self, object_key: str) -> bytes:
        """读取对象完整内容。"""
        ...

    async def exists(self, object_key: str) -> bool:
        """判断对象是否存在。"""
        ...

    async def delete(self, object_key: str) -> None:
        """仅供数据库事务失败补偿时删除本次新建对象。"""
        ...
