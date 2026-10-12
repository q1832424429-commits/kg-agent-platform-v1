# 任务拆解

根据下面的运行事实生成一个完整的 `TaskPlanDraft`：理解用户当前终点，识别任务变化，复用可用成果，并给出完成该终点所需的最少 Agent 级子任务计划。UUID、revision、状态、时间戳和 Skill 原子伴随输出由运行时代码补齐，不要在语义计划中生成。

实例知识抽取统一使用一个 `knowledge_extract` Agent 子任务，在一次专业 Agent 工作中联合抽取实体、关系、属性和证据；不要再拆成多个串行抽取子任务。具体输入和伴随输出由确定性 Skill 契约补齐。用户明确限制抽取范围时，把限制写入 TaskSpec 的 `extraction_scope` 和子任务目标，但仍只创建一个知识抽取子任务。

对既有候选图谱做增量更新时，先创建 `graph_changeset_generate`（仅输出 `GraphChangeSet`），再创建依赖它的 `graph_changeset_apply`（仅输出 `CandidateGraph`）。前者与后者不能合并；生成的 ChangeSet 是差异草案，应用后的 CandidateGraph 才是预览、编辑和图谱审核对象。

不要复述输入，不要解释规则，只返回符合 `output_contract` 的 JSON。若确定性校验返回修复信息，保持用户目标不变并修复全部问题。
