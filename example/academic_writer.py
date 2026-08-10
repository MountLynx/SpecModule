# example/academic_writer.py
"""academic_writer — 灵感式写作 → 学术英语完整流水线（普通 Module 过程式组装）。

仅 fact_review_loop 是 SubModule（可复用处理单元）；本文件是**顶层工作流**（整机）：
模块级 `academic_tasklist`（Loop1/Loop2 为 submodule 节点）+ `run_writer()` 入口
（内部构造 `Module(spec, tasklist, modules={"fact_review_loop": FactReviewLoop})`）。

spec 契约：input {raw_text: str}（可选未声明字段 target_field / max_words，
传入即生效，缺省时 prompt 占位符渲染 None 并提示忽略）；
output {final_text: str, modification_notes: str}。

图::

    [Organize] --> Loop1 --> Polish --> Loop2 --> Finalize --> Report

Loop1/Loop2 复用同一 fact_review_loop 处理单元（黑盒嵌入运行，不进审计/
快照/回滚，只暴露终点输出）——同一 fact_review harness、两个节点实例。
"""

from __future__ import annotations

from typing import Any

from llm import LLMConfig, create_llm_client

from module_harness.config import HarnessConfig
from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.outputfmt import OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist

from .fact_review_loop import FactReviewLoop

ORGANIZE_CONFIG = HarnessConfig(
    name="organize",
    prompt_core=(
        "你是学术写作助手。把以下「灵感草稿」整理成逻辑通顺的英文文段："
        "去除重复、合并碎片、理顺语序、把中文表达翻译为英文。\n"
        "必须保留草稿中的全部信息，不得新增任何草稿中没有的事实或观点，"
        "不得遗漏任何要点。\n"
        "目标领域（值为 None 表示未提供，忽略此要求）：{target_field}\n"
        "字数上限（值为 None 表示未提供，不设限）：{max_words}\n\n"
        "灵感草稿：{raw_text}"
    ),
    output_format=OutputFormat(type="json_object"),
    notdo=["新增事实", "遗漏要点", "改变原意"],
)

POLISH_CONFIG = HarnessConfig(
    name="polish",
    prompt_core=(
        "你是学术英语写作专家。把以下「整理稿」润色为符合学术英语写作规范的"
        "文段：正式、精确、句式多样、逻辑衔接自然。\n"
        "约束：只改变语言表达，不得改变任何事实、不得增删信息点。\n\n"
        "整理稿（JSON 对象，取其 text 字段）：{draft}"
    ),
    output_format=OutputFormat(type="json_object"),
    notdo=["改变事实", "新增信息", "删减信息"],
)

FINALIZE_CONFIG = HarnessConfig(
    name="finalize",
    prompt_core=(
        "你是学术写作助手。基于「原始文段」与「润色后文段」整合输出最终版本：\n"
        "- 以润色后文段为主体；\n"
        "- 对照原始文段核验：遗漏的信息点补回，多余/杜撰的内容删除；\n"
        "- 可对语言做最后一次微调，但不得改变事实。\n\n"
        "输出两个字段：\n"
        "- text：最终文段；\n"
        "- notes：本次整合阶段的语言调整说明（简述改了哪些语言表达，为何）。\n\n"
        "原始文段：{original}\n\n"
        "润色后文段（JSON 对象，取其 text 字段）：{draft}"
    ),
    output_format=OutputFormat(type="json_object"),
    notdo=["改变事实", "杜撰信息"],
)


