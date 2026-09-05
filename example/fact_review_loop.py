# example/fact_review_loop.py
"""FactReviewLoop — 通用事实审阅循环 submodule。

spec 契约：input {original_text: str, draft_text: str|dict}；
output {text, attempt, clean, issues_remaining}。

图（`Merge.join: OR` = 一次性种子 + loop 回边的标准 loop 成员）::

    [Seed] --> Merge --> Review --|has_issues|--> Fix --> Merge
                          |                                  ^
                          |--|clean|--> Exit                 |
                          +----------------------------------+

- Seed    harness 转发种子稿（script 读不到 spec；LLM 转发失真由 loop 自愈）
- Merge   script 合并：修复稿优先于种子稿，计数轮次
- Review  harness 事实审阅：原始 vs 当前稿逐句对比（缺漏/幻觉/改动）
- Fix     harness 按 issues 逐条修复
- Exit    script 收集终点输出

任一「文本需对照原文核验」的工作流可引用本模块为 submodule 节点复用。
"""

from __future__ import annotations

from typing import Any

from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat
from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.model.submodule import SubModule, script


def has_issues(view: Any) -> bool:
    """issues 非空且未达上限（3 轮）→ 走修复边。

    上限为函数内联常量（guard 读不到 spec；pack 单文件导出只含函数体，
    不能引用模块级常量）。达上限仍有 issues 时 clean 边触发退出，
    遗留 issues 由 Exit 收进 issues_remaining，不静默丢弃。

    GuardView 消费：``output`` = 本 tick 被裁决的 src（Review）输出；
    ``field("draft")`` = Review 具名 bind 字段（task.inputs 键），
    即 Merge 的最新输出（同 producer 名访问同源同策略）。
    """
    review = view.output
    issues = review.get("issues", []) if isinstance(review, dict) else []
    merge = view.field("draft")
    attempt = merge.get("attempt", 0) if isinstance(merge, dict) else 0
    return bool(issues) and attempt < 3


def clean(view: Any) -> bool:
    """与 has_issues 严格互补（XOR 分支不得双走）。

    注意：必须内联 has_issues 逻辑（pack 逐文件单函数导出，跨函数引用在
    加载后失效）——两处判断必须保持同步修改。
    """
    review = view.output
    issues = review.get("issues", []) if isinstance(review, dict) else []
    merge = view.field("draft")
    attempt = merge.get("attempt", 0) if isinstance(merge, dict) else 0
    return not (bool(issues) and attempt < 3)


