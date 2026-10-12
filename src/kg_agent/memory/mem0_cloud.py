"""使用 Mem0 Platform REST API 实现长期记忆云适配器。

新增与搜索采用 V3 端点；按 ID 原位更新采用平台当前公开的 V1 Update 端点。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from kg_agent.domain.memory_models import (
    LongTermMemoryRecord,
    MemoryMessage,
    MemoryWriteReceipt,
)
from kg_agent.exceptions import MemoryProviderError


class Mem0CloudMemoryStore:
    """通过服务端 API Key 调用 Mem0 Cloud 的写入和语义搜索接口。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.mem0.ai",
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """保存不对外暴露的凭据和 HTTP 客户端配置。"""

        if not api_key.strip():
            raise ValueError("Mem0 API Key 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("Mem0 请求超时时间必须大于 0")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def provider_name(self) -> str:
        """返回长期记忆供应商名称。"""

        return "mem0-cloud"

    @property
    def enabled(self) -> bool:
        """Mem0 适配器始终表示真实云能力已启用。"""

        return True

    async def add(
        self,
        *,
        user_id: str,
        messages: list[MemoryMessage],
        metadata: dict[str, str | int | float | bool | None],
        infer: bool,
        agent_id: str | None = None,
        run_id: str | None = None,
        app_id: str | None = None,
    ) -> MemoryWriteReceipt:
        """调用 V3 add API；Mem0 的异步处理状态通过事件回执返回。"""

        payload: dict[str, Any] = {
            "user_id": user_id,
            "messages": [item.model_dump(mode="json") for item in messages],
            "metadata": metadata,
            "infer": infer,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        if run_id:
            payload["run_id"] = run_id
        if app_id:
            payload["app_id"] = app_id
        value = await self._request("POST", "/v3/memories/add/", payload)
        # V3 当前返回 results[]，其中 id 是已创建的 memory ID；旧版事件回执仍
        # 允许按原契约解析。仅在恰好生成一条记忆时记录可用于精确删除的 ID。
        results = value.get("results")
        if isinstance(results, list):
            ids = [str(item["id"]) for item in results if isinstance(item, dict) and item.get("id")]
            return MemoryWriteReceipt(
                event_id=ids[0] if len(ids) == 1 else None,
                status="SUCCEEDED",
                message=f"Mem0 已生成 {len(ids)} 条记忆",
            )
        try:
            return MemoryWriteReceipt.model_validate(value)
        except ValidationError as exc:
            raise MemoryProviderError("Mem0 写入回执不符合预期格式") from exc

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int,
        threshold: float | None,
        rerank: bool,
        app_id: str | None = None,
    ) -> list[LongTermMemoryRecord]:
        """调用 V3 search API，并把供应商响应转换为平台领域模型。"""

        filters: dict[str, Any] = {"user_id": user_id}
        if app_id:
            # 测试端和正式端必须同时按用户与应用命名空间过滤，避免跨环境召回。
            filters = {"AND": [{"user_id": user_id}, {"app_id": app_id}]}
        payload: dict[str, Any] = {
            "query": query,
            "filters": filters,
            "top_k": top_k,
            "rerank": rerank,
        }
        if threshold is not None:
            payload["threshold"] = threshold
        value = await self._request("POST", "/v3/memories/search/", payload)
        raw_results = value.get("results") if isinstance(value, dict) else None
        if not isinstance(raw_results, list):
            raise MemoryProviderError("Mem0 搜索响应缺少 results 数组")
        records: list[LongTermMemoryRecord] = []
        try:
            for item in raw_results:
                if not isinstance(item, dict):
                    raise MemoryProviderError("Mem0 搜索结果条目不是对象")
                records.append(
                    LongTermMemoryRecord(
                        provider_memory_id=str(item["id"]),
                        memory=str(item["memory"]),
                        score=item.get("score"),
                        metadata=item.get("metadata") or {},
                        categories=item.get("categories") or [],
                        created_at=item.get("created_at"),
                        updated_at=item.get("updated_at"),
                    )
                )
        except (KeyError, TypeError, ValidationError) as exc:
            raise MemoryProviderError("Mem0 搜索结果不符合平台契约") from exc
        return records

    async def update(
        self,
        *,
        provider_memory_id: str,
        text: str,
        metadata: dict[str, str | int | float | bool | None],
    ) -> MemoryWriteReceipt:
        """调用 Mem0 Update API 原位合并一条现有记忆，避免保留重复旧正文。"""

        if not provider_memory_id.strip():
            raise MemoryProviderError("Mem0 更新缺少 memory_id")
        value = await self._request(
            "PUT",
            f"/v1/memories/{quote(provider_memory_id, safe='')}/",
            {"text": text, "metadata": metadata},
        )
        memory_id = value.get("id")
        if memory_id is None:
            raise MemoryProviderError("Mem0 更新响应缺少记忆 ID")
        return MemoryWriteReceipt(
            event_id=str(memory_id),
            status="SUCCEEDED",
            message="长期记忆已合并更新",
        )

    async def delete(self, *, provider_memory_id: str) -> MemoryWriteReceipt:
        """调用 Mem0 删除端点，仅清理由平台精确匹配到的独立记忆。"""

        if not provider_memory_id.strip():
            raise MemoryProviderError("Mem0 删除缺少 memory_id")
        value = await self._request(
            "DELETE",
            f"/v1/memories/{quote(provider_memory_id, safe='')}/",
            {},
        )
        return MemoryWriteReceipt(
            event_id=str(value.get("id") or provider_memory_id),
            status="DELETED",
            message="长期记忆已删除",
        )

    async def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """执行一次带 Token 鉴权的 JSON 请求并统一转换供应商错误。"""

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers={
                        "Authorization": f"Token {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MemoryProviderError("Mem0 Cloud 请求失败") from exc
        if not isinstance(value, dict):
            raise MemoryProviderError("Mem0 Cloud 响应不是 JSON 对象")
        return value
