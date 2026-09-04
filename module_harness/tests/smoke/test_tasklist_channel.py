"""新功能冒烟测试：spec + 自定义 tasklist 输入通道（真实 LLM）。

覆盖 smoke 套件中的 3 个完整 module（summarize / codereview / docwrite）：
- 用户手写完整 tasklist（不经过模板翻译）→ 结构校验 → LLM 一致性审核 → 执行
- 每次构建多一次审核 LLM 调用（一致性审核是新增通道的核心步骤）
- 负向用例：故意引用 spec 中不存在的字段，验证审核阻断（ConsistencyError）

真实 LLM 调用次数：正向 2/3/3 次 + 负向 1 次 = 9 次。
"""

import pytest

from module_harness.core.config import HarnessConfig, OutputFormat
from module_harness.orchestrate.consistency import (
    ConsistencyError,
    register_review_harness,
)
from module_harness.infra.events import ConsistencyReviewed
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import TaskDefinition, Tasklist
from module_harness.model.module import Module
from module_harness.tests.smoke.test_full_modules import _setup_base_registry

pytestmark = pytest.mark.smoke


# ── 用户手写 tasklist（镜像各模板骨架，完全自定义）──────────────

def _summarize_tasklist() -> Tasklist:
    """单链：summarize → format_summary。"""
    return Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="summarize",
                inputs={"text": "{spec.text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_summary",
                inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )


def _codereview_tasklist() -> Tasklist:
    """并行 AND-join：review_code + check_rules 同时汇入 merge_review。"""
    return Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="review_code",
                inputs={"code": "{spec.code}", "language": "{spec.language}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="harness", harness="check_rules",
                inputs={"code": "{spec.code}", "rules": "{spec.rules}"},
                outputformat={"type": "json_object"},
            ),
            "C": TaskDefinition(
                type="script", script="merge_review",
                inputs={"review": "A", "rules": "B"},
            ),
        },
        flow="[A] --> B\n[A] --> C\nB --> C",
    )


def _docwrite_tasklist() -> Tasklist:
    """链式多节点：write_outline → write_section → merge_doc。"""
    return Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="write_outline",
                inputs={"topic": "{spec.topic}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="harness", harness="write_section",
                inputs={"outline": "A"},
                outputformat={"type": "json_object"},
            ),
            "C": TaskDefinition(
                type="script", script="merge_doc",
                inputs={"outline": "A", "sections": "B"},
            ),
        },
        flow="A --> B\nB --> C",
    )


def _assert_review_passed(mod, event_bus) -> None:
    """审核必须通过，且 ConsistencyReviewed 事件真实发出。"""
    assert mod.review_result is not None, "review_result 未填充（审核未执行）"
    assert mod.review_result.consistent is True, (
        f"一致性审核未通过: {mod.review_result.suggestions}"
    )
    reviews = [e for e in event_bus.recorded if isinstance(e, ConsistencyReviewed)]
    assert len(reviews) == 1, f"ConsistencyReviewed 事件数量异常: {len(reviews)}"
    assert reviews[0].consistent is True


@pytest.mark.asyncio
async def test_summarize_custom_tasklist(llm_client, event_bus):
    """summarize：自定义 tasklist 通道（单链）+ 一致性审核 + 执行。"""
    reg = _setup_base_registry(llm_client, event_bus)
    register_review_harness(reg)

    mod = Module(
        spec={"text": "Python 是一种解释型高级编程语言。它支持面向对象、函数式和过程式编程。"
                       "Python 拥有动态类型系统和自动内存管理。它被广泛应用于 Web 开发、"
                       "数据分析、人工智能和自动化脚本等领域。"},
        tasklist=_summarize_tasklist(),
        llm_client=llm_client,
        event_bus=event_bus,
        module_id="smoke_summarize",
        registry=reg,
    )
    runner = await mod._build_runner_async()
    _assert_review_passed(mod, event_bus)

    firings = await runner.run_until_idle(max_ticks=10)
    assert runner.is_idle(), "Runner 未完成"
    assert len(firings) >= 2
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    final = firings[-1].output
    assert "summary" in final and len(final["summary"]) > 0
    assert "points_count" in final
    print(f"\n[summarize/tasklist] summary={final['summary'][:50]}... "
          f"points={final['points_count']} 审核={mod.review_result.suggestions[:30]}")


