"""独立 Assignment Worker 进程入口。"""

import asyncio
import logging
import os
import socket

from kg_agent.agents.professional.factory import build_professional_agent_pool
from kg_agent.agents.professional.prompts import ProfessionalPromptRepository
from kg_agent.agents.professional.registry import ProfessionalAgentPool
from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.extensions.runtime_registry import build_composite_registries
from kg_agent.model_gateway.deepseek import DeepSeekModelAdapter
from kg_agent.runtime import build_runtime
from kg_agent.services.model_call_observer import RunLogModelCallObserver
from kg_agent.services.postgres_worker_services import PostgresWorkerServices
from kg_agent.services.task_coordinator import TaskCoordinator
from kg_agent.workers.assignment_worker import AssignmentWorker

LOGGER = logging.getLogger("kg_agent.worker")


async def run_worker() -> None:
    """并行轮询内置及已发布自定义专业 Agent 的 Assignment，并推进计划状态。"""
    runtime = build_runtime()
    await runtime.startup()
    # 专业 Agent 的模型规划调用与 LLM Skill 共享同一任务日志观测出口。
    model = DeepSeekModelAdapter(
        runtime.settings,
        observer=RunLogModelCallObserver(runtime.run_logs),
    )

    async def build_worker_components() -> tuple[
        AssignmentWorker, PostgresWorkerServices, ProfessionalAgentPool
    ]:
        """按当前已发布扩展构造本轮 Worker 快照，避免重启才识别新 Agent/Skill。"""

        agents, skills = await build_composite_registries(
            builtin_agents=runtime.agent_registry,
            builtin_skills=runtime.skill_registry,
            extensions=runtime.extensions,
            model_gateway=model,
            artifact_reader=runtime.artifacts,
            artifact_registry=runtime.artifact_registry,
        )
        pool = build_professional_agent_pool(
            model_gateway=model,
            agent_registry=agents,
            skill_registry=skills,
            artifact_registry=runtime.artifact_registry,
            artifact_writer=runtime.artifacts,
            prompt_repository=ProfessionalPromptRepository(
                runtime.project_root / "configs" / "prompts" / "professional"
            ),
            max_repairs=runtime.settings.controller_max_repairs,
        )
        services = PostgresWorkerServices(
            runtime.database.session_factory,
            agents=agents,
            skills=skills,
            lease_seconds=runtime.settings.assignment_lease_seconds,
            memory_service=runtime.memory,
        )
        return (
            AssignmentWorker(
                worker_id=f"{socket.gethostname()}-{os.getpid()}",
                services=services,
                agents=pool,
                heartbeat_seconds=runtime.settings.assignment_heartbeat_seconds,
            ),
            services,
            pool,
        )

    worker, services, pool = await build_worker_components()
    coordinator = TaskCoordinator(runtime)
    poll_seconds = max(float(os.getenv("WORKER_POLL_SECONDS", "1")), 0.1)
    LOGGER.info("Worker 已启动，专业 Agent 类型：%s", ", ".join(pool.list_types()))
    try:
        while True:
            # 每轮均从当前发布指针重建轻量 Registry 快照；草稿、停用版本和历史
            # 修订不会出现。正在执行中的 Assignment 仍由其既有租约和运行记录追溯。
            worker, services, pool = await build_worker_components()

            async def run_agent(
                agent_type: str, active_worker: AssignmentWorker = worker
            ) -> AgentRunResult | None:
                """隔离单个角色异常，避免一个 Agent 阻塞其他无依赖任务。"""

                try:
                    return await active_worker.run_once(agent_type)
                except Exception:
                    LOGGER.exception("处理 %s Assignment 失败", agent_type)
                    return None

            results = await asyncio.gather(
                *(run_agent(agent_type) for agent_type in pool.list_types())
            )
            completed = [result for result in results if result is not None]
            for result in completed:
                await runtime.run_logs.append(
                    task_id=result.task_id,
                    record_type="AGENT_RUN",
                    actor=result.agent_name,
                    payload=result.model_dump(mode="json"),
                )
            for task_id in {result.task_id for result in completed}:
                action = await coordinator.advance(task_id)
                await runtime.run_logs.append(
                    task_id=task_id,
                    record_type="COORDINATION",
                    actor="task_coordinator",
                    payload={"action": action},
                )
            if not completed:
                # 进程退出或网络中断可能留下 CLAIMED/RUNNING 租约；只有租约真正
                # 过期后才创建下一次尝试，避免任务永久停在“等待 Assignment”。
                recovered = await services.recover_expired()
                if recovered:
                    LOGGER.warning("已回收 %s 个过期 Assignment 租约", len(recovered))
                    continue
                await asyncio.sleep(poll_seconds)
    finally:
        await runtime.shutdown()


def main() -> None:
    """配置基础日志并启动异步 Worker。"""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
