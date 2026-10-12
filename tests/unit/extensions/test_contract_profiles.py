"""验证自定义能力契约模板只能引用已注册 Artifact，并保持业务视图稳定。"""

import pytest

from kg_agent.extensions.contract_profiles import ContractProfileRegistry, SkillContractProfile
from kg_agent.extensions.contracts import BusinessSkillDraftInput
from kg_agent.extensions.service import CustomExtensionService
from kg_agent.infrastructure.database.session import Database
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.artifact_registry import ArtifactRegistry


def test_v1_contract_profiles_cover_pipeline_side_output_and_standalone() -> None:
    """第一版模板应覆盖三种使用方式，并包含文字报告终端能力。"""

    registry = ContractProfileRegistry.v1(ArtifactRegistry.v1())
    profiles = {item.profile_id: item for item in registry.list_all()}

    assert profiles["property_transform"].integration_mode == "PIPELINE"
    assert profiles["document_text_report"].integration_mode == "SIDE_OUTPUT"
    assert profiles["standalone_text_report"].integration_mode == "STANDALONE"
    assert profiles["standalone_text_report"].input_artifact_types == ()
    assert profiles["standalone_text_report"].output_artifact_types == ("TextReport",)


def test_contract_profile_rejects_unknown_artifact_type() -> None:
    """模板不能以业务便利为由绕过 Artifact Registry 创造自由类型。"""

    with pytest.raises(Exception, match="未知 Artifact 类型"):
        ContractProfileRegistry(
            ArtifactRegistry.v1(),
            (
                SkillContractProfile(
                    profile_id="invalid",
                    business_name="非法模板",
                    integration_mode="PIPELINE",
                    input_artifact_types=("MagicInput",),
                    output_artifact_types=("TextReport",),
                    result_semantics="DERIVE",
                    input_description="未知输入",
                    output_description="文字报告",
                    suggested_position="未知位置",
                ),
            ),
        )


def test_legacy_request_keeps_document_table_contract() -> None:
    """没有 profile_id 的历史请求必须维持原有文档表格分析行为。"""

    profile = ContractProfileRegistry.v1(ArtifactRegistry.v1()).require(None)

    assert profile.profile_id == "document_table_analysis"
    assert profile.input_artifact_types == ("SemanticDocument",)
    assert profile.output_artifact_types == ("StructuredDataset",)


@pytest.mark.asyncio
async def test_model_can_only_select_registered_contract_profile() -> None:
    """模型负责语义推荐，但正式说明和集成模式必须来自代码 Profile。"""

    database = Database("sqlite+aiosqlite:///:memory:")
    model = FakeModelAdapter(
        [
            {
                "profile_id": "property_transform",
                "confidence": 0.95,
                "reason": "用户要求处理已经抽取出的属性并继续融合。",
            }
        ]
    )
    service = CustomExtensionService(
        database.session_factory,
        artifact_registry=ArtifactRegistry.v1(),
        builtin_agent_keys=[],
        builtin_skill_keys=[],
        model_gateway=model,
    )

    proposal = await service.propose_business_skill_contract(
        BusinessSkillDraftInput(
            name="设备单位标准化",
            solves_problem="把已经抽取出的设备参数统一为标准单位。",
            applicable_materials="设备属性数据。",
            expected_result="标准化后的属性继续进入知识融合。",
            example_request="请统一设备参数单位。",
        )
    )

    assert proposal.selected.profile_id == "property_transform"
    assert proposal.selected.integration_mode == "PIPELINE"
    assert proposal.selected.input_description == "已经抽取的属性"
    assert len(model.calls) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_unknown_model_selected_profile_is_rejected_by_code() -> None:
    """模型虚构 Profile 时必须停止创建，不能把未知契约带入定义或 SharedContext。"""

    database = Database("sqlite+aiosqlite:///:memory:")
    model = FakeModelAdapter(
        [
            {
                "profile_id": "model_invented_contract",
                "confidence": 0.99,
                "reason": "这是模型自行创造的契约。",
            }
        ]
    )
    service = CustomExtensionService(
        database.session_factory,
        artifact_registry=ArtifactRegistry.v1(),
        builtin_agent_keys=[],
        builtin_skill_keys=[],
        model_gateway=model,
    )

    with pytest.raises(Exception, match="未知自定义 Skill 能力模板"):
        await service.propose_business_skill_contract(
            BusinessSkillDraftInput(
                name="非法能力",
                solves_problem="要求模型创造系统不存在的产物。",
                applicable_materials="未知资料。",
                expected_result="未知结果。",
                example_request="请创造一种新的产物。",
            )
        )
    await database.dispose()
