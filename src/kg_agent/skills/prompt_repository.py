"""读取 Skill.md 的前置元数据和正文执行说明。

本模块只负责解析单个 Skill 包的 Markdown 定义；不负责模型调用、业务处理或 Artifact 校验。
"""

from pathlib import Path

import yaml

from kg_agent.exceptions import RegistryValidationError


class SkillPromptRepository:
    """为 Skill Registry 提供受路径保护的提示词读取能力。"""

    def load(self, package_directory: Path) -> tuple[dict[str, object], str]:
        """读取 ``SKILL.md`` 的 YAML front matter 与执行说明。

        Args:
            package_directory: 当前 Skill 的独立目录，必须包含 ``SKILL.md``。

        Returns:
            前置元数据字典及去除首尾空白后的 Markdown 执行说明。

        Raises:
            RegistryValidationError: 文件缺失或内容为空。
        """

        target = package_directory / "SKILL.md"
        try:
            content = target.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RegistryValidationError(f"无法读取 Skill 定义：{target}") from exc
        if not content.startswith("---\n"):
            raise RegistryValidationError(f"Skill 定义缺少 YAML front matter：{target}")
        try:
            _, raw_metadata, body = content.split("---", maxsplit=2)
            metadata = yaml.safe_load(raw_metadata)
        except (ValueError, yaml.YAMLError, TypeError) as exc:
            raise RegistryValidationError(f"Skill 元数据格式无效：{target}") from exc
        instruction = body.strip()
        if not isinstance(metadata, dict) or not instruction:
            raise RegistryValidationError(f"Skill 定义缺少元数据或执行说明：{target}")
        return metadata, instruction
