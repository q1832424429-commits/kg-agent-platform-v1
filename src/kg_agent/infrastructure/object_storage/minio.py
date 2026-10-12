"""实现 MinIO 对象存储适配器。

MinIO 官方客户端是同步接口，本模块通过 asyncio.to_thread 隔离阻塞调用；业务层看不到
访问密钥，也不能自行拼接桶外路径。
"""

from __future__ import annotations

import asyncio
from io import BytesIO

from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import HTTPError

from kg_agent.exceptions import ObjectStorageError


class MinioObjectStorage:
    """使用固定 Bucket 保存 Artifact 内容。"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
    ) -> None:
        """创建 MinIO 客户端；不会在构造时发起网络请求。"""

        self._bucket = bucket
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    @property
    def bucket(self) -> str:
        """返回受控 Bucket 名称。"""

        return self._bucket

    async def ensure_bucket(self) -> None:
        """确保 Bucket 存在；仅应用启动阶段调用。"""

        try:
            exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
            if not exists:
                await asyncio.to_thread(self._client.make_bucket, self._bucket)
        except (S3Error, HTTPError, OSError) as exc:
            raise ObjectStorageError("无法检查或创建 MinIO Bucket") from exc

    async def put_bytes(self, object_key: str, content: bytes, media_type: str) -> str:
        """幂等上传内容，并拒绝同一对象键覆盖不同字节。"""

        if await self.exists(object_key):
            existing = await self.get_bytes(object_key)
            if existing != content:
                raise ObjectStorageError("MinIO 对象键已存在且内容不同，禁止覆盖")
            return f"minio://{self._bucket}/{object_key}"
        try:
            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                object_key,
                BytesIO(content),
                len(content),
                content_type=media_type,
            )
        except (S3Error, HTTPError, OSError) as exc:
            raise ObjectStorageError("MinIO 对象写入失败") from exc
        return f"minio://{self._bucket}/{object_key}"

    async def get_bytes(self, object_key: str) -> bytes:
        """读取对象并确保底层 HTTP 响应被关闭。"""

        try:
            response = await asyncio.to_thread(self._client.get_object, self._bucket, object_key)
            try:
                return await asyncio.to_thread(response.read)
            finally:
                response.close()
                response.release_conn()
        except (S3Error, HTTPError, OSError) as exc:
            raise ObjectStorageError("MinIO 对象读取失败") from exc

    async def exists(self, object_key: str) -> bool:
        """通过 stat_object 判断对象是否存在。"""

        try:
            await asyncio.to_thread(self._client.stat_object, self._bucket, object_key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise ObjectStorageError("MinIO 对象状态检查失败") from exc
        except (HTTPError, OSError) as exc:
            raise ObjectStorageError("MinIO 对象状态检查失败") from exc

    async def delete(self, object_key: str) -> None:
        """删除事务失败时刚写入的对象。"""

        try:
            await asyncio.to_thread(self._client.remove_object, self._bucket, object_key)
        except (S3Error, HTTPError, OSError) as exc:
            raise ObjectStorageError("MinIO 对象补偿删除失败") from exc
