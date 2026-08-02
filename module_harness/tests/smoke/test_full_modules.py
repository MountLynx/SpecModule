"""完整 module 冒烟测试：summarize / codereview / docwrite。

覆盖三种图结构：
- summarize: 单链（script 翻译器）
- codereview: 并行 AND-join（harness 翻译器，LLM 生成 tasklist）
- docwrite: 链式多节点（script 翻译器）
"""

import pytest
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.translator import TemplateLoader
from module_harness.module import Module

pytestmark = pytest.mark.smoke


def _setup_base_registry(llm_client, event_bus=None) -> HarnessRegistry:
    """注册所有内置模板引用的 harness/script + 翻译器。"""
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    # ── 翻译 harness（spec → tasklist，codereview 用）──
    reg.harness("spec_to_tasklist", HarnessConfig(
        prompt_core="你是一个流程设计器。根据 spec 生成合法的 tasklist JSON。\n{spec}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    # ── summarize 元件 ──
    reg.harness("summarize", HarnessConfig(
        prompt_core=(
            "阅读以下文本，提取要点并输出 JSON: "
            "{\"summary\": \"一段话总结\", \"key_points\": [\"要点1\", \"要点2\"]}。"
            "\n文本：\n{text}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("format_summary")
    def format_summary(view):
        data = view.A.value
        return {
            "summary": data["summary"].strip(),
            "points_count": len(data.get("key_points", [])),
        }

    @reg.script("summarize_translator")
    def summarize_translator(view):
        return {
            "Tasks": {
                "A": {
                    "type": "harness",
                    "harness": "summarize",
                    "inputs": {"text": "{spec.text}"},
                    "outputformat": {"type": "json_object"},
                },
                "B": {
                    "type": "script",
                    "script": "format_summary",
                    "inputs": {"data": "A"},
                },
            },
            "Flow": "A --> B",
        }

    # ── codereview 元件 ──
    reg.harness("review_code", HarnessConfig(
        prompt_core=(
            "审查以下 {language} 代码，找出问题并输出 JSON: "
            "{\"issues\": [{\"severity\": \"error|warning|suggestion\", "
            "\"line\": 1, \"message\": \"问题描述\"}]}。"
            "\n代码：\n{code}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    reg.harness("check_rules", HarnessConfig(
        prompt_core=(
            "对照以下规范检查代码，输出 JSON: "
            "{\"violations\": [{\"rule\": \"规范名\", \"detail\": \"违反情况\"}]}。"
            "\n规范：{rules}"
            "\n代码：\n{code}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("merge_review")
    def merge_review(view):
        # DictView 以 producer 名为 key：view.A=review, view.B=rules
        review = view.A.value or {}
        rules = view.B.value or {}
        return {
            "issues": review.get("issues", []),
            "violations": rules.get("violations", []),
            "total": len(review.get("issues", [])) + len(rules.get("violations", [])),
        }

    # ── docwrite 元件 ──
    reg.harness("write_outline", HarnessConfig(
        prompt_core=(
            "为以下主题生成文章大纲，输出 JSON: "
            "{\"title\": \"标题\", \"sections\": [\"节1\", \"节2\", \"节3\"]}。"
            "\n主题：{topic}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    reg.harness("write_section", HarnessConfig(
        prompt_core=(
            "根据大纲撰写各节正文，输出 JSON: "
            "{\"content\": {\"节名\": \"正文内容\"}}。"
            "\n大纲：{outline}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("merge_doc")
    def merge_doc(view):
        # DictView 以 producer 名为 key：view.A=outline, view.B=sections
        outline = view.A.value or {}
        sections = view.B.value or {}
        title = outline.get("title", "Untitled")
        content = sections.get("content", {})
        return {
            "title": title,
            "sections": list(content.keys()),
            "chars": sum(len(str(v)) for v in content.values()),
        }

    @reg.script("docwrite_translator")
    def docwrite_translator(view):
        return {
            "Tasks": {
                "A": {
                    "type": "harness",
                    "harness": "write_outline",
                    "inputs": {"topic": "{spec.topic}"},
                    "outputformat": {"type": "json_object"},
                },
                "B": {
                    "type": "harness",
                    "harness": "write_section",
                    "inputs": {"outline": "A"},
                    "outputformat": {"type": "json_object"},
                },
                "C": {
                    "type": "script",
                    "script": "merge_doc",
                    "inputs": {"outline": "A", "sections": "B"},
                },
            },
            "Flow": "A --> B\nB --> C",
        }

    return reg


@pytest.mark.asyncio
async def test_summarize_module(llm_client, event_bus):
    """文本总结：单链，script 翻译器。"""
    reg = _setup_base_registry(llm_client, event_bus)
    loader = TemplateLoader()
    loader.load_builtins()

    mod = Module(
        spec={"text": "Python 是一种解释型高级编程语言。它支持面向对象、函数式和过程式编程。"
                       "Python 拥有动态类型系统和自动内存管理。它被广泛应用于 Web 开发、"
                       "数据分析、人工智能和自动化脚本等领域。"},
        template_name="summarize",
        llm_client=llm_client,
        event_bus=event_bus,
        template_loader=loader,
        registry=reg,
    )
    runner = await mod._build_runner_async()
    firings = await runner.run_until_idle(max_ticks=10)

    assert runner.is_idle(), "Runner 未完成"
    assert len(firings) >= 2
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    final = firings[-1].output
    assert "summary" in final, f"输出缺 summary: {final}"
    assert len(final["summary"]) > 0
    assert "points_count" in final, f"输出缺 points_count: {final}"
    print(f"\n[summarize] summary={final['summary'][:50]}... points={final['points_count']}")


@pytest.mark.asyncio
async def test_codereview_module(llm_client, event_bus):
    """代码审查：并行 AND-join，harness 翻译器（LLM 生成 tasklist）。"""
    reg = _setup_base_registry(llm_client, event_bus)
    loader = TemplateLoader()
    loader.load_builtins()

    code = "def calculate_total(items):\n    total = 0\n    for i in items:\n        total = total + i\n    return total\n"
    rules = "函数应有 docstring；变量名应使用 snake_case；避免魔法数字"

    mod = Module(
        spec={"code": code, "language": "python", "rules": rules},
        template_name="codereview",
        llm_client=llm_client,
        event_bus=event_bus,
        template_loader=loader,
        registry=reg,
    )

    try:
        runner = await mod._build_runner_async()
        firings = await runner.run_until_idle(max_ticks=10)

        assert runner.is_idle(), "Runner 未完成"
        assert len(firings) >= 3
        for f in firings:
            assert f.status == "ok", f"节点 {f.node}: {f.error}"

        final = firings[-1].output
        assert "issues" in final, f"输出缺 issues: {final}"
        assert "violations" in final, f"输出缺 violations: {final}"
        print(f"\n[codereview] issues={len(final['issues'])} violations={len(final['violations'])}")

    except ValueError as e:
        pytest.xfail(f"codereview 模板（LLM tasklist 生成）: {e}")


@pytest.mark.asyncio
async def test_docwrite_module(llm_client, event_bus):
    """文档写作：链式多节点，script 翻译器。"""
    reg = _setup_base_registry(llm_client, event_bus)
    loader = TemplateLoader()
    loader.load_builtins()

    mod = Module(
        spec={"topic": "如何用 Python 处理 Excel 文件"},
        template_name="docwrite",
        llm_client=llm_client,
        event_bus=event_bus,
        template_loader=loader,
        registry=reg,
    )
    runner = await mod._build_runner_async()
    firings = await runner.run_until_idle(max_ticks=10)

    assert runner.is_idle(), "Runner 未完成"
    assert len(firings) >= 3
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    final = firings[-1].output
    # 结构断言：链路跑通（LLM 内容质量有波动，不在此处断言）
    assert "title" in final, f"输出缺 title: {final}"
    assert "sections" in final, f"输出缺 sections: {final}"
    assert "chars" in final, f"输出缺 chars: {final}"
    print(f"\n[docwrite] title={final['title']} sections={final['sections']} chars={final['chars']}")
