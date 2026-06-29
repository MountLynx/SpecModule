# module_harness/config.py
"""HarnessConfig — harness 节点的完整配置数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .outputfmt import OutputFormat


@dataclass
class HarnessConfig:
    """harness 节点的完整配置。

    对标 tasklist 中 Task 定义的字段。
    翻译层使用 :meth:`from_task_definition` 直接构造。
    """

    # ── 三层 prompt ──
    prompt_core: str
    """Layer 1：核心提示词模板，含 {key} 占位符。"""

    prompt_modes: dict[str, str] = field(default_factory=dict)
    """Layer 2：动态 prompt 选项集。{"formal": "...", "casual": "..."}。"""

    # ── 输出约束 ──
    output_format: OutputFormat | None = None
    """输出格式约束（None = 不约束）。"""

    notdo: list[str] = field(default_factory=list)
    """否定性约束列表，拼入 system prompt。"""

    # ── LLM 默认参数（Task 可逐项覆盖）──
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None

    @classmethod
    def from_task_definition(cls, task: dict[str, Any]) -> "HarnessConfig":
        """从 tasklist Task dict 构造 HarnessConfig。

        task 中预期的键：
        - prompt_core   → Layer 1
        - prompt_modes  → Layer 2
        - outputformat  → 输出格式（dict，含 type/schema/instruction）
        - notdo         → 否定性约束列表
        - model         → LLM 模型覆盖
        - temperature   → 温度覆盖
        - think         → 扩展思考覆盖
        """
        output_format = None
        of_data = task.get("outputformat")
        if of_data is not None:
            output_format = OutputFormat(
                type=of_data["type"],
                schema=of_data.get("schema"),
                instruction=of_data.get("instruction"),
            )

        return cls(
            prompt_core=task["prompt_core"],
            prompt_modes=task.get("prompt_modes", {}),
            output_format=output_format,
            notdo=task.get("notdo", []),
            model=task.get("model"),
            temperature=task.get("temperature"),
            think=task.get("think"),
        )
