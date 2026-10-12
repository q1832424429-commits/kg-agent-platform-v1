"""验证 Artifact 内容、元数据、血缘、复用与 Assignment fencing 的持久化闭环。"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactReuseRequest
from kg_agent.domain.enums import ArtifactStatus
from kg_agent.exceptions import ArtifactValidationError, AssignmentConflictError, PersistenceError
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.artifact_reuse_service import ArtifactReuseService
from kg_agent.services.artifact_service import ArtifactService
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_decomposition, make_subtask


async def create_task(database, task_id: UUID) -> None:
    """创建带一个数据解析 Assignment 的测试任务。"""

    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )


def artifact_service(
    session,
    storage: InMemoryObjectStorage,
    *,
    max_file_bytes: int = 1024 * 1024,
    max_files: int = 20,
    max_total_bytes: int = 10 * 1024 * 1024,
) -> ArtifactService:
    """使用同一数据库事务创建 ArtifactService。"""

    return ArtifactService(
        artifact_repository=SqlAlchemyArtifactRepository(session),
        task_repository=SqlAlchemyTaskRepository(session),
        assignment_repository=SqlAlchemyAssignmentRepository(session),
        context_repository=SqlAlchemyContextRepository(session),
        event_repository=SqlAlchemyEventRepository(session),
        storage=storage,
        registry=ArtifactRegistry.v1(),
        max_file_bytes=max_file_bytes,
        max_files_per_task=max_files,
        max_total_bytes_per_task=max_total_bytes,
    )


def raw_command(task_id: UUID, *, content: bytes = b"%PDF-mock") -> ArtifactCreateCommand:
    """构造用户上传 RawDocument 命令。"""

    return ArtifactCreateCommand(
        artifact_type="RawDocument",
        task_id=task_id,
        task_spec_revision=1,
        producer_agent="user",
        producer_run_id=uuid4(),
        content_summary="测试 PDF",
        media_type="application/pdf",
        content=content,
    )


def semantic_command(
    task_id: UUID,
    parent_id: UUID,
    *,
    artifact_id: UUID | None = None,
    supersedes_artifact_id: UUID | None = None,
) -> ArtifactCreateCommand:
    """构造由旧 Artifact 派生的 SemanticDocument 命令。"""

    return ArtifactCreateCommand(
        artifact_id=artifact_id or uuid4(),
        artifact_type="SemanticDocument",
        task_id=task_id,
        task_spec_revision=1,
        producer_agent="system",
        producer_skill="parse_document",
        producer_run_id=uuid4(),
        parent_artifact_ids=[parent_id],
        content_summary="模拟语义文档",
        content={
            "schema_version": "1.0.0",
            "source_artifact_ids": [str(parent_id)],
            "segments": [],
        },
        supersedes_artifact_id=supersedes_artifact_id,
    )


@pytest.mark.asyncio
async def test_artifact_content_checksum_lineage_and_context_are_consistent(database) -> None:
    """一次创建必须同步产生对象、元数据、血缘、事件和新上下文版本。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    command = raw_command(task_id)

    async with database.session_factory() as session, session.begin():
        record = await artifact_service(session, storage).create(command)

    assert record.ref.checksum.startswith("sha256:")
    assert record.ref.content_uri.startswith("memory://")
    assert await storage.get_bytes(record.object_key) == command.content
    context = await PostgresContextService(database.session_factory).get(task_id)
    assert context is not None
    assert context.version == 2
    assert [item.artifact_id for item in context.artifact_refs] == [command.artifact_id]

    child_command = semantic_command(task_id, command.artifact_id)
    async with database.session_factory() as session, session.begin():
        child = await artifact_service(session, storage).create(child_command)
        parents = await SqlAlchemyArtifactRepository(session).get_parent_refs(child.ref.artifact_id)
        assert [item.artifact_id for item in parents] == [command.artifact_id]

    async with database.session_factory() as session:
        repository = SqlAlchemyArtifactRepository(session)
        stored = await repository.get(child.ref.artifact_id)
        assert stored == child
        assert await repository.task_usage(task_id, "RawDocument") == (
            1,
            len(command.content),
        )


