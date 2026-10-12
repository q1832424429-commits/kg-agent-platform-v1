"""提供总控 Agent 校验错误的稳定格式化能力。"""

from kg_agent.validators.subtask_validator import ValidationIssue


def format_validation_issues(issues: list[ValidationIssue]) -> str:
    """把结构化问题转换为适合异常和日志的简短文本。"""

    return "; ".join(f"{item.code}[{item.subtask_id or '-'}]: {item.message}" for item in issues)
