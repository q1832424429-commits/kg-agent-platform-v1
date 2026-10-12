"""暴露只生成建议、不自动修改生产配置的离线自进化工具。"""

from kg_agent.evolution.suggestions import OptimizationSuggestion, generate_suggestions

__all__ = ["OptimizationSuggestion", "generate_suggestions"]