@pytest.mark.asyncio
async def test_artifact_idempotency_and_immutable_supersede(database) -> None:
    """相同 ID 可幂等重放，不同内容被拒绝；修改产生新版本并保留旧记录。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    command = raw_command(task_id)

    async with database.session_factory() as session, session.begin():
        service = artifact_service(session, storage)
        first = await service.create(command)
        repeated = await service.create(command)
        assert repeated == first

    changed = command.model_copy(update={"content": b"different"})
    async with database.session_factory() as session, session.begin():
        with pytest.raises(PersistenceError):
            await artifact_service(session, storage).create(changed)

    first_semantic = semantic_command(task_id, command.artifact_id)
    async with database.session_factory() as session, session.begin():
        await artifact_service(session, storage).create(first_semantic)
    replacement = semantic_command(
        task_id,
        command.artifact_id,
        supersedes_artifact_id=first_semantic.artifact_id,
    )
    async with database.session_factory() as session, session.begin():
        await artifact_service(session, storage).create(replacement)
        repository = SqlAlchemyArtifactRepository(session)
        old = await repository.get(first_semantic.artifact_id)
        new = await repository.get(replacement.artifact_id)
        assert old is not None and old.ref.status is ArtifactStatus.SUPERSEDED
        assert new is not None and new.ref.status is ArtifactStatus.AVAILABLE
        assert first_semantic.artifact_id in new.ref.parent_artifact_ids


@pytest.mark.asyncio
async def test_reuse_checks_schema_revision_ontology_and_parent_checksum(database) -> None:
    """任一复用兼容条件不满足时都必须给出确定性拒绝原因。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    parent_command = raw_command(task_id)
    async with database.session_factory() as session, session.begin():
        service = artifact_service(session, storage)
        parent = await service.create(parent_command)
        child_command = semantic_command(task_id, parent.ref.artifact_id)
        child = await service.create(child_command)

    valid = ArtifactReuseRequest(
        artifact_id=child.ref.artifact_id,
        expected_artifact_type="SemanticDocument",
        expected_schema_version="1.0.0",
        compatible_task_spec_revisions={1},
        expected_parent_checksums={parent.ref.checksum},
    )
    async with database.session_factory() as session:
        reuse = ArtifactReuseService(SqlAlchemyArtifactRepository(session))
        assert (await reuse.evaluate(valid)).reusable

        invalid = valid.model_copy(
            update={
                "expected_schema_version": "2.0.0",
                "compatible_task_spec_revisions": {2},
                "expected_ontology_version": "9.0.0",
                "expected_parent_checksums": {"sha256:wrong"},
            }
        )
        decision = await reuse.evaluate(invalid)
        assert not decision.reusable
        assert {
            "SCHEMA_VERSION_MISMATCH",
            "TASK_SPEC_REVISION_INCOMPATIBLE",
            "ONTOLOGY_VERSION_MISMATCH",
            "PARENT_CHECKSUM_MISMATCH",
        } <= set(decision.reason_codes)


@pytest.mark.asyncio
async def test_professional_artifact_write_requires_current_fencing_token(database) -> None:
    """专业 Agent 必须携带当前 Assignment 持有者和 fencing token。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    source_command = raw_command(task_id)
    async with database.session_factory() as session, session.begin():
        await artifact_service(session, storage).create(source_command)

    async with database.session_factory() as session, session.begin():
        assignment = await AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        ).claim_next(agent_type="document_parsing_agent", worker_id="worker-1")
        assert assignment is not None

    manifest = ArtifactCreateCommand(
        artifact_type="DocumentManifest",
        task_id=task_id,
        task_spec_revision=1,
        producer_agent="document_parsing_agent",
        producer_skill="build_document_manifest",
        producer_run_id=uuid4(),
        parent_artifact_ids=[source_command.artifact_id],
        content_summary="模拟清单",
        content={
            "schema_version": "1.0.0",
            "source_artifact_id": str(source_command.artifact_id),
            "filename": "test.pdf",
            "media_type": "application/pdf",
            "elements": [],
        },
        assignment_id=assignment.assignment_id,
        worker_id="worker-1",
        fencing_token=assignment.fencing_token - 1,
    )
    async with database.session_factory() as session, session.begin():
        with pytest.raises(AssignmentConflictError):
            await artifact_service(session, storage).create(manifest)

    valid = manifest.model_copy(update={"fencing_token": assignment.fencing_token})
    async with database.session_factory() as session, session.begin():
        created = await artifact_service(session, storage).create(valid)
        assert created.ref.producer_agent == "document_parsing_agent"


@pytest.mark.asyncio
async def test_artifact_limits_and_raw_document_mime_are_enforced(database) -> None:
    """过大内容和不支持的上传格式必须在对象写入前被拒绝。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()

    async with database.session_factory() as session, session.begin():
        with pytest.raises(ArtifactValidationError):
            await artifact_service(session, storage, max_file_bytes=1).create(
                raw_command(task_id, content=b"12")
            )

    unsupported = raw_command(task_id).model_copy(update={"media_type": "application/zip"})
    async with database.session_factory() as session, session.begin():
        with pytest.raises(ArtifactValidationError):
            await artifact_service(session, storage).create(unsupported)


