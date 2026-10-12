"""实现长期记忆写入、召回和 SharedContext 引用转换。"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from time import perf_counter
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.memory_models import (
    CompletedTaskExperience,
    LongTermMemoryRecord,
    MemoryConsolidationResult,
    MemoryMessage,
    MemoryRef,
    MemoryWriteReceipt,
)
from kg_agent.exceptions import MemoryProviderError
from kg_agent.memory.base import LongTermMemoryStore

LOGGER = logging.getLogger(__name__)

_INACTIVE_MEMORY_STATUSES = {"SUPERSEDED", "INACTIVE", "DELETED", "REJECTED"}
_NEAR_DUPLICATE_TEXT_RATIO = 0.96


class LongTermMemoryService:
    """在单用户 V1 中统一管理 Mem0 命名空间、写入策略和降级行为。"""

    def __init__(
        self,
        store: LongTermMemoryStore,
        *,
        user_id: str,
        app_id: str = "kg-agent-platform-production",
        default_top_k: int = 3,
        candidate_top_k: int = 12,
        default_threshold: float | None = 0.5,
        dedup_threshold: float = 0.92,
        merge_threshold: float = 0.8,
        default_rerank: bool = True,
    ) -> None:
        """绑定供应商适配器和服务端控制的用户标识。"""

        if not user_id.strip():
            raise ValueError("长期记忆 user_id 不能为空")
        if not app_id.strip():
            raise ValueError("长期记忆 app_id 不能为空")
        if default_top_k <= 0:
            raise ValueError("长期记忆 default_top_k 必须大于 0")
        if candidate_top_k < default_top_k:
            raise ValueError("长期记忆候选数量不能小于最终召回数量")
        if not 0 <= dedup_threshold <= 1:
            raise ValueError("长期记忆去重阈值必须在 0 到 1 之间")
        if not 0 <= merge_threshold < dedup_threshold:
            raise ValueError("长期记忆合并阈值必须在 0 到去重阈值之间")
        self._store = store
        self._user_id = user_id
        self._app_id = app_id
        self._top_k = default_top_k
        self._candidate_top_k = candidate_top_k
        self._threshold = default_threshold
        self._dedup_threshold = dedup_threshold
        self._merge_threshold = merge_threshold
        self._rerank = default_rerank

    @property
    def provider_name(self) -> str:
        """返回当前供应商名称，供 API 和日志观测。"""

        return self._store.provider_name

    @property
    def enabled(self) -> bool:
        """返回长期记忆读写能力是否启用。"""

        return self._store.enabled

    @property
    def app_id(self) -> str:
        """返回不含凭据的环境隔离标识，供测试端展示和审计。"""

        return self._app_id

    async def create_authorized_memory(
        self,
        *,
        text: str,
        source_refs: list[str],
        infer: bool,
    ) -> MemoryWriteReceipt:
        """保存用户明确授权的长期偏好、术语或本体选择。"""

        return await self._add_if_novel(
            text=text,
            messages=[MemoryMessage(role="user", content=text)],
            metadata={"source": "user_authorized", "source_refs": "|".join(source_refs)},
            infer=infer,
        )

    async def remember_agent_result(self, result: AgentRunResult) -> list[MemoryWriteReceipt]:
        """兼容旧调用方，但不再把单个专业 Agent 结果写入长期记忆。

        长期经验现在只在整个用户目标完成后聚合写入。保留本方法是为了避免旧 Worker
        或扩展直接报错；它始终返回空列表，不能形成零散 ``agent_result`` 记忆。
        """
        LOGGER.info(
            "跳过单 Agent 长期经验写入，等待任务完成后聚合: task_id=%s run_id=%s status=%s",
            getattr(result, "task_id", None),
            getattr(result, "run_id", None),
            getattr(result, "status", None),
        )
        return []

    async def remember_completed_task_experience(
        self,
        experience: CompletedTaskExperience,
    ) -> MemoryConsolidationResult:
        """按 0.92 去重、0.80 合并的三段策略保存完整成功经验。

        规则由代码确定执行：相似度大于等于去重阈值时不写入；处于合并区间时把
        已有经验与本轮新增事实去重合并后写入；低于合并阈值时创建独立经验。
        Mem0 只负责语义检索和正文存储，不负责改变这三个业务分支。
        """

        if not self.enabled:
            receipt = await self._store.add(
                user_id=self._user_id,
                messages=[MemoryMessage(role="assistant", content=experience.summary)],
                metadata={
                    "source": "completed_task_experience",
                    "experience_key": experience.experience_key,
                    "task_id": str(experience.task_id),
                },
                infer=True,
                run_id=experience.experience_key,
                app_id=self._app_id,
            )
            return MemoryConsolidationResult(action="DISABLED", receipt=receipt)

        candidates = await self._store.search(
            user_id=self._user_id,
            query=experience.summary,
            top_k=self._candidate_top_k,
            threshold=self._merge_threshold,
            rerank=True,
            app_id=self._app_id,
        )
        comparable = [
            item
            for item in candidates
            if self._is_active(item) and item.metadata.get("source") == "completed_task_experience"
        ]
        best = max(comparable, key=lambda item: item.score or 0.0, default=None)
        similarity = best.score if best is not None else None

        if best is not None and (
            self._normalize_text(best.memory) == self._normalize_text(experience.summary)
            or (similarity is not None and similarity >= self._dedup_threshold)
        ):
            return MemoryConsolidationResult(
                action="SKIPPED_DUPLICATE",
                receipt=MemoryWriteReceipt(
                    status="SKIPPED_DUPLICATE",
                    message="已存在语义相似度不低于 0.92 的完整任务经验",
                ),
                similarity=similarity,
                matched_provider_memory_id=best.provider_memory_id,
            )

        action: Literal["CREATED", "MERGED"] = "CREATED"
        content = experience.summary
        matched_id: str | None = None
        if best is not None and similarity is not None and similarity >= self._merge_threshold:
            action = "MERGED"
            content = self._merge_experience_text(best.memory, experience.summary)
            matched_id = best.provider_memory_id

        metadata: dict[str, str | int | float | bool | None] = {
            "source": "completed_task_experience",
            "experience_key": experience.experience_key,
            "task_id": str(experience.task_id),
            "task_spec_revision": experience.task_spec_revision,
            "decomposition_revision": experience.decomposition_revision,
            "source_refs": "|".join(experience.source_refs),
            "consolidation_action": action,
            "merged_from_memory_id": matched_id,
            "matched_similarity": similarity,
        }
        if action == "MERGED" and matched_id is not None:
            # Mem0 提供按 memory_id 原位更新能力。这里更新命中的旧经验，而不是
            # 追加一条“旧正文 + 新正文”，从物理层避免两条近似记录继续参与召回。
            receipt = await self._store.update(
                provider_memory_id=matched_id,
                text=content,
                metadata={**best.metadata, **metadata} if best is not None else metadata,
            )
        else:
            receipt = await self._store.add(
                user_id=self._user_id,
                messages=[MemoryMessage(role="assistant", content=content)],
                metadata=metadata,
                # 完整任务经验已由确定性服务聚合，保持单条原文以获得稳定 memory ID。
                infer=False,
                run_id=experience.experience_key,
                app_id=self._app_id,
            )
        return MemoryConsolidationResult(
            action=action,
            receipt=receipt,
            similarity=similarity,
            matched_provider_memory_id=matched_id,
        )

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
        rerank: bool | None = None,
    ) -> list[LongTermMemoryRecord]:
        """在当前单用户命名空间内执行可观察的直接检索。"""

        return await self._store.search(
            user_id=self._user_id,
            query=query,
            top_k=top_k or self._top_k,
            threshold=self._threshold if threshold is None else threshold,
            rerank=self._rerank if rerank is None else rerank,
            app_id=self._app_id,
        )

    async def recall_refs(self, query: str) -> list[MemoryRef]:
        """召回较大候选集，完成失效过滤、排序和去重后返回少量轻量引用。

        时间戳仅参与服务端内部的同分排序，不写入 MemoryRef，也不直接进入 Agent
        Prompt。启用供应商重排时保留供应商返回顺序，避免再次按向量分数破坏重排。
        """

        started = perf_counter()
        records = await self._store.search(
            user_id=self._user_id,
            query=query,
            top_k=self._candidate_top_k,
            threshold=self._threshold,
            rerank=self._rerank,
            app_id=self._app_id,
        )
        active = [
            item
            for item in records
            if self._is_active(item)
            # 旧版本曾把每个专业 Agent 和每轮提交分别写入 Mem0；这些碎片继续
            # 保留在供应商侧供审计，但不再自动注入 Agent 上下文。
            and item.metadata.get("source") not in {"agent_result", "task_turn"}
        ]
        if not self._rerank:
            active.sort(key=self._fallback_sort_key, reverse=True)
        unique = self._deduplicate_records(active)
        final = unique[: self._top_k]
        LOGGER.info(
            "长期记忆召回完成: provider=%s candidates=%d active=%d unique=%d final=%d "
            "rerank=%s duration_ms=%.3f",
            self.provider_name,
            len(records),
            len(active),
            len(unique),
            len(final),
            self._rerank,
            (perf_counter() - started) * 1000,
        )
        return [self._to_ref(item) for item in final]

    async def safe_recall_refs(self, query: str) -> list[MemoryRef]:
        """召回失败时记录不含敏感信息的告警，并允许核心任务继续。"""

        try:
            return await self.recall_refs(query)
        except MemoryProviderError:
            LOGGER.warning("Mem0 长期记忆召回失败，本轮按无长期记忆继续", exc_info=True)
            return []

    async def safe_remember_agent_result(self, result: AgentRunResult) -> None:
        """以非阻断方式保存专业 Agent 经验，避免云故障破坏 Assignment 提交。"""

        try:
            await self.remember_agent_result(result)
        except MemoryProviderError:
            LOGGER.warning("Mem0 Agent 经验写入失败，Assignment 结果不回滚", exc_info=True)

    async def safe_remember_completed_task_experience(
        self,
        experience: CompletedTaskExperience,
    ) -> MemoryConsolidationResult | None:
        """非阻断保存完整任务经验；Mem0 故障不得回滚已成功的 MasterTask。"""

        try:
            return await self.remember_completed_task_experience(experience)
        except MemoryProviderError:
            LOGGER.warning("Mem0 完整任务经验写入失败，任务结果不回滚", exc_info=True)
            return None

    async def delete_unmerged_task_experiences(self, task_ids: list[str]) -> list[str]:
        """删除指定任务独立创建的经验，合并到其他经验的记录绝不删除。

        ``MERGED`` 表示新任务事实已经写入另一条共享记忆，删除它会破坏仍存会话的
        经验；只有 Mem0 元数据精确标记为本任务的 ``CREATED`` 经验才会删除。
        """

        if not self.enabled:
            return []
        deleted: list[str] = []
        for task_id in task_ids:
            records = await self._store.search(
                user_id=self._user_id,
                query=f"task:{task_id}",
                top_k=self._candidate_top_k,
                threshold=None,
                rerank=False,
                app_id=self._app_id,
            )
            for record in records:
                metadata = record.metadata
                if (
                    metadata.get("source") != "completed_task_experience"
                    or str(metadata.get("task_id")) != task_id
                    or metadata.get("consolidation_action") != "CREATED"
                ):
                    continue
                receipt = await self._store.delete(provider_memory_id=record.provider_memory_id)
                if receipt.status == "DELETED":
                    deleted.append(record.provider_memory_id)
        return deleted

    async def delete_provider_memory(self, provider_memory_id: str) -> MemoryWriteReceipt:
        """删除已由 PostgreSQL 回执精确标识的独立长期记忆。"""

        if not self.enabled:
            return MemoryWriteReceipt(status="DISABLED", message="长期记忆未启用")
        return await self._store.delete(provider_memory_id=provider_memory_id)

    async def _add_if_novel(
        self,
        *,
        text: str,
        messages: list[MemoryMessage],
        metadata: dict[str, str | int | float | bool | None],
        infer: bool,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> MemoryWriteReceipt:
        """写入前用高阈值语义检索拦截重复项；预检失败不阻断正式写入。"""

        if self.enabled:
            try:
                candidates = await self._store.search(
                    user_id=self._user_id,
                    query=text,
                    top_k=min(5, self._candidate_top_k),
                    threshold=self._dedup_threshold,
                    rerank=True,
                    app_id=self._app_id,
                )
                if any(self._is_semantic_duplicate(text, item) for item in candidates):
                    LOGGER.info(
                        "跳过重复长期记忆写入: source=%s candidate_count=%d",
                        metadata.get("source", "unknown"),
                        len(candidates),
                    )
                    return MemoryWriteReceipt(
                        status="SKIPPED_DUPLICATE",
                        message="已存在高度相似的有效长期记忆",
                    )
            except MemoryProviderError:
                # 去重是质量增强，不应因预检暂时不可用而丢失用户已授权的记忆。
                LOGGER.warning("Mem0 写入前去重预检失败，本次继续写入", exc_info=True)

        return await self._store.add(
            user_id=self._user_id,
            messages=messages,
            metadata=metadata,
            infer=infer,
            agent_id=agent_id,
            run_id=run_id,
            app_id=self._app_id,
        )

    @classmethod
    def _deduplicate_records(
        cls, records: list[LongTermMemoryRecord]
    ) -> list[LongTermMemoryRecord]:
        """按标准化文本折叠完全重复和近似重复召回结果，并保留当前排序首项。"""

        unique: list[LongTermMemoryRecord] = []
        normalized: list[str] = []
        for record in records:
            current = cls._normalize_text(record.memory)
            if any(
                current == previous
                or SequenceMatcher(None, current, previous).ratio() >= _NEAR_DUPLICATE_TEXT_RATIO
                for previous in normalized
            ):
                continue
            unique.append(record)
            normalized.append(current)
        return unique

    def _is_semantic_duplicate(self, text: str, record: LongTermMemoryRecord) -> bool:
        """判断高相关候选是否足以阻止产生一条重复长期记忆。"""

        if not self._is_active(record):
            return False
        left = self._normalize_text(text)
        right = self._normalize_text(record.memory)
        if left == right:
            return True
        if SequenceMatcher(None, left, right).ratio() >= _NEAR_DUPLICATE_TEXT_RATIO:
            return True
        return record.score is not None and record.score >= self._dedup_threshold

    @staticmethod
    def _is_active(record: LongTermMemoryRecord) -> bool:
        """过滤已经被供应商或平台标记为失效、替代、删除或拒绝的记忆。"""

        status = str(record.metadata.get("status", "ACTIVE")).strip().upper()
        return status not in _INACTIVE_MEMORY_STATUSES

    @staticmethod
    def _fallback_sort_key(record: LongTermMemoryRecord) -> tuple[float, float]:
        """未启用重排时按相关度优先、最近更新时间次优进行确定性排序。"""

        timestamp: datetime | None = record.updated_at or record.created_at
        return record.score or 0.0, timestamp.timestamp() if timestamp is not None else 0.0

    @staticmethod
    def _normalize_text(value: str) -> str:
        """统一角色前缀、大小写、Unicode、空白与标点，供去重比较使用。"""

        normalized = unicodedata.normalize("NFKC", value).strip().lower()
        normalized = re.sub(r"^(user|assistant|system)\s*:\s*", "", normalized)
        normalized = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
        return normalized

    @classmethod
    def _merge_experience_text(cls, existing: str, current: str) -> str:
        """按行合并两条公开经验并去除重复事实，避免简单字符串无限叠加。"""

        merged: list[str] = []
        normalized: set[str] = set()
        for line in [*existing.splitlines(), *current.splitlines()]:
            clean = line.strip()
            key = cls._normalize_text(clean)
            if not clean or not key or key in normalized:
                continue
            normalized.add(key)
            merged.append(clean)
        return "\n".join(merged)

    @staticmethod
    def _to_ref(record: LongTermMemoryRecord) -> MemoryRef:
        """使用供应商 ID 生成稳定 UUID，并保留可审计来源与相关度。"""

        refs = [f"mem0://memory/{record.provider_memory_id}"]
        if record.score is not None:
            refs.append(f"score:{record.score:.6f}")
        return MemoryRef(
            memory_id=uuid5(NAMESPACE_URL, f"mem0:{record.provider_memory_id}"),
            memory_type="LONG_TERM",
            summary=record.memory,
            source_refs=refs,
        )
