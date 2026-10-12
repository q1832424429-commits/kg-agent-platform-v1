"""导出对象存储协议及 V1 的内存、MinIO 适配器。"""

from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.infrastructure.object_storage.local_file import LocalFileObjectStorage
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.infrastructure.object_storage.minio import MinioObjectStorage

__all__ = [
    "InMemoryObjectStorage",
    "LocalFileObjectStorage",
    "MinioObjectStorage",
    "ObjectStorage",
]