SEED_DRAFT_CONFIG = HarnessConfig(
    name="seed_draft",
    prompt_core=(
        "你是文档处理管线中的转发节点。原样转发以下「待审稿」内容，禁止任何修改、"
        "删减、补充、翻译或重新组织。\n"
        "若「待审稿」是 JSON 对象（含 text 字段），只转发其中的 text 字段内容；"
        "若为纯文本，直接原样输出。\n"
        "直接输出文本内容本身，不要用 JSON 包裹，不要添加任何解释、前后缀或标记。\n\n"
        "待审稿：{draft}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["修改内容", "删减内容", "补充内容", "翻译", "添加解释"],
)

FACT_REVIEW_CONFIG = HarnessConfig(
    name="fact_review",
    prompt_core=(
        "你是学术写作管线中的事实审阅者。将「原始文段」与「当前稿」逐句对比，"
        "只报告以下三类事实问题：\n"
        "1. omission 信息缺漏：原始文段有、当前稿缺失的信息；\n"
        "2. hallucination 幻觉新增：当前稿有、原始文段没有的信息（杜撰）；\n"
        "3. alteration 事实改动：同一信息被改写成与原意不符。\n\n"
        "每条问题含：type（omission/hallucination/alteration）、detail（说明）、"
        "quote_original（原文引文）、quote_draft（当前稿引文）。\n"
        "没有任何事实问题时 issues 为空数组、clean 为 true。\n\n"
        "原始文段：{original}\n\n"
        "当前稿（JSON 对象，取其 draft 字段）：{draft}"
    ),
    output_format=OutputFormat(type="json_object"),
    notdo=["报告语言风格问题", "报告结构问题", "报告用词问题"],
)

FIX_ISSUES_CONFIG = HarnessConfig(
    name="fix_issues",
    prompt_core=(
        "你是学术写作管线中的修复者。按「问题列表」逐条修复「当前稿」：\n"
        "- omission：补回缺失信息；\n"
        "- hallucination：删除杜撰内容；\n"
        "- alteration：还原为原始文段事实。\n\n"
        "约束：只改动被点名内容，不引入原始文段中没有的新事实，"
        "不重写未被点名内容的措辞。\n"
        "直接输出修复后的完整文本内容本身，不要用 JSON 包裹，"
        "不要添加任何解释、前后缀或标记。\n\n"
        "当前稿（JSON 对象，取其 draft 字段）：{draft}\n\n"
        "问题列表（JSON 对象，取其 issues 字段）：{issues}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["新增原文没有的事实", "修改未被点名内容", "改动事实", "添加解释"],
)


class FactReviewLoop(SubModule):
    """通用事实审阅循环：原始 vs 当前稿 → 发现问题 → 修复 → 回审。"""

    name = "fact_review_loop"
    version = "0.1.0"
    description = (
        "给定原始文段与待审文段，循环执行「事实审阅 → 问题修复 → 回审」，"
        "直到无事实问题或达最大轮数（3）。"
    )
    spec_schema = SpecSchema(
        input={"original_text": "str", "draft_text": "any"},
        output={
            "text": "str",
            "attempt": "int",
            "clean": "bool",
            "issues_remaining": "list",
        },
    )
    harnesses = [SEED_DRAFT_CONFIG, FACT_REVIEW_CONFIG, FIX_ISSUES_CONFIG]
    guards = [("has_issues", has_issues), ("clean", clean)]
    tasklist = Tasklist(
        tasks={
            "Seed": TaskDefinition(
                type="harness",
                harness="seed_draft",
                inputs={"draft": "{spec.draft_text}"},
            ),
            "Merge": TaskDefinition(
                type="script",
                script="merge",
                inputs={"seed": "Seed", "fixed": "Fix"},
            ),
            "Review": TaskDefinition(
                type="harness",
                harness="fact_review",
                inputs={"draft": "Merge", "original": "{spec.original_text}"},
            ),
            "Fix": TaskDefinition(
                type="harness",
                harness="fix_issues",
                inputs={"draft": "Merge", "issues": "Review"},
            ),
            "Exit": TaskDefinition(
                type="script",
                script="collect_result",
                inputs={"review": "Review", "merge": "Merge"},
            ),
        },
        flow=(
            "[Seed] --> Merge\n"
            "Merge --> Review\n"
            "Review --|has_issues|--> Fix\n"
            "Fix --> Merge\n"
            "Review --|clean|--> Exit\n"
            "Merge.join: OR"
        ),
    )

    @script("merge")
    def merge(view: Any) -> dict[str, Any]:
        """合并输入：修复稿优先于种子稿（Fix 首次未触发时用 Seed）；计数轮次。

        读 bind 字段名（seed/fixed，即 task.inputs 的键）——本模块固定节点
        名；详细模式内联 loop 经 academic_writer._make_merge 闭包绑定各自
        节点名（逻辑同步）。
        """
        try:
            fixed = view.field("fixed")
        except (KeyError, TypeError):
            fixed = None
        if isinstance(fixed, dict) and fixed.get("text"):
            draft = fixed["text"]
        elif isinstance(fixed, str) and fixed:
            draft = fixed
        else:
            seed = view.field("seed")
            draft = seed if isinstance(seed, str) else (
                seed.get("text", "") if isinstance(seed, dict) else ""
            )
        n = view.state.get("attempt", 0) + 1
        view.state["attempt"] = n
        return {"draft": draft, "attempt": n}

    @script("collect_result")
    def collect_result(view: Any) -> dict[str, Any]:
        """收集终点输出：当前稿 + 轮次 + verdict + 遗留 issues（达上限未清时）。

        读 bind 字段名（review/merge，即 task.inputs 的键）——详细模式经
        _make_collect_result 闭包绑定各自节点名（逻辑同步）。
        """
        review = view.field("review")
        merge = view.field("merge")
        issues = review.get("issues", []) if isinstance(review, dict) else []
        clean_flag = review.get("clean", False) if isinstance(review, dict) else False
        return {
            "text": merge.get("draft", "") if isinstance(merge, dict) else "",
            "attempt": merge.get("attempt", 0) if isinstance(merge, dict) else 0,
            "clean": bool(clean_flag),
            "issues_remaining": [] if clean_flag else issues,
        }
