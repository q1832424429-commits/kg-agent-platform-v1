"""设备故障规则检测自定义 SCRIPT Skill 示例。

本脚本只演示平台自定义 SCRIPT Skill 的标准输入输出协议：从标准输入读取受控的
``SemanticDocument`` Artifact，依据少量可解释关键词生成一个 ``StructuredDataset``。
它不读取网络、数据库、文件系统或模型密钥，也不替代正式的故障诊断算法。
"""

from __future__ import annotations

import json
import sys
from typing import Any


# 这些规则仅用于验证“脚本 Skill → Artifact → 下游 Agent”的完整链路。正式版本应替换为
# 经过领域校验的模型、规则库或算法，而不是不断扩大此示例词表。
FAULT_KEYWORDS = ("故障", "异常", "报警", "过热", "振动", "漏油", "停机", "失效")
CRITICAL_KEYWORDS = ("停机", "安全", "爆炸", "火灾")
HIGH_KEYWORDS = ("报警", "过热", "严重", "失效")


def _severity(text: str) -> str:
    """根据示例关键词返回风险等级，优先返回更高等级。"""

    if any(keyword in text for keyword in CRITICAL_KEYWORDS):
        return "严重"
    if any(keyword in text for keyword in HIGH_KEYWORDS):
        return "高"
    return "中"


def _first_semantic_document(payload: dict[str, Any]) -> dict[str, Any] | None:
    """从平台统一输入中找到第一个已解析语义文档，忽略不属于本 Skill 的 Artifact。"""

    for item in payload.get("inputs", []):
        if item.get("artifact_type") == "SemanticDocument":
            content = item.get("content")
            if isinstance(content, dict):
                return content
    return None


def build_result(payload: dict[str, Any]) -> dict[str, Any]:
    """把语义片段中的故障描述转换为平台规定的 StructuredDataset 输出。"""

    document = _first_semantic_document(payload)
    rows: list[dict[str, str]] = []
    if document is not None:
        for segment in document.get("segments", []):
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text", "")).strip()
            if not text or not any(keyword in text for keyword in FAULT_KEYWORDS):
                continue
            evidence = segment.get("evidence", [])
            locator = ""
            if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
                locator = str(evidence[0].get("locator", ""))
            rows.append(
                {
                    "风险等级": _severity(text),
                    "故障描述": text,
                    "处理建议": "请由设备工程师结合现场工况进一步确认并处理。",
                    "证据位置": locator or str(segment.get("segment_id", "未知片段")),
                }
            )

    return {
        "outputs": [
            {
                "artifact_type": "StructuredDataset",
                "content": {
                    "schema_version": "1.0.0",
                    "columns": ["风险等级", "故障描述", "处理建议", "证据位置"],
                    "rows": rows,
                },
                "content_summary": f"设备故障规则检测识别到 {len(rows)} 条风险项。",
            }
        ],
        "quality_signals": {
            "fault_item_count": float(len(rows)),
            "rule_based": 1.0,
        },
    }


def main() -> None:
    """读取 Runner 通过标准输入传入的 JSON，并只向标准输出写入一个 JSON 对象。"""

    payload = json.load(sys.stdin)
    print(json.dumps(build_result(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
