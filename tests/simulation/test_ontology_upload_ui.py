"""验证隔离实验室公开专用外部本体上传入口。"""

from pathlib import Path


def test_simulation_ui_separates_real_documents_and_external_ontology(
    project_root: Path,
) -> None:
    """测试端应分别提交真实普通文档与真实外部本体。"""

    page = (project_root / "simulation_lab" / "web" / "index.html").read_text("utf-8")
    script = (project_root / "simulation_lab" / "web" / "app.js").read_text("utf-8")

    assert 'id="ontology-input"' in page
    assert 'accept=".json,application/json"' in page
    assert 'id="document-input"' in page
    assert "PDF、DOCX、XLSX、PPTX、JPEG、PNG、TIFF、WebP" in page
    assert 'id="inject-default-ontology"' not in page
    assert "simulation-1.0.0" not in page
    assert 'id="published-ontology-select"' in page
    assert 'id="published-graph-select"' in page
    assert 'body.append("files", file)' in script
    assert 'body.append("ontology_file", ontology)' in script
    assert 'body.append("published_ontology_artifact_id"' in script
    assert 'body.append("published_graph_artifact_id"' in script
    assert "/api/v1/published-assets/ontologies" in script
    assert "/api/v1/published-assets/graphs" in script
    assert 'data-tab="reviews"' in page
    assert 'id="review-dialog"' in page
    assert 'id="review-actions"' in page
    assert "target_content" in script
    assert "查看候选内容" in script
    assert 'const canResolve = materials.review.status === "PENDING"' in script
    assert "SUBMIT_CHANGES" in script
    assert "查看图谱预览" in script
    assert "graph-preview" in script
    assert "GraphChangeSet" in script
    assert 'id="plan-review-panel"' in page
    assert 'id="plan-review-dialog"' in page
    assert 'id="plan-review-feedback"' in page
    assert "批准并开始执行" in page
    assert "提交修改并重新规划" in page
    assert "/plan-review/approve" in script
    assert "/plan-review/revise" in script
    assert "openPlanReviewDialog" in script
    assert "revisePlan" in script
    assert "data-plan-move" in script
    assert "ordered_subtask_ids" in script
    assert "topologicalPlanOrder" in script
    assert "planStages" in script
    assert "只能调整同一执行阶段内的优先顺序" in script
    assert "Agent 调用 Skill" in script
    assert "skill.duration_ms" in script
    assert "PLAN_REVIEW_REQUESTED" in script
    assert "本轮验证执行结束，请人工检查以上意图、拆解、分配和 Skill 顺序" not in script

    simulation_app = (project_root / "simulation_lab" / "app.py").read_text("utf-8")
    assert "execute_workers=False" in simulation_app
    assert "publish_decomposition=False" in simulation_app
    assert "当前候选计划尚未审核" in simulation_app
    assert "publish_planned_decomposition" in simulation_app
    assert "_dependency_order_violations" in simulation_app
    assert "审核顺序必须遵守前置依赖" in simulation_app
    assert "本轮验证执行结束，请人工检查以上意图、拆解、分配和 Skill 顺序" not in simulation_app

    graph_component = (project_root / "src" / "kg_agent" / "web" / "graph_view.js").read_text(
        "utf-8"
    )
    vendor = (project_root / "src" / "kg_agent" / "web" / "vendor" / "g6-5.1.1.min.js").read_text(
        "utf-8"
    )
    assert "KGGraphView" in graph_component
    assert "G6.Graph" in graph_component
    assert "GraphChangeSet" in graph_component
    assert "drag-element" in graph_component
    assert "apply_graph_changeset" in (
        project_root / "src" / "kg_agent" / "skills" / "graph_changes.py"
    ).read_text("utf-8")
    assert "AntV" in vendor
