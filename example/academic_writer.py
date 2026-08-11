# example/academic_writer.py
"""academic_writer — 灵感式写作 → 学术英语完整流水线（普通 Module，双模板）。

一个 module 两种使用方式（框架原生多模板设置：TemplateLoader 注册 +
Module(template_name=...) 选择，翻译器为 script 类型、确定性返回流程）：

- **academic_writer**（默认）：Loop1/Loop2 为 **submodule 节点**，复用
  fact_review_loop 处理单元（黑盒嵌入运行，不进审计，只暴露终点输出）
- **academic_writer_detailed**（详细模式）：事实审阅 loop **内联展开到主图**
  （Seed1→Merge1→Review1→Fix1 循环 + Exit1；Loop2 同构），全部节点进审计
  记录，每轮修复过程可审阅

spec 契约：input {raw_text: str}（可选未声明字段 target_field / max_words，
传入即生效，缺省时 prompt 占位符渲染 None 并提示忽略）；
output {final_text: str, modification_notes: str}。

submodule 形式图::

    [Organize] --> Loop1 --> Polish --> Loop2 --> Finalize --> Report

详细模式图（Loop1 展开示例；Loop2 同构）::

    [Organize] --> Seed1 --> Merge1 --> Review1 --|has_issues_1|--> Fix1 --> Merge1
                                    |                                    ^
                                    |--|clean_1|--> Exit1 --> Polish --> ...
                                    +------------------------------------+
"""

from __future__ import annotations

from typing import Any, Callable

from llm import LLMConfig, create_llm_client

from module_harness.config import HarnessConfig
from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.outputfmt import OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist
from module_harness.translator import TemplateLoader

from .fact_review_loop import (
    FACT_REVIEW_CONFIG,
    FIX_ISSUES_CONFIG,
    SEED_DRAFT_CONFIG,
    FactReviewLoop,
)

