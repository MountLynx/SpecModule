# module_harness/consistency.py
"""一致性审核 — spec + tasklist 语义一致性检查。

独立于翻译通道：审核不经过模板，经 call_harness 直接调用注册的审核
harness 配置（不走 tickflow 图）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .call import HarnessCallError, call_harness
from .config import HarnessConfig
from .outputfmt import OutputFormat
from .registry import HarnessRegistry
from .spec import Spec, Tasklist


@dataclass
class ConsistencyReport:
    """一致性审核结果。"""

    consistent: bool
    suggestions: str
    raw: str  # LLM 原始输出（审计链）


class ConsistencyError(ValueError):
    """一致性审核未通过。携带完整 report，str() 输出问题描述。"""

    def __init__(self, report: ConsistencyReport) -> None:
        self.report = report
        super().__init__(f"一致性审核未通过: {report.suggestions}")


REVIEW_HARNESS_CONFIG = HarnessConfig(
    prompt_core=(
        "你是一致性审核器。判断给定 tasklist 是否能实现 spec 的目标。\n"
        "审核要点：\n"
        "1. spec 的每个目标/需求是否被 tasklist 中的任务覆盖\n"
        "2. task 中引用的字段（{spec.xxx}、inputs）在 spec 中是否存在\n"
        "3. flow 是否可达、是否有死路或未定义节点\n"
        "spec: {spec}\n"
        "tasklist: {tasklist}\n"
        '输出 JSON：{"consistent": true/false, "suggestions": "..."}'
    ),
    output_format=OutputFormat(type="json_object"),
    temperature=0.1,
)


def register_review_harness(
    reg: HarnessRegistry, name: str = "spec_tasklist_review"
) -> None:
    """注册内置一致性审核 harness（默认名 spec_tasklist_review）。"""
    reg.harness(name, REVIEW_HARNESS_CONFIG)


class ConsistencyReviewer:
    """调用审核 harness，返回 ConsistencyReport。

    审核走 call_harness（task 级地板）：不传 bus，内部中间事件静默
    （ConsistencyReviewed 领域事件由 Module 直接发射，不经此处）。
    按 register_review_harness 契约，审核 harness 只带 config 注册
    （注册期 promptmode/prompt_extra 对审核器无意义，call_harness 路径不传）。
    """

    def __init__(
        self, registry: HarnessRegistry, harness_name: str = "spec_tasklist_review"
    ) -> None:
        self.reg = registry
        self.harness_name = harness_name

    async def review(self, spec: Spec, tasklist: Tasklist) -> ConsistencyReport:
        """执行一致性审核。审核失败（LLM 错误/输出不合法）抛 ValueError。"""
        if self.reg.harness_config(self.harness_name) is None:
            raise ValueError(
                f"审核 harness '{self.harness_name}' 未注册。"
                f"请先调用 register_review_harness(reg) 注册内置审核器，"
                f"或自行 reg.harness('{self.harness_name}', ...)。"
            )

        try:
            call = await call_harness(
                self.reg.harness_config(self.harness_name),
                {
                    "spec": spec.to_dict(),
                    "tasklist": json.dumps(tasklist.to_dict(), ensure_ascii=False),
                },
                llm_client=self.reg.llm_client,
            )
        except HarnessCallError as e:
            raise ValueError(f"审核 harness 返回 Failure: {e.failure.error}") from e

        data = call.value
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as e:
                raise ValueError(f"审核输出不是合法 JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"审核输出必须是 JSON 对象: {data!r}")

        consistent = data.get("consistent")
        suggestions = data.get("suggestions")  # 缺字段 → None → 下方 isinstance 校验抛错
        if not isinstance(consistent, bool):
            raise ValueError(f"审核输出缺少合法的 'consistent' 布尔字段: {data!r}")
        if not isinstance(suggestions, str):
            raise ValueError(f"审核输出 'suggestions' 必须是字符串: {data!r}")

        raw = call.raw
        if raw is None:
            raw = (
                call.value
                if isinstance(call.value, str)
                else json.dumps(data, ensure_ascii=False)
            )
        return ConsistencyReport(consistent=consistent, suggestions=suggestions, raw=raw)
