"""为自定义脚本提供固定的函数式入口适配器。

该文件属于 Runner 镜像，不来自用户上传。用户模块只需实现
``run(inputs, parameters)`` 并返回完整脚本响应对象，无需自行处理标准输入输出。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from collections.abc import Awaitable
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_module(entrypoint: str) -> ModuleType:
    """从当前沙箱加载用户入口模块，不修改系统模块搜索目录。"""

    path = Path(entrypoint).resolve()
    specification = importlib.util.spec_from_file_location("custom_skill_entrypoint", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("无法加载脚本入口模块")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


async def _resolve_awaitable(value: Awaitable[Any]) -> Any:
    """把用户异步 run 的通用 Awaitable 转换为 asyncio.run 可执行协程。"""

    return await value


def main() -> None:
    """读取标准请求，调用用户 run 函数，并把返回对象编码为标准 JSON。"""

    if len(sys.argv) != 2:
        raise RuntimeError("缺少脚本入口路径")
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("脚本输入必须是 JSON 对象")
    module = _load_module(sys.argv[1])
    parameters = payload.get("parameters", {})
    adapter = parameters.get("entity_record_adapter")
    if adapter is not None:
        result = _run_entity_record_adapter(module, payload.get("inputs", []), adapter)
    else:
        function = getattr(module, "run", None)
        if not callable(function):
            raise RuntimeError("函数式脚本必须定义 run(inputs, parameters)")
        result = function(payload.get("inputs", []), parameters)
        if inspect.isawaitable(result):
            result = asyncio.run(_resolve_awaitable(result))
    if not isinstance(result, dict):
        raise ValueError("run 函数必须返回 JSON 对象")
    json.dump(result, sys.stdout, ensure_ascii=False)


def _run_entity_record_adapter(
    module: ModuleType, inputs: list[dict[str, Any]], adapter: dict[str, str]
) -> dict[str, Any]:
    """逐项调用已确认的用户函数，保留实体 ID、类型、证据和原始别名。"""

    matching = [item for item in inputs if item.get("artifact_type") == "EntityDataset"]
    if len(matching) != 1:
        raise ValueError("实体逐项脚本需要唯一的 EntityDataset 输入")
    content = matching[0].get("content")
    if not isinstance(content, dict) or not isinstance(content.get("entities"), list):
        raise ValueError("实体输入缺少 entities 列表")
    function = getattr(module, adapter["function_name"], None)
    if not callable(function):
        raise ValueError("脚本中不存在已确认的实体处理函数")
    entities = []
    for record in content["entities"]:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise ValueError("实体缺少名称，不能调用逐项脚本")
        original_name = record["name"]
        value = function(**{adapter["parameter_name"]: original_name})
        if inspect.isawaitable(value):
            value = asyncio.run(_resolve_awaitable(value))
        new_name = (
            value
            if adapter["result_field"] == "__value__" and isinstance(value, str)
            else value.get(adapter["result_field"])
            if isinstance(value, dict)
            else None
        )
        if not isinstance(new_name, str) or not new_name.strip():
            raise ValueError("实体处理函数未返回非空的标准名称")
        changed = dict(record)
        changed["name"] = new_name.strip()
        aliases = list(record.get("aliases", []))
        if changed["name"] != original_name and original_name not in aliases:
            aliases.append(original_name)
        changed["aliases"] = aliases
        entities.append(changed)
    return {
        "outputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {**content, "entities": entities},
                "content_summary": f"已标准化 {len(entities)} 个实体名称",
            }
        ]
    }


if __name__ == "__main__":
    main()