def build_report(view: Any) -> dict[str, Any]:
    """确定性聚合修改说明（markdown）——script 聚合可审计，不用 LLM 生成。"""
    f_out = view["Finalize"].value
    l1_out = view["Loop1"].value
    l2_out = view["Loop2"].value
    finalize = f_out if isinstance(f_out, dict) else {}
    loop1 = l1_out if isinstance(l1_out, dict) else {}
    loop2 = l2_out if isinstance(l2_out, dict) else {}

    def stage(name: str, loop: dict[str, Any]) -> str:
        attempt = loop.get("attempt", 0)
        verdict = "通过（无事实问题）" if loop.get("clean") else "达上限未清"
        remaining = loop.get("issues_remaining", [])
        lines = [f"### {name}", f"- 事实审阅轮数：{attempt}", f"- 结论：{verdict}"]
        if remaining:
            lines.append("- 遗留问题：")
            for i, issue in enumerate(remaining, 1):
                detail = issue.get("detail", issue) if isinstance(issue, dict) else issue
                lines.append(f"  {i}. {detail}")
        return "\n".join(lines)

    notes = "\n\n".join([
        "# 修改说明",
        "## 处理流程",
        "1. 整理：中英混杂灵感草稿 → 逻辑通顺英文文段（保留全部信息）",
        "2. 阶段 1 事实审阅：原始文段 vs 整理稿（循环修复至无事实问题或达上限）",
        "3. 学术润色：整理稿 → 学术英语文段（只改语言，不改事实）",
        "4. 阶段 2 事实审阅：原始文段 vs 润色稿（同上）",
        "5. 整合：原始 + 润色 → 最终版本（语言微调）",
        stage("阶段 1 审阅（整理稿）", loop1),
        stage("阶段 2 审阅（润色稿）", loop2),
        "## 整合阶段语言调整",
        str(finalize.get("notes", "")).strip() or "（无说明）",
    ])
    return {
        "final_text": str(finalize.get("text", "")),
        "modification_notes": notes,
    }


academic_tasklist = Tasklist(
    tasks={
        "Organize": TaskDefinition(
            type="harness",
            harness="organize",
            inputs={
                "raw_text": "{spec.raw_text}",
                "target_field": "{spec.target_field}",
                "max_words": "{spec.max_words}",
            },
        ),
        "Loop1": TaskDefinition(
            type="submodule",
            submodule="fact_review_loop",
            inputs={
                "original_text": "{spec.raw_text}",
                "draft_text": "Organize",
            },
        ),
        "Polish": TaskDefinition(
            type="harness",
            harness="polish",
            inputs={"draft": "Loop1"},
        ),
        "Loop2": TaskDefinition(
            type="submodule",
            submodule="fact_review_loop",
            inputs={
                "original_text": "{spec.raw_text}",
                "draft_text": "Polish",
            },
        ),
        "Finalize": TaskDefinition(
            type="harness",
            harness="finalize",
            inputs={
                "original": "{spec.raw_text}",
                "draft": "Loop2",
            },
        ),
        "Report": TaskDefinition(
            type="script",
            script="build_report",
            inputs={"finalize": "Finalize", "loop1": "Loop1", "loop2": "Loop2"},
        ),
    },
    flow=(
        "[Organize] --> Loop1\n"
        "Loop1 --> Polish\n"
        "Polish --> Loop2\n"
        "Loop2 --> Finalize\n"
        "Finalize --> Report"
    ),
)


def _build_registry(llm_client: Any) -> HarnessRegistry:
    """注册本流水线的 harness 与 script（过程式形态需显式构造 registry）。"""
    reg = HarnessRegistry(llm_client=llm_client, event_bus=EventBus.null())
    for hc in (ORGANIZE_CONFIG, POLISH_CONFIG, FINALIZE_CONFIG):
        reg.harness(hc.name, hc)
    reg.script("build_report")(build_report)
    return reg


def run_writer(
    spec: dict[str, Any],
    *,
    llm_client: Any = None,
    max_ticks: int = 100,
    persist: bool = True,
):
    """构造并运行 academic_writer（普通 Module 过程式组装），返回 firings 列表。

    - llm_client 缺省从 env 创建（LLMConfig.from_env）
    - persist=False：零落盘快速模式（测试/演示用）
    - review_harness=None：固定 tasklist，发布前已验证，跳过一致性审核
    """
    if llm_client is None:
        llm_client = create_llm_client(LLMConfig.from_env())
    mod = Module(
        spec=spec,
        tasklist=academic_tasklist,
        llm_client=llm_client,
        registry=_build_registry(llm_client),
        modules={"fact_review_loop": FactReviewLoop},
        review_harness=None,
        persist=persist,
        status_file=persist,
    )
    return mod.run(max_ticks=max_ticks)
