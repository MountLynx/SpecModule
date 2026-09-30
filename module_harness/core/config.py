# module_harness/config.py
"""HarnessConfig — harness 节点的完整配置数据模型。"""

from __future__ import annotations

import dataclasses
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

    validate_retries: int = 0
    """输出校验失败时的额外重试次数（带校验错误反馈重问）。

    0（缺省）= 不重试，行为与无此字段时逐字节一致。
    N > 0 = 校验失败后最多再问 N 次，每次 prompt 追加校验错误反馈段；
    预算耗尽返回最后一次的 Failure(type="llm")。
    仅作用于输出校验失败；LLMError（传输层）不重试、不消耗预算。
    """

    # ── LLM 参数（Task 可逐项覆盖）──
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None

    # ── SDK 透传参数 ──
    api_params: dict[str, Any] = field(default_factory=dict)
    """透传给 LLM SDK 的额外参数。按 API 官方格式写入，如
    ``{"temperature": 0.3, "thinking": {"type": "enabled"}}``。
    会与 temperature / think 等独立字段合并（api_params 优先级更高）。"""

    # ── 调用形态 ──
    mode: str = "text"
    """调用形态："text" = chat 补全（默认）；"image" = 图像生成（文生图）。"""

    image_size: str | None = None
    """图像尺寸，如 "1024x1024"；None = API 默认。仅 mode="image" 生效。"""

    image_dir: str = "images"
    """图像产物落盘目录（cwd 相对）。仅 mode="image" 生效。"""

    # ── 注册信息（submodule 用）──
    name: str | None = None
    """注册名。submodule 的 harnesses 列表中必须提供。"""

    def __post_init__(self) -> None:
        if self.mode not in ("text", "image"):
            raise ValueError(f"mode 须为 'text' | 'image'，得到 {self.mode!r}")
        if self.mode == "image" and self.output_format is not None:
            raise ValueError(
                "mode='image' 与 output_format 互斥（图像无文本输出格式可校验）"
            )
        if self.mode == "image" and self.image_dir is None:
            raise ValueError("mode='image' 不接受显式 null 的 image_dir")
        if self.validate_retries < 0:
            raise ValueError(f"validate_retries 须 >= 0，得到 {self.validate_retries!r}")
        if self.validate_retries > 0 and self.mode == "image":
            raise ValueError("mode='image' 无文本输出格式可校验，validate_retries 须为 0")

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写 dict（含 output_format）。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HarnessConfig":
        """从 to_dict() 输出还原。"""
        data = dict(d)
        of = data.pop("output_format", None)
        if of is not None:
            data["output_format"] = OutputFormat(**of)
        return cls(**data)

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
        - api_params    → SDK 透传参数（dict）
        - mode          → 调用形态："text"（默认）| "image"
        - image_size    → 图像尺寸（仅 mode="image"）
        - image_dir     → 图像落盘目录（仅 mode="image"）
        - validate_retries → 校验失败重试预算（int ≥ 0，缺省 0）

        mode/image_size/image_dir：显式 null 的 image_dir 会被拒绝。
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
            api_params=task.get("api_params", {}),
            mode=task.get("mode", "text"),
            image_size=task.get("image_size"),
            image_dir=task.get("image_dir", "images"),
            validate_retries=task.get("validate_retries", 0),
        )