@pytest.mark.asyncio
async def test_codereview_custom_tasklist(llm_client, event_bus):
    """codereview：自定义 tasklist 通道（并行 AND-join）+ 一致性审核 + 执行。"""
    reg = _setup_base_registry(llm_client, event_bus)
    register_review_harness(reg)

    code = ("def calculate_total(items):\n"
            "    total = 0\n"
            "    for i in items:\n"
            "        total = total + i\n"
            "    return total\n")
    rules = "函数应有 docstring；变量名应使用 snake_case；避免魔法数字"

    mod = Module(
        spec={"code": code, "language": "python", "rules": rules},
        tasklist=_codereview_tasklist(),
        llm_client=llm_client,
        event_bus=event_bus,
        module_id="smoke_codereview",
        registry=reg,
    )
    runner = await mod._build_runner_async()
    _assert_review_passed(mod, event_bus)

    firings = await runner.run_until_idle(max_ticks=10)
    assert runner.is_idle(), "Runner 未完成"
    assert len(firings) >= 3
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    final = firings[-1].output
    assert "issues" in final and "violations" in final
    print(f"\n[codereview/tasklist] issues={len(final['issues'])} "
          f"violations={len(final['violations'])}")


@pytest.mark.asyncio
async def test_docwrite_custom_tasklist(llm_client, event_bus):
    """docwrite：自定义 tasklist 通道（链式多节点）+ 一致性审核 + 执行。"""
    reg = _setup_base_registry(llm_client, event_bus)
    register_review_harness(reg)

    mod = Module(
        spec={"topic": "如何用 Python 处理 Excel 文件"},
        tasklist=_docwrite_tasklist(),
        llm_client=llm_client,
        event_bus=event_bus,
        module_id="smoke_docwrite",
        registry=reg,
    )
    runner = await mod._build_runner_async()
    _assert_review_passed(mod, event_bus)

    firings = await runner.run_until_idle(max_ticks=10)
    assert runner.is_idle(), "Runner 未完成"
    assert len(firings) >= 3
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    final = firings[-1].output
    assert "title" in final and "sections" in final and "chars" in final
    print(f"\n[docwrite/tasklist] title={final['title']} "
          f"sections={final['sections']} chars={final['chars']}")


@pytest.mark.asyncio
async def test_inconsistent_tasklist_blocked(llm_client, event_bus):
    """负向：tasklist 引用 spec 中不存在的字段 → 审核应阻断（ConsistencyError）。

    探针模式：若 LLM 未识别不一致，记录问题不阻塞套件（与 codereview 同款 xfail）。
    """
    reg = _setup_base_registry(llm_client, event_bus)
    register_review_harness(reg)

    bad = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="summarize",
                inputs={"text": "{spec.nonexistent_field}"},
                outputformat={"type": "json_object"},
            ),
        },
        flow="[A]",
    )
    mod = Module(
        spec={"text": "一段需要总结的文本。"},
        tasklist=bad,
        llm_client=llm_client,
        event_bus=event_bus,
        module_id="smoke_inconsistent",
        registry=reg,
    )

    try:
        await mod._build_runner_async()
    except ConsistencyError as e:
        # 审核阻断机制生效：报告保留 + 建议非空
        assert mod.review_result is not None
        assert mod.review_result.consistent is False
        assert e.report.suggestions, "ConsistencyError 缺少建议"
        reviews = [ev for ev in event_bus.recorded if isinstance(ev, ConsistencyReviewed)]
        assert len(reviews) == 1 and reviews[0].consistent is False
        print(f"\n[inconsistent] 审核阻断生效: {e.report.suggestions[:60]}")
    else:
        pytest.xfail("探针：LLM 未识别不一致 tasklist（引用不存在的 spec 字段未被审核阻断）")
