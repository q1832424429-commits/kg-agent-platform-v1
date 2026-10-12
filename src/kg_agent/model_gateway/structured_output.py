"""统一清理并校验模型返回的 JSON 结构化文本。

本模块只处理 UTF-8 文本外围的空白和 Markdown JSON 代码围栏，不猜测、不截取夹杂
说明文字的响应，也不修补业务字段。清理后的内容必须完整通过调用方指定的 Pydantic
模型，无法解析的数据绝不能进入 Agent、Skill 或 Artifact 流程。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ValidationError

from kg_agent.model_gateway.base import StructuredModelT

_JSON_FENCE = re.compile(
    r"\A\s*```(?:json)?\s*(?P<body>[\s\S]*?)\s*```\s*\Z",
    flags=re.IGNORECASE,
)


def normalize_json_text(raw: str) -> str:
    """去除结构化响应两端空白和可选 JSON 代码围栏。

    只接受整个响应都被一组代码围栏包裹的情况。围栏外存在解释文字时保持原样，
    让后续 Pydantic JSON 解析明确失败，避免从不可信文本中猜测截取 JSON。
    """

    stripped = raw.strip()
    match = _JSON_FENCE.fullmatch(stripped)
    return match.group("body").strip() if match is not None else stripped


def validate_structured_text(
    raw: str,
    output_type: type[StructuredModelT],
) -> StructuredModelT:
    """把模型文本清理后严格解析为目标 Pydantic 模型。"""

    return output_type.model_validate_json(normalize_json_text(raw))


def format_validation_error(exc: ValidationError | ValueError) -> str:
    """生成不含原始输入的字段级校验摘要，供日志和模型修复提示使用。"""

    if isinstance(exc, ValidationError):
        details: list[str] = []
        for item in exc.errors(include_url=False, include_input=False)[:12]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
            details.append(
                f"{location}: {item.get('msg', '校验失败')}"
                f" [{item.get('type', 'validation_error')}]"
            )
        return "; ".join(details)
    return type(exc).__name__


def dump_structured_value(value: BaseModel) -> dict[str, object]:
    """把已经校验的嵌套 Pydantic 内容转换为 JSON 兼容字典。"""

    return value.model_dump(mode="json")
