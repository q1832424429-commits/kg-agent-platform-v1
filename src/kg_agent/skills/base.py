"""定义业务 Skill 的最小异步运行协议。

专业 Agent 和 Registry 只依赖此协议，不依赖具体 LLM 或未来真实算法实现。
"""

from typing import Protocol

from kg_agent.domain.skill_models import SkillInvocation, SkillRunResult


class Skill(Protocol):
    """约束一个可注册 Skill 的公开执行接口。"""

    @property
    def name(self) -> str:
        """返回与 SkillRegistration 一致的稳定名称。"""
        ...

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """执行一次 Skill 调用并返回结构化结果。"""
        ...
