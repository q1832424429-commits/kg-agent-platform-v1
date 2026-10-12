"""实现仅供本机 uv 联调使用的持久化文件对象存储。

该适配器保持对象键不可变、路径受控和原子写入语义；正式部署仍使用 MinIO。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath

from kg_agent.exceptions import ObjectStorageError


class LocalFileObjectStorage:
    """把 Artifact 内容持久化到指定本地根目录，不允许对象逃逸或原地覆盖。"""

    def __init__(self, root: Path, *, bucket: str) -> None:
        """绑定本机数据根目录和逻辑 Bucket 名称，构造时不创建文件。"""

        self._root = root.resolve() / bucket
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        """返回用于生成受控 URI 的逻辑 Bucket 名称。"""

        return self._bucket

    async def ensure_bucket(self) -> None:
        """创建受控根目录；只在本机应用启动阶段调用。"""

        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, object_key: str) -> Path:
        """解析并验证对象键，拒绝绝对路径、目录穿越和根目录逃逸。"""

        key = PurePosixPath(object_key)
        if not object_key or key.is_absolute() or ".." in key.parts or key.name in {"", "."}:
            raise ObjectStorageError("本地对象键必须是受控相对 POSIX 路径")
        target = self._root.joinpath(*key.parts).resolve(strict=False)
        if not target.is_relative_to(self._root):
            raise ObjectStorageError("本地对象键超出受控存储目录")
        return target

    async def put_bytes(self, object_key: str, content: bytes, media_type: str) -> str:
        """以临时文件加硬链接原子写入；已存在的不同内容一律拒绝覆盖。"""

        del media_type  # 内容类型由 PostgreSQL/SQLite Artifact 元数据保存。
        target = self._path_for(object_key)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != content:
                    raise ObjectStorageError("本地对象键已存在且内容不同，禁止覆盖")
                return
            descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError as exc:
                    if target.read_bytes() != content:
                        raise ObjectStorageError("并发写入了不同内容，禁止覆盖") from exc
            finally:
                temporary.unlink(missing_ok=True)

        write()
        return f"local-artifact://{self._bucket}/{object_key}"

    async def get_bytes(self, object_key: str) -> bytes:
        """读取完整对象；不存在或读取失败时转换为统一存储异常。"""

        try:
            return self._path_for(object_key).read_bytes()
        except (OSError, ObjectStorageError) as exc:
            if isinstance(exc, ObjectStorageError):
                raise
            raise ObjectStorageError("本地 Artifact 对象不存在或无法读取") from exc

    async def exists(self, object_key: str) -> bool:
        """判断受控对象是否存在且为普通文件。"""

        path = self._path_for(object_key)
        return path.is_file()

    async def delete(self, object_key: str) -> None:
        """幂等删除本次事务失败时新写入的受控对象。"""

        path = self._path_for(object_key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ObjectStorageError("无法删除本地 Artifact 对象") from exc
