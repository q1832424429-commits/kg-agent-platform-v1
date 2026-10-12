"""保存任务级公开 JSONL 日志及受控的 Skill 原始模型回答。

JSONL 只保存公开结构化结论和原始回答引用，不记录模型隐藏思维、API Key、完整输入
文档正文或 Mem0 候选池/原始响应。已经通过过滤并实际提供给 Agent 的最多三条长期记忆
摘要允许作为公开召回证据记录。Skill 原始结构化回答写入独立受限文件；PostgreSQL 与
Artifact 存储仍是业务事实来源，这些文件仅用于开发联调和数据流排障。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID


class RunLogService:
    """以单次原子追加方式维护每个 MasterTask 的公开 JSONL 诊断日志。"""

    def __init__(self, directory: Path | None) -> None:
        """绑定日志目录；传入 ``None`` 时显式禁用文件日志。"""

        self._directory = directory

    @property
    def enabled(self) -> bool:
        """返回当前是否启用了任务级文件日志。"""

        return self._directory is not None

    def path_for(self, task_id: UUID) -> Path | None:
        """返回安全的任务日志路径，不接受用户提供的文件名。"""

        if self._directory is None:
            return None
        return self._directory / f"task-{task_id}.jsonl"

    def model_output_path_for(self, task_id: UUID, call_id: str) -> Path | None:
        """返回 Skill 原始模型回答的受控路径，拒绝可造成目录穿越的调用编号。"""

        if self._directory is None:
            return None
        if re.fullmatch(r"[A-Za-z0-9-]{1,128}", call_id) is None:
            raise ValueError("模型调用编号包含非法字符")
        return self._directory / "model-outputs" / f"task-{task_id}" / f"{call_id}.json"

    async def save_model_output(
        self,
        *,
        task_id: UUID,
        call_id: str,
        content: str,
    ) -> tuple[str, str] | None:
        """完整保存一次 Skill 模型回答，并返回文件路径和 SHA-256 校验和。

        原始回答不写入 SSE 使用的 JSONL，避免大响应阻塞实时进度；JSONL 只保存本文件
        的位置、字符数和校验和。文件采用受限权限并以内部 call_id 命名。
        """

        path = self.model_output_path_for(task_id, call_id)
        if path is None:
            return None
        encoded = content.encode("utf-8")
        await asyncio.to_thread(self._write_private_bytes, path, encoded)
        checksum = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        return str(path), checksum

    async def append(
        self,
        *,
        task_id: UUID,
        record_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        """追加一条公开结构化记录；文件写入失败会向调用方明确抛出异常。"""

        path = self.path_for(task_id)
        if path is None:
            return
        record = {
            "logged_at": datetime.now(UTC).isoformat(),
            "task_id": str(task_id),
            "record_type": record_type,
            "actor": actor,
            "payload": payload,
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        await asyncio.to_thread(self._append_bytes, path, encoded)

    async def read(self, task_id: UUID) -> bytes | None:
        """读取一个任务的完整 JSONL；未启用或文件不存在时返回 ``None``。"""

        path = self.path_for(task_id)
        if path is None or not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def delete_task_files(self, task_id: UUID) -> None:
        """删除一个已删除会话所拥有的 JSONL 与模型原始回答目录。

        路径完全由任务 UUID 和受控日志根目录推导，不接受客户端路径，避免删除请求
        被用于访问或清理任意宿主文件。
        """

        if self._directory is None:
            return
        task_log = self.path_for(task_id)
        model_dir = self._directory / "model-outputs" / f"task-{task_id}"
        await asyncio.to_thread(self._delete_task_paths, task_log, model_dir)

    async def count(self, task_id: UUID) -> int:
        """返回任务日志的物理行数，供 SSE 从当前文件末尾开始追踪。

        行号同时作为测试端 SSE 的稳定事件游标。即使某一行 JSON 损坏，游标也会
        继续前进，避免客户端断线重连后反复读取同一条坏记录。
        """

        content = await self.read(task_id)
        return len(content.splitlines()) if content is not None else 0

    async def read_after(
        self,
        task_id: UUID,
        *,
        after: int = 0,
    ) -> tuple[list[tuple[int, dict[str, Any]]], int]:
        """读取指定物理行号之后的合法公开日志。

        参数：
            task_id: 需要追踪的 MasterTask ID。
            after: 客户端已经消费的最后一条物理行号，最小为零。

        返回：
            ``(记录列表, 最新物理行号)``。记录列表中的元组由行号和 JSON 对象组成。
        """

        content = await self.read(task_id)
        if content is None:
            return [], 0
        lines = content.splitlines()
        cursor = min(max(after, 0), len(lines))
        records: list[tuple[int, dict[str, Any]]] = []
        for line_number, line in enumerate(lines[cursor:], start=cursor + 1):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                records.append((line_number, value))
        return records, len(lines)

    @staticmethod
    def _append_bytes(path: Path, content: bytes) -> None:
        """创建目录并使用 O_APPEND 单次写入，降低多进程日志相互覆盖风险。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            os.write(descriptor, content)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_private_bytes(path: Path, content: bytes) -> None:
        """原子替换受控原始回答文件，并限制为服务用户可读写。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, content)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)

    @staticmethod
    def _delete_task_paths(task_log: Path | None, model_dir: Path) -> None:
        """执行受控物理删除；仅接受由 ``delete_task_files`` 构造的路径。"""

        if task_log is not None:
            task_log.unlink(missing_ok=True)
        if model_dir.is_dir():
            shutil.rmtree(model_dir)
