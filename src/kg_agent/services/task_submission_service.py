"""提供总控 Agent 最小闭环的应用服务入口。

本服务不实现 FastAPI，也不执行专业 Agent；它只把经过校验的请求交给总控 Agent。
"""

from kg_agent.agents.controller.agent import ControllerAgent
from kg_agent.agents.controller.state import ControllerRequest, ControllerRunResult


class TaskSubmissionService:
    """封装一次用户消息到总控 Agent 运行结果的应用调用。"""

    def __init__(self, controller_agent: ControllerAgent) -> None:
        """绑定唯一的总控 Agent。"""

        self._controller_agent = controller_agent

    async def submit(self, request: ControllerRequest) -> ControllerRunResult:
        """提交用户消息并返回结构化拆解或澄清结果。"""

        return await self._controller_agent.run(request)
