"""验证任务级公开 JSONL 日志的隔离、追加和读取。"""

import json
from uuid import uuid4

import pytest

from kg_agent.services.run_log_service import RunLogService


@pytest.mark.asyncio
async def test_run_log_service_appends_one_file_per_task(tmp_path) -> None:
    """同一任务应按行追加，不同任务不得写入同一个日志文件。"""

    first_task, second_task = uuid4(), uuid4()
    service = RunLogService(tmp_path)

    await service.append(
        task_id=first_task,
        record_type="CONTROLLER_RUN",
        actor="controller_agent",
        payload={"intent": "START"},
    )
    await service.append(
        task_id=first_task,
        record_type="COORDINATION",
        actor="task_coordinator",
        payload={"action": "WAITING_DEPENDENCIES"},
    )
    await service.append(
        task_id=second_task,
        record_type="AGENT_RUN",
        actor="document_parsing_agent",
        payload={"status": "SUCCESS"},
    )

    first_content = await service.read(first_task)
    second_content = await service.read(second_task)
    assert first_content is not None and second_content is not None
    first_rows = [json.loads(line) for line in first_content.decode().splitlines()]
    second_rows = [json.loads(line) for line in second_content.decode().splitlines()]
    assert [row["record_type"] for row in first_rows] == [
        "CONTROLLER_RUN",
        "COORDINATION",
    ]
    assert [row["task_id"] for row in first_rows] == [str(first_task), str(first_task)]
    assert second_rows[0]["task_id"] == str(second_task)

    assert await service.count(first_task) == 2
    records, cursor = await service.read_after(first_task, after=1)
    assert cursor == 2
    assert records[0][0] == 2
    assert records[0][1]["record_type"] == "COORDINATION"
