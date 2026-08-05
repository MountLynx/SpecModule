# module_harness/consistency.py
"""一致性审核 — spec + tasklist 语义一致性检查。

独立于翻译通道：审核不经过模板，直接调用注册的审核 harness body（不走 tickflow）。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from tickflow import Failure
from tickflow.views import DictView, Resolved

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
    """调用审核 harness body，返回 ConsistencyReport。"""

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
        body = self.reg.get_body(self.harness_name)

        tasklist_dict = {
            "Tasks": {
                key: dataclasses.asdict(task)
                for key, task in tasklist.tasks.items()
            },
            "Flow": tasklist.flow,
        }
        view = DictView(
            {
                "spec": Resolved(value=spec.to_dict(), k=None),
                "tasklist": Resolved(
                    value=json.dumps(tasklist_dict, ensure_ascii=False), k=None
                ),
            },
            node="__review__",
        )
        result = await body(view)

        if isinstance(result, Failure):
            raise ValueError(f"审核 harness 返回 Failure: {result.error}")

        if isinstance(result, str):
            try:
                data = json.loads(result)
            except json.JSONDecodeError as e:
                raise ValueError(f"审核输出不是合法 JSON: {e}") from e
        elif isinstance(result, dict):
            data = result
        else:
            raise ValueError(f"审核输出类型异常: {type(result).__name__}")

        consistent = data.get("consistent")
        suggestions = data.get("suggestions")  # 缺字段 → None → 下方 isinstance 校验抛错
        if not isinstance(consistent, bool):
            raise ValueError(f"审核输出缺少合法的 'consistent' 布尔字段: {data!r}")
        if not isinstance(suggestions, str):
            raise ValueError(f"审核输出 'suggestions' 必须是字符串: {data!r}")

        raw = result if isinstance(result, str) else json.dumps(data, ensure_ascii=False)
        return ConsistencyReport(
            consistent=consistent, suggestions=suggestions, raw=raw
        )
