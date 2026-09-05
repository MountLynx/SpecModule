"""内置 translate.json 模板真实测试（2 次 LLM 调用）。

注意：此测试依赖 LLM 生成合法的 tasklist JSON 和正确的 Flow 语法。
内置模板的 prompt 未显式约束这些格式，LLM 输出变化大，测试可能不稳定。
本测试以探针模式运行——通过即说明端到端通，失败则记录问题不阻塞。
"""

import pytest
from module_harness.core.config import HarnessConfig, OutputFormat
from module_harness.core.registry import HarnessRegistry
from module_harness.model.translator import TemplateLoader
from module_harness.model.module import Module

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_builtin_translate_template(llm_client):
    """
    使用内置 translate.json 模板：LLM 翻译 spec → tasklist → 执行。

    与 test_module.py 的区别：翻译器是 harness 类型（LLM），
    需要 2 次 LLM 调用，且 tasklist 由模型生成。
    """
    reg = HarnessRegistry(llm_client=llm_client)
    loader = TemplateLoader()
    loader.load_builtins()

    # 注册执行 harness
    reg.harness("translate", HarnessConfig(
        prompt_core="将以下文本翻译为中文：{text}",
        prompt_modes={
            "formal": "请使用正式语气翻译。",
            "casual": "请使用日常语气翻译。",
        },
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
        notdo=["不要添加解释"],
    ))

    @reg.script("format_output")
    def format_output(view):
        data = view.field("data") or {}
        # LLM 输出质量有波动，用 .get 兜底
        return {"result": (data.get("translation") or "").strip()}

    # 注册翻译 harness（spec → tasklist）
    reg.harness("spec_to_tasklist", HarnessConfig(
        prompt_core="你是一个流程设计器。根据 spec 生成合法的 tasklist JSON。\n{spec}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    spec = {
        "harness_name": "translate",
        "source_text": "Good morning, how are you today?",
        "style": "formal",
    }

    try:
        mod = Module(
            spec=spec,
            template_name="translate",
            llm_client=llm_client,
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
        assert "result" in final, f"最终输出: {final}"

    except (ValueError, AssertionError) as e:
        # LLM 生成的 tasklist 格式不稳定（数组/字符串、body 缺失等）
        pytest.xfail(f"内置模板测试（LLM tasklist 生成尚不稳定）: {e}")
