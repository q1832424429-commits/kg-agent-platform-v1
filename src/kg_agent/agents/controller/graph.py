"""构建总控 Agent 的固定小型 LangGraph。

该图只组织总控 Agent 自身步骤，不表达专业 Skill DAG 或全局业务工作流。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from kg_agent.agents.controller.nodes import ControllerNodes
from kg_agent.agents.controller.state import ControllerState


def build_controller_graph(nodes: ControllerNodes) -> Any:
    """编译总控 Agent 的加载、推理、校验、修复和发布状态机。"""

    graph = StateGraph(ControllerState)
    graph.add_node("load_context", nodes.load_context)
    graph.add_node("decompose", nodes.decompose)
    graph.add_node("validate", nodes.validate_candidate)
    graph.add_node("repair", nodes.repair_candidate)
    graph.add_node("publish", nodes.publish)
    graph.add_node("fail", nodes.fail)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "decompose")
    graph.add_edge("decompose", "validate")
    graph.add_conditional_edges(
        "validate",
        nodes.route_after_validation,
        {
            "publish": "publish",
            "repair": "repair",
            "fail": "fail",
        },
    )
    graph.add_edge("repair", "validate")
    graph.add_edge("publish", END)
    graph.add_edge("fail", END)
    return graph.compile()