@pytest.mark.asyncio
async def test_controller_cannot_bypass_assignment_or_publish_guard(database) -> None:
    """总控 Agent 不能伪造业务产物，审核发布服务完成前也不能写发布结果。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    source = raw_command(task_id)
    async with database.session_factory() as session, session.begin():
        await artifact_service(session, storage).create(source)

    controller_output = semantic_command(task_id, source.artifact_id).model_copy(
        update={"producer_agent": "controller_agent"}
    )
    async with database.session_factory() as session, session.begin():
        with pytest.raises(AssignmentConflictError):
            await artifact_service(session, storage).create(controller_output)

    forbidden_publish = ArtifactCreateCommand(
        artifact_type="PublishedGraph",
        task_id=task_id,
        task_spec_revision=1,
        producer_agent="system",
        producer_run_id=uuid4(),
        content_summary="不应发布",
        content={
            "schema_version": "1.0.0",
            "entities": [],
            "relations": [],
            "properties": [],
            "evidence": [],
            "graph_id": str(uuid4()),
            "graph_version": "1.0.0",
            "approval_artifact_id": str(uuid4()),
        },
    )
    async with database.session_factory() as session, session.begin():
        with pytest.raises(ArtifactValidationError, match="PublishGuard"):
            await artifact_service(session, storage).create(forbidden_publish)


@pytest.mark.asyncio
async def test_reuse_rejects_invalidated_parent(database) -> None:
    """直接父 Artifact 失效后，下游结果即使 checksum 相同也不能复用。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    parent_command = raw_command(task_id)
    child_command = semantic_command(task_id, parent_command.artifact_id)
    async with database.session_factory() as session, session.begin():
        service = artifact_service(session, storage)
        parent = await service.create(parent_command)
        child = await service.create(child_command)
    async with database.session_factory() as session, session.begin():
        await artifact_service(session, storage).change_status(
            artifact_id=parent.ref.artifact_id,
            target=ArtifactStatus.INVALIDATED,
            actor_id="test",
            reason_code="SOURCE_REVOKED",
        )

    request = ArtifactReuseRequest(
        artifact_id=child.ref.artifact_id,
        expected_artifact_type="SemanticDocument",
        expected_schema_version="1.0.0",
        compatible_task_spec_revisions={1},
        expected_parent_checksums={parent.ref.checksum},
    )
    async with database.session_factory() as session:
        decision = await ArtifactReuseService(SqlAlchemyArtifactRepository(session)).evaluate(
            request
        )

    assert not decision.reusable
    assert "PARENT_NOT_AVAILABLE" in decision.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_reason", ["expired", "different_task"])
