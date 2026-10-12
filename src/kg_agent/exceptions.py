"""定义项目当前阶段使用的基础业务异常。"""


class KGAgentError(Exception):
    """项目内可预期异常的公共基类。"""


class ConfigurationError(KGAgentError):
    """运行配置缺失或格式错误。"""


class ModelGatewayError(KGAgentError):
    """模型调用失败、返回空内容或无法解析结构化结果。"""


class RegistryValidationError(KGAgentError):
    """Agent Registry 配置重复、不完整或无法通过契约校验。"""


class CustomExtensionValidationError(KGAgentError):
    """用户自定义 Agent 或 Skill 的草稿、权限或发布状态不合法。"""


class SubTaskValidationError(KGAgentError):
    """总控 Agent 生成的子任务方案在修复次数耗尽后仍不合法。"""


class ContextConflictError(KGAgentError):
    """SharedContext 不存在、版本不匹配或写入违反追加式更新规则。"""


class StateTransitionError(KGAgentError):
    """任务或 Assignment 请求执行不允许的状态迁移。"""


class AssignmentConflictError(KGAgentError):
    """Assignment 已被认领、租约失效或 fencing token 不匹配。"""


class PersistenceError(KGAgentError):
    """持久化操作违反唯一性、不可变性或事务一致性要求。"""


class ArtifactValidationError(KGAgentError):
    """Artifact 类型、内容、血缘、大小或复用条件不合法。"""


class ArtifactNotFoundError(KGAgentError):
    """请求读取或变更的 Artifact 不存在。"""


class ObjectStorageError(KGAgentError):
    """对象存储访问失败或检测到不可变对象覆盖。"""


class SkillPermissionError(KGAgentError):
    """Agent 尝试调用未授权的业务 Skill。"""


class SkillExecutionError(KGAgentError):
    """Skill 调用参数或输入 Artifact 不符合注册契约。"""


class ReviewConflictError(KGAgentError):
    """人工审核请求、目标版本或用户决策发生冲突。"""


class PublishGuardError(KGAgentError):
    """发布请求缺少有效审核批准、角色权限或目标版本绑定。"""


class MemoryProviderError(KGAgentError):
    """长期记忆云服务请求失败或返回了不符合契约的数据。"""