ORGANIZE_CONFIG = HarnessConfig(
    name="organize",
    prompt_core=(
        "你是学术写作助手。把以下「灵感草稿」整理成逻辑通顺的英文文段："
        "去除重复、合并碎片、理顺语序、把中文表达翻译为英文。\n"
        "必须保留草稿中的全部信息，不得新增任何草稿中没有的事实或观点，"
        "不得遗漏任何要点。\n"
        "目标领域（值为 None 表示未提供，忽略此要求）：{target_field}\n"
        "字数上限（值为 None 表示未提供，不设限）：{max_words}\n"
        "直接输出整理后的英文文段本身，不要用 JSON 包裹，"
        "不要添加任何解释、前后缀或标记。\n\n"
        "灵感草稿：{raw_text}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["新增事实", "遗漏要点", "改变原意", "添加解释"],
)

POLISH_CONFIG = HarnessConfig(
    name="polish",
    prompt_core=(
        "你是学术英语写作专家。把以下「整理稿」润色为符合学术英语写作规范的"
        "文段：正式、精确、句式多样、逻辑衔接自然。\n"
        "约束：只改变语言表达，不得改变任何事实、不得增删信息点。\n"
        "直接输出润色后的文段本身，不要用 JSON 包裹，"
        "不要添加任何解释、前后缀或标记。\n\n"
        "整理稿（JSON 对象，取其 text 字段）：{draft}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["改变事实", "新增信息", "删减信息", "添加解释"],
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


def _make_build_report(loop1_key: str, loop2_key: str) -> Callable[[Any], dict[str, Any]]:
    """生成绑定 loop 终点节点名的 build_report body。

    逻辑与 submodule 形式一致（聚合 {text, attempt, clean, issues_remaining}
    同构输出）；submodule 形式 loop1_key="Loop1"、详细模式 loop1_key="Exit1"。
    用 reg.body 注册（非 @script——绑定的节点名因形式而异）。
    """

    def build_report(view: Any) -> dict[str, Any]:
        f_out = view["Finalize"].value
        l1_out = view[loop1_key].value
        l2_out = view[loop2_key].value
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

    return build_report


def _make_merge(seed_key: str, fix_key: str) -> Callable[[Any], dict[str, Any]]:
    """生成绑定节点名的 merge body（详细模式内联 loop 用）。

    逻辑与 FactReviewLoop.merge 一致（修复稿优先于种子稿、计数轮次）——
    fact_review_loop 的 @script 版本受 pack 单文件导出约束需自包含，此处
    闭包绑定各自节点名，两处必须保持同步修改。
    """

    def merge(view: Any) -> dict[str, Any]:
        try:
            fixed = view[fix_key].value
        except (KeyError, AttributeError):
            fixed = None
        if isinstance(fixed, dict) and fixed.get("text"):
            draft = fixed["text"]
        elif isinstance(fixed, str) and fixed:
            draft = fixed
        else:
            seed = view[seed_key].value
            draft = seed if isinstance(seed, str) else (
                seed.get("text", "") if isinstance(seed, dict) else ""
            )
        n = view.state.get("attempt", 0) + 1
        view.state["attempt"] = n
        return {"draft": draft, "attempt": n}

    return merge


def _make_collect_result(review_key: str, merge_key: str) -> Callable[[Any], dict[str, Any]]:
    """生成绑定节点名的 collect_result body（同 FactReviewLoop.collect_result
    逻辑——当前稿 + 轮次 + verdict + 遗留 issues）。"""

    def collect_result(view: Any) -> dict[str, Any]:
        review = view[review_key].value
        merge = view[merge_key].value
        issues = review.get("issues", []) if isinstance(review, dict) else []
        clean_flag = review.get("clean", False) if isinstance(review, dict) else False
        return {
            "text": merge.get("draft", "") if isinstance(merge, dict) else "",
            "attempt": merge.get("attempt", 0) if isinstance(merge, dict) else 0,
            "clean": bool(clean_flag),
            "issues_remaining": [] if clean_flag else issues,
        }

    return collect_result


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


def _make_loop_guards(suffix: str, review_key: str, merge_key: str) -> list[tuple[str, Callable]]:
    """生成绑定指定节点名的互补 guard 对（详细模式内联 loop 用）。

    上限 3 内联在函数体（与 fact_review_loop 的 has_issues/clean 同语义）；
    闭包绑定各 loop 自己的 Review/Merge 节点名。详细模式不打包（主 module
    无 pack），无"注册名 = 函数名"自包含约束。
    """

    def has_issues(view: Any) -> bool:
        review = view[review_key].value
        issues = review.get("issues", []) if isinstance(review, dict) else []
        merge = view[merge_key].value
        attempt = merge.get("attempt", 0) if isinstance(merge, dict) else 0
        return bool(issues) and attempt < 3

    def clean(view: Any) -> bool:
        # 与 has_issues 严格互补（XOR 分支不得双走）
        review = view[review_key].value
        issues = review.get("issues", []) if isinstance(review, dict) else []
        merge = view[merge_key].value
        attempt = merge.get("attempt", 0) if isinstance(merge, dict) else 0
        return not (bool(issues) and attempt < 3)

    has_issues.__name__ = f"has_issues_{suffix}"
    clean.__name__ = f"clean_{suffix}"
    return [(f"has_issues_{suffix}", has_issues), (f"clean_{suffix}", clean)]


# ── 详细模式：事实审阅 loop 内联展开（不用 submodule，全程可审计）──────
# 两组 loop 节点（Seed1/.../Exit1 与 Seed2/.../Exit2）；merge / collect_result
# / build_report 经闭包工厂绑定各自节点名（reg.body 注册，非 @script——绑定
# 名因形式而异；逻辑与 FactReviewLoop 的 @script 版本保持同步）。
detailed_tasklist = Tasklist(
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
        # Loop 1（整理稿审阅）
        "Seed1": TaskDefinition(
            type="harness", harness="seed_draft",
            inputs={"draft": "Organize"},
        ),
        "Merge1": TaskDefinition(
            type="script", script="merge1",
            inputs={"seed": "Seed1", "fixed": "Fix1"},
        ),
        "Review1": TaskDefinition(
            type="harness", harness="fact_review",
            inputs={"draft": "Merge1", "original": "{spec.raw_text}"},
        ),
        "Fix1": TaskDefinition(
            type="harness", harness="fix_issues",
            inputs={"draft": "Merge1", "issues": "Review1"},
        ),
        "Exit1": TaskDefinition(
            type="script", script="collect_result1",
            inputs={"review": "Review1", "merge": "Merge1"},
        ),
        "Polish": TaskDefinition(
            type="harness", harness="polish",
            inputs={"draft": "Exit1"},
        ),
        # Loop 2（润色稿审阅）
        "Seed2": TaskDefinition(
            type="harness", harness="seed_draft",
            inputs={"draft": "Polish"},
        ),
        "Merge2": TaskDefinition(
            type="script", script="merge2",
            inputs={"seed": "Seed2", "fixed": "Fix2"},
        ),
        "Review2": TaskDefinition(
            type="harness", harness="fact_review",
            inputs={"draft": "Merge2", "original": "{spec.raw_text}"},
        ),
        "Fix2": TaskDefinition(
            type="harness", harness="fix_issues",
            inputs={"draft": "Merge2", "issues": "Review2"},
        ),
        "Exit2": TaskDefinition(
            type="script", script="collect_result2",
            inputs={"review": "Review2", "merge": "Merge2"},
        ),
        "Finalize": TaskDefinition(
            type="harness", harness="finalize",
            inputs={"original": "{spec.raw_text}", "draft": "Exit2"},
        ),
        "Report": TaskDefinition(
            type="script", script="build_report",
            inputs={"finalize": "Finalize", "loop1": "Exit1", "loop2": "Exit2"},
        ),
    },
    flow=(
        "[Organize] --> Seed1\n"
        "Seed1 --> Merge1\n"
        "Merge1 --> Review1\n"
        "Review1 --|has_issues_1|--> Fix1\n"
        "Fix1 --> Merge1\n"
        "Review1 --|clean_1|--> Exit1\n"
        "Exit1 --> Polish\n"
        "Polish --> Seed2\n"
        "Seed2 --> Merge2\n"
        "Merge2 --> Review2\n"
        "Review2 --|has_issues_2|--> Fix2\n"
        "Fix2 --> Merge2\n"
        "Review2 --|clean_2|--> Exit2\n"
        "Exit2 --> Finalize\n"
        "Finalize --> Report\n"
        "Merge1.join: OR\n"
        "Merge2.join: OR"
    ),
)


# ── 翻译器（script 类型，确定性）："翻译为另一种形式的 tasklist"──────────
# 模板通道经 translate(spec, template) 产出最终 tasklist——翻译器返回值即
# 流程形式（submodule 黑盒 vs 内联展开），view["spec"] 可读 spec（此处不需要）。
def _tl_academic(view: Any) -> dict[str, Any]:
    """翻译器：返回 submodule 形式 tasklist（Loop1/Loop2 为 submodule 节点）。"""
    return academic_tasklist.to_dict()


def _tl_detailed(view: Any) -> dict[str, Any]:
    """翻译器：返回详细模式 tasklist（loop 内联展开）。"""
    return detailed_tasklist.to_dict()


# 模板 = 翻译声明 + 特定流程 tasklist 定义（TemplateLoader.register 直收 dict）
ACADEMIC_TEMPLATE: dict[str, Any] = {
    "name": "academic_writer",
    "description": "灵感式写作 → 学术英语（默认：事实审阅 loop 以 submodule 节点复用，黑盒嵌入）",
    "translation": {"type": "script", "script": "tl_academic"},
    "tasklist": academic_tasklist.to_dict(),
}

DETAILED_TEMPLATE: dict[str, Any] = {
    "name": "academic_writer_detailed",
    "description": "灵感式写作 → 学术英语（详细模式：事实审阅 loop 内联展开到主图，全程可审计）",
    "translation": {"type": "script", "script": "tl_detailed"},
    "tasklist": detailed_tasklist.to_dict(),
}


def _build_registry(
    llm_client: Any,
    mode: str = "submodule",
    event_bus: EventBus | None = None,
) -> HarnessRegistry:
    """注册流水线 harness / script / guard（按模式；含模板通道翻译器）。

    ``event_bus`` 缺省 None → EventBus.null()（CLI 传入外部 bus 时接入，
    否则 CLI 收不到 harness 事件）。
    """
    reg = HarnessRegistry(
        llm_client=llm_client, event_bus=event_bus or EventBus.null()
    )
    for hc in (ORGANIZE_CONFIG, POLISH_CONFIG, FINALIZE_CONFIG):
        reg.harness(hc.name, hc)
    if mode == "detailed":
        # 内联 loop 所需的 harness / 闭包 body / guard
        for hc in (SEED_DRAFT_CONFIG, FACT_REVIEW_CONFIG, FIX_ISSUES_CONFIG):
            reg.harness(hc.name, hc)
        reg.body("merge1", _make_merge("Seed1", "Fix1"))
        reg.body("collect_result1", _make_collect_result("Review1", "Merge1"))
        reg.body("merge2", _make_merge("Seed2", "Fix2"))
        reg.body("collect_result2", _make_collect_result("Review2", "Merge2"))
        reg.body("build_report", _make_build_report("Exit1", "Exit2"))
        for name, fn in _make_loop_guards("1", "Review1", "Merge1"):
            reg.guard(name, fn)
        for name, fn in _make_loop_guards("2", "Review2", "Merge2"):
            reg.guard(name, fn)
    else:
        reg.body("build_report", _make_build_report("Loop1", "Loop2"))
    # 模板通道翻译器（两种模式都需要）
    reg.script("tl_academic")(_tl_academic)
    if mode == "detailed":
        reg.script("tl_detailed")(_tl_detailed)
    return reg


def run_writer(
    spec: dict[str, Any],
    *,
    mode: str = "submodule",
    llm_client: Any = None,
    max_ticks: int = 100,
    persist: bool = True,
):
    """构造并运行 academic_writer（框架模板通道，template_name 按 mode 选择）。

    - mode="submodule"（默认）：模板 `academic_writer`——事实审阅 loop 以
      submodule 节点复用（黑盒嵌入，不进审计，只暴露终点输出）
    - mode="detailed"：模板 `academic_writer_detailed`——loop 内联展开到主图，
      全部节点进审计记录（详细模式，可逐 tick 审阅修复过程）
    - llm_client 缺省从 env 创建（LLMConfig.from_env）
    - persist=False：零落盘快速模式（测试/演示用）
    - review_harness=None：固定流程模板，发布前已验证，跳过一致性审核
    """
    if llm_client is None:
        llm_client = create_llm_client(LLMConfig.from_env())
    if mode == "detailed":
        template_name = "academic_writer_detailed"
        template = DETAILED_TEMPLATE
        modules: dict[str, Any] = {}
    elif mode == "submodule":
        template_name = "academic_writer"
        template = ACADEMIC_TEMPLATE
        modules = {"fact_review_loop": FactReviewLoop}
    else:
        raise ValueError(f"未知 mode: {mode!r}（支持 'submodule' / 'detailed'）")
    loader = TemplateLoader()
    loader.register(template_name, template)
    mod = Module(
        spec=spec,
        template_name=template_name,
        template_loader=loader,
        llm_client=llm_client,
        registry=_build_registry(llm_client, mode),
        modules=modules,
        review_harness=None,
        persist=persist,
        status_file=persist,
    )
    return mod.run(max_ticks=max_ticks)