async def test_artifact_claim_checks_lease_and_task_boundary(database, invalid_reason) -> None:
    """持有正确 token 仍需租约有效且 Assignment 属于当前 MasterTask。"""

    task_id = uuid4()
    other_task_id = uuid4()
    await create_task(database, task_id)
    async with database.session_factory() as session, session.begin():
        assignment = await AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        ).claim_next(agent_type="document_parsing_agent", worker_id="worker-1")
        assert assignment is not None
        if invalid_reason == "expired":
            expired = assignment.model_copy(
                update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
            await SqlAlchemyAssignmentRepository(session).update(expired)
    if invalid_reason == "different_task":
        await create_task(database, other_task_id)
    command = ArtifactCreateCommand(
        artifact_type="DocumentManifest",
        task_id=task_id if invalid_reason == "expired" else other_task_id,
        task_spec_revision=1,
        producer_agent="document_parsing_agent",
        producer_skill="build_document_manifest",
        producer_run_id=uuid4(),
        content_summary="无效认领不能写入",
        content={
            "source_artifact_id": str(uuid4()),
            "filename": "test.pdf",
            "media_type": "application/pdf",
            "elements": [],
        },
        assignment_id=assignment.assignment_id,
        worker_id="worker-1",
        fencing_token=assignment.fencing_token,
    )
    async with database.session_factory() as session, session.begin():
        with pytest.raises(AssignmentConflictError):
            await artifact_service(session, InMemoryObjectStorage()).create(command)


@pytest.mark.asyncio
async def test_file_count_does_not_limit_intermediate_artifacts(database) -> None:
    """RawDocument 数量上限不能误伤同任务的中间处理产物。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    command = raw_command(task_id)
    async with database.session_factory() as session, session.begin():
        service = artifact_service(session, storage, max_files=1)
        await service.create(command)
        await service.create(semantic_command(task_id, command.artifact_id))
        with pytest.raises(ArtifactValidationError, match="原始文档数量"):
            await service.create(raw_command(task_id))


@pytest.mark.asyncio
async def test_application_service_commits_content_and_metadata(database) -> None:
    """持久化应用入口返回时，内容和元数据已经提交并可在新 Session 中读取。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    service = PostgresArtifactService(
        database.session_factory, storage=storage, registry=ArtifactRegistry.v1()
    )
    command = raw_command(task_id)

    created = await service.create(command)

    assert await service.read_content(created.ref.artifact_id) == command.content
    async with database.session_factory() as session:
        assert await SqlAlchemyArtifactRepository(session).get(command.artifact_id) is not None


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_metadata_and_compensates_object(
    database, monkeypatch
) -> None:
    """数据库 commit 阶段失败时不能留下可见元数据，也要清理本次孤儿对象。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    service = PostgresArtifactService(
        database.session_factory, storage=storage, registry=ArtifactRegistry.v1()
    )
    command = raw_command(task_id)

    async def fail_commit(session):
        """模拟连接在 commit 前失败，而不是放宽任何验收断言。"""

        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated commit"):
        await service.create(command)

    digest = hashlib.sha256(command.content).hexdigest()
    object_key = f"tasks/{task_id}/artifacts/{command.artifact_id}/content-{digest}.bin"
    assert not await storage.exists(object_key)
    async with database.session_factory() as session:
        assert await SqlAlchemyArtifactRepository(session).get(command.artifact_id) is None
        context = await SqlAlchemyContextRepository(session).get(task_id)
        assert context is not None and context.version == 1


@pytest.mark.asyncio
async def test_repository_failure_compensates_uploaded_object(database, monkeypatch) -> None:
    """元数据尚未创建就失败时，应用服务回滚并清理本次对象。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    service = PostgresArtifactService(
        database.session_factory, storage=storage, registry=ArtifactRegistry.v1()
    )
    command = raw_command(task_id)

    async def fail_add(repository, record):
        """在存储上传之后模拟数据库写入失败。"""

        raise PersistenceError("simulated insert failure")

    monkeypatch.setattr(SqlAlchemyArtifactRepository, "add", fail_add)
    with pytest.raises(PersistenceError, match="simulated insert"):
        await service.create(command)

    digest = hashlib.sha256(command.content).hexdigest()
    key = f"tasks/{task_id}/artifacts/{command.artifact_id}/content-{digest}.bin"
    assert not await storage.exists(key)


@pytest.mark.asyncio
async def test_uncertain_commit_does_not_delete_committed_content(database, monkeypatch) -> None:
    """commit 实际成功但响应丢失时，二次核实必须保留已经被元数据引用的内容。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    service = PostgresArtifactService(
        database.session_factory, storage=storage, registry=ArtifactRegistry.v1()
    )
    command = raw_command(task_id)
    original_commit = AsyncSession.commit

    async def commit_then_fail(session):
        """先真实提交再模拟响应丢失，确保不会误删已提交对象。"""

        await original_commit(session)
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_fail)
    with pytest.raises(RuntimeError, match="response lost"):
        await service.create(command)

    assert await service.read_content(command.artifact_id) == command.content


@pytest.mark.asyncio
async def test_standard_skill_content_persists_with_parent_lineage(database) -> None:
    """自定义 Skill 的标准产物按既有契约保存，且保留输入血缘。"""

    task_id = uuid4()
    await create_task(database, task_id)
    storage = InMemoryObjectStorage()
    async with database.session_factory() as session, session.begin():
        parent = await artifact_service(session, storage).create(raw_command(task_id))

    command = semantic_command(task_id, parent.ref.artifact_id)
    async with database.session_factory() as session, session.begin():
        child = await artifact_service(session, storage).create(command)

    assert child.ref.parent_artifact_ids == [parent.ref.artifact_id]
    async with database.session_factory() as session:
        service = artifact_service(session, storage)
        standard = await service.read_content(child.ref.artifact_id)
        stored = await SqlAlchemyArtifactRepository(session).get(child.ref.artifact_id)
    assert standard == await storage.get_bytes(child.object_key)
    assert stored is not None
    assert stored.ref.parent_artifact_ids == [parent.ref.artifact_id]
