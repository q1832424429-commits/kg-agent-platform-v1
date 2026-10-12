"""验证 Mem0 Cloud 新增、搜索与原位更新请求的格式和响应转换。"""

import json

import httpx
import pytest

from kg_agent.domain.memory_models import MemoryMessage
from kg_agent.memory.mem0_cloud import Mem0CloudMemoryStore


@pytest.mark.asyncio
async def test_mem0_add_uses_server_token_and_v3_payload() -> None:
    """写入必须使用 Token 鉴权，并把用户标识放在请求顶层。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v3/memories/add/"
        assert request.headers["Authorization"] == "Token secret-test-key"
        assert payload["user_id"] == "single-user"
        assert payload["app_id"] == "kg-agent-platform-simulation"
        assert payload["messages"] == [{"role": "user", "content": "偏好设备本体"}]
        return httpx.Response(
            200,
            json={"event_id": "event-1", "status": "PENDING", "message": "accepted"},
        )

    store = Mem0CloudMemoryStore(
        api_key="secret-test-key",
        transport=httpx.MockTransport(handler),
    )
    receipt = await store.add(
        user_id="single-user",
        messages=[MemoryMessage(role="user", content="偏好设备本体")],
        metadata={"source": "test"},
        infer=True,
        app_id="kg-agent-platform-simulation",
    )

    assert receipt.event_id == "event-1"
    assert receipt.status == "PENDING"


@pytest.mark.asyncio
async def test_mem0_v3_add_returns_exact_memory_id_for_single_experience() -> None:
    """V3 results 中的单条 memory ID 可以精确定位未来的独立经验清理。"""

    memory_id = "14e1b28a-2014-40ad-ac42-69c9ef42193d"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["infer"] is False
        return httpx.Response(
            200,
            json={"results": [{"id": memory_id, "memory": "完整任务经验", "event": "ADD"}]},
        )

    store = Mem0CloudMemoryStore(
        api_key="secret-test-key", transport=httpx.MockTransport(handler)
    )
    receipt = await store.add(
        user_id="single-user",
        messages=[MemoryMessage(role="assistant", content="完整任务经验")],
        metadata={"source": "completed_task_experience"},
        infer=False,
    )

    assert receipt.status == "SUCCEEDED"
    assert receipt.event_id == memory_id


@pytest.mark.asyncio
async def test_mem0_delete_only_targets_exact_memory_without_cascade() -> None:
    """删除已确认独立记忆时只访问其 ID，不能触发关联记忆级联删除。"""

    memory_id = "14e1b28a-2014-40ad-ac42-69c9ef42193d"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/v1/memories/{memory_id}/"
        assert "delete_linked" not in request.url.params
        return httpx.Response(200, json={"message": "Memory deleted successfully!"})

    store = Mem0CloudMemoryStore(
        api_key="secret-test-key", transport=httpx.MockTransport(handler)
    )
    receipt = await store.delete(provider_memory_id=memory_id)

    assert receipt.status == "DELETED"
    assert receipt.event_id == memory_id


@pytest.mark.asyncio
async def test_mem0_search_places_user_id_inside_filters() -> None:
    """V3 搜索必须通过 filters 隔离用户，且正确转换分数和元数据。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/v3/memories/search/"
        assert payload == {
            "query": "设备故障",
            "filters": {
                "AND": [
                    {"user_id": "single-user"},
                    {"app_id": "kg-agent-platform-simulation"},
                ]
            },
            "top_k": 3,
            "rerank": True,
            "threshold": 0.2,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "memory-1",
                        "memory": "用户偏好抽取设备故障关系",
                        "score": 0.91,
                        "metadata": {"domain": "manufacturing"},
                        "categories": ["preference"],
                    }
                ]
            },
        )

    store = Mem0CloudMemoryStore(
        api_key="secret-test-key",
        transport=httpx.MockTransport(handler),
    )
    results = await store.search(
        user_id="single-user",
        query="设备故障",
        top_k=3,
        threshold=0.2,
        rerank=True,
        app_id="kg-agent-platform-simulation",
    )

    assert results[0].provider_memory_id == "memory-1"
    assert results[0].score == 0.91
    assert results[0].metadata["domain"] == "manufacturing"


@pytest.mark.asyncio
async def test_mem0_update_replaces_merged_memory_in_place() -> None:
    """合并区间必须调用 Update API 原位修改命中记录，不能追加第二条重复记忆。"""

    memory_id = "14e1b28a-2014-40ad-ac42-69c9ef42193d"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.method == "PUT"
        assert request.url.path == f"/v1/memories/{memory_id}/"
        assert payload == {
            "text": "合并后的完整任务经验",
            "metadata": {"source": "completed_task_experience"},
        }
        return httpx.Response(
            200,
            json={"id": memory_id, "memory": "合并后的完整任务经验"},
        )

    store = Mem0CloudMemoryStore(
        api_key="secret-test-key",
        transport=httpx.MockTransport(handler),
    )
    receipt = await store.update(
        provider_memory_id=memory_id,
        text="合并后的完整任务经验",
        metadata={"source": "completed_task_experience"},
    )

    assert receipt.status == "SUCCEEDED"
    assert receipt.event_id == memory_id
