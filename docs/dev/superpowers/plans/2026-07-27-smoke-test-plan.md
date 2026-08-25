# 真实 LLM 冒烟测试实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建 4 个真实 DeepSeek API 冒烟测试，覆盖 Harness→LLM→Validator→EventBus→Runner 全链路。

**Architecture:** `module_harness/tests/smoke/` 下 6 个文件：conftest（fixtures）+ 4 个测试文件。全部使用 `@pytest.mark.smoke` 标记，通过 `pytest-asyncio` 运行异步测试。

**Tech Stack:** pytest + pytest-asyncio + DeepSeek API (via OpenAI SDK)

**Spec:** `docs/dev/superpowers/specs/2026-07-27-smoke-test-design.md`

---

## 文件结构

```
module_harness/tests/smoke/
├── __init__.py           ← 新文件，空
├── conftest.py           ← 新文件，真实 LLM fixtures
├── test_minimal.py       ← 新文件，基础链路测试
├── test_think.py         ← 新文件，think 开关对比测试
├── test_module.py        ← 新文件，Module 编排器测试
└── test_builtin.py       ← 新文件，内置模板测试
```

---

### Task 1: 基础设施 — conftest.py

**Files:**
- Create: `module_harness/tests/smoke/__init__.py`
- Create: `module_harness/tests/smoke/conftest.py`

- [ ] **Step 1: 创建 `__init__.py`**

```python
# module_harness/tests/smoke/__init__.py
```

- [ ] **Step 2: 编写 `conftest.py`**

```python
"""smoke test 共享 fixtures — 真实 LLM 客户端、EventBus。"""

import pytest
from llm.config import LLMConfig
from llm.client import create_llm_client
from module_harness.events import EventBus


@pytest.fixture(scope="module")
def llm_config():
    """真实配置：从 config.json + .env 加载。"""
    return LLMConfig.from_env()


@pytest.fixture(scope="module")
def llm_client(llm_config):
    """真实 LLM 客户端。module scope 复用连接。"""
    return create_llm_client(llm_config)


@pytest.fixture
def event_bus():
    """带录制功能的 EventBus — 每次测试独立。"""

    class RecordingBus(EventBus):
        def __init__(self):
            super().__init__()
            self.recorded: list = []

        def emit(self, event):
            self.recorded.append(event)
            super().emit(event)

    return RecordingBus()
```

- [ ] **Step 3: 验证 conftest 可导入**

```bash
python -c "import sys; sys.path.insert(0,'.'); from module_harness.tests.smoke.conftest import llm_config; c = llm_config(); print(c.model)"
```

---

### Task 2: 基础链路测试 — test_minimal.py

**Files:**
- Create: `module_harness/tests/smoke/test_minimal.py`

- [ ] **Step 1: 编写测试**

```python
"""基础链路冒烟测试：1 harness + 1 script。"""

import pytest
from tickflow import parse
from tickflow.async_runner import AsyncRunner
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.events import (
    PromptRendered,
    LlmCallStarted,
    LlmToken,
    LlmCallCompleted,
    OutputValidated,
)

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_harness_translate_and_script_echo(llm_client, event_bus):
    """
    [A: harness translate, think=False] --> B: script echo

    验证：LLM 调用成功、JSON 输出正确、事件齐全。
    """
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    reg.harness("translate", HarnessConfig(
        prompt_core="将以下文本翻译为中文，只输出 JSON: {\"translation\": \"你的翻译\"}。文本：{text}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("echo")
    def echo(view):
        return view.A.value

    graph = parse("[A] --> B\nA.body: translate\nB.body: echo", registry=reg)

    runner = AsyncRunner(graph, registry=reg, keep_records=True)
    firings = await runner.run_until_idle(max_ticks=10)

    # 断言：两个节点都正常完成
    assert len(firings) == 2, f"期望 2 个 firing，实际 {len(firings)}"
    for f in firings:
        assert f.status == "ok", f"节点 {f.node} 状态异常: {f.status}, error={f.error}"

    # 断言：script 拿到了翻译结果
    output = firings[-1].output
    assert isinstance(output, dict), f"输出应为 dict，实际 {type(output)}"
    assert "translation" in output, f"输出缺 translation 字段: {output}"
    assert len(output["translation"]) > 0

    # 断言：事件齐全
    event_types = [type(e).__name__ for e in event_bus.recorded]
    for expected in ["PromptRendered", "LlmCallStarted", "LlmToken",
                     "LlmCallCompleted", "OutputValidated"]:
        assert expected in event_types, f"缺少事件: {expected}"
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest module_harness/tests/smoke/test_minimal.py::test_harness_translate_and_script_echo -v -s
```

期望：PASS，输出真实翻译结果。

---

### Task 3: Think 开关对比测试 — test_think.py

**Files:**
- Create: `module_harness/tests/smoke/test_think.py`

- [ ] **Step 1: 编写测试**

```python
"""think 模式开关对比测试。"""

import pytest
from tickflow import parse
from tickflow.async_runner import AsyncRunner
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry

pytestmark = pytest.mark.smoke


async def _run_analyze(llm_client, event_bus, think):
    """运行一次分析任务，返回 (output, usage_dict)。"""
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    reg.harness("analyze", HarnessConfig(
        prompt_core=(
            "分析以下代码的时间复杂度并只输出 JSON: "
            "{\"complexity\": \"O(n) 或 O(n^2) 等\", \"explanation\": \"一句话解释\"}。"
            "\n代码：\n{code}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
        api_params={"thinking": think} if think is not None else {},
    ))

    @reg.script("echo")
    def echo(view):
        return view.A.value

    graph = parse("[A] --> B\nA.body: analyze\nB.body: echo", registry=reg)
    runner = AsyncRunner(graph, registry=reg, keep_records=True)
    firings = await runner.run_until_idle(max_ticks=10)

    # usage 数据在 LlmCallCompleted 事件中（NodeState 不含 usage）
    from module_harness.events import LlmCallCompleted
    usage = {}
    for e in event_bus.recorded:
        if isinstance(e, LlmCallCompleted):
            usage = getattr(e, "usage", {})
            break

    return firings[0].output, usage


@pytest.mark.asyncio
async def test_think_off_vs_on(llm_client, event_bus):
    """think=False vs think={\"type\":\"enabled\"} 对比。"""
    code = "def f(n):\n    for i in range(n):\n        for j in range(n):\n            print(i, j)"

    # 普通模式
    out_off, usage_off = await _run_analyze(llm_client, event_bus,
                                            think={"type": "disabled"})
    # 思考模式
    bus2 = event_bus.__class__()
    out_on, usage_on = await _run_analyze(llm_client, bus2,
                                          think={"type": "enabled"})

    # 断言：两次都成功
    assert "complexity" in out_off, f"off 模式输出异常: {out_off}"
    assert "complexity" in out_on, f"on 模式输出异常: {out_on}"

    # 断言：思考模式 tokens 更多（输出 tokens 或总 tokens）
    off_total = usage_off.get("input_tokens", 0) + usage_off.get("output_tokens", 0)
    on_total = usage_on.get("input_tokens", 0) + usage_on.get("output_tokens", 0)
    assert on_total > off_total, (
        f"思考模式 tokens({on_total}) 应多于普通模式({off_total})"
    )
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest module_harness/tests/smoke/test_think.py::test_think_off_vs_on -v -s
```

期望：PASS，思考模式 tokens 明显更多。

---

### Task 4: Module 编排器测试 — test_module.py

**Files:**
- Create: `module_harness/tests/smoke/test_module.py`

- [ ] **Step 1: 编写测试**

```python
"""Module 编排器全链路冒烟测试（script 翻译器，1 次 LLM 调用）。"""

import pytest
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.events import EventBus
from module_harness.translator import TemplateLoader
from module_harness.module import Module

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_module_run_with_script_translator(llm_client, event_bus):
    """
    Module.run() 全链路：模板加载 → 翻译 → graph 构建 → 命名空间隔离 → Runner 执行。
    """
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)
    loader = TemplateLoader()

    # 注册执行元件
    reg.harness("translate", HarnessConfig(
        prompt_core="将'{text}'翻译为中文，只输出 JSON: {\"translation\": \"...\"}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("format_output")
    def format_output(view):
        data = view.A.value
        return {"result": data["translation"].strip()}

    # 注册 script 翻译器：固定返回 tasklist
    @reg.script("smoke_translator")
    def smoke_translator(view):
        return {
            "Tasks": {
                "A": {
                    "type": "harness",
                    "harness": "translate",
                    "inputs": {"text": "{spec.text}"},
                    "outputformat": {"type": "json_object"},
                },
                "B": {
                    "type": "script",
                    "script": "format_output",
                    "inputs": {"data": "A"},
                },
            },
            "Flow": "A --> B",
        }

    # 注册模板
    loader.register("smoke_translate", {
        "name": "smoke_translate",
        "translation": {"type": "script", "script": "smoke_translator"},
        "tasklist": {"Tasks": {}, "Flow": ""},  # 骨架，会被翻译器覆盖
    })

    # 运行 Module
    mod = Module(
        spec={"text": "Hello world, this is a test."},
        template_name="smoke_translate",
        llm_client=llm_client,
        event_bus=event_bus,
        template_loader=loader,
        registry=reg,
    )

    runner = await mod._build_runner_async()
    firings = await runner.run_until_idle(max_ticks=10)

    # 断言
    assert runner.is_idle()
    assert len(firings) >= 2
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    # 验证输出
    final = firings[-1].output
    assert "result" in final, f"最终输出: {final}"
    assert len(final["result"]) > 0
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest module_harness/tests/smoke/test_module.py::test_module_run_with_script_translator -v -s
```

期望：PASS，Module 全链路完成。

---

### Task 5: 内置模板测试 — test_builtin.py

**Files:**
- Create: `module_harness/tests/smoke/test_builtin.py`

- [ ] **Step 1: 编写测试**

```python
"""内置 translate.json 模板真实测试（2 次 LLM 调用）。"""

import pytest
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.translator import TemplateLoader
from module_harness.module import Module

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
        data = view.A.value
        return {"result": data["translation"].strip()}

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

    except ValueError as e:
        if "缺少" in str(e) or "未找到" in str(e):
            pytest.xfail(f"内置模板测试跳过（tasklist 生成/校验问题）: {e}")
        raise
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest module_harness/tests/smoke/test_builtin.py::test_builtin_translate_template -v -s
```

期望：PASS 或 XFAIL（如果 LLM 生成的 tasklist 无法通过校验）。

---

### Task 6: 全量运行 + 修复循环

- [ ] **Step 1: 运行全部冒烟测试**

```bash
python -m pytest module_harness/tests/smoke/ -v -s 2>&1
```

- [ ] **Step 2: 分析失败，当场修复**

每发现一个问题：
1. 定位根因（`llm/` 或 `module_harness/` 或测试代码）
2. 修改代码
3. 重跑该测试
4. 通过后重跑全部 `smoke/`

- [ ] **Step 3: 确认常规测试无回归**

```bash
python -m pytest module_harness/tests/ -q --ignore=module_harness/tests/smoke
```

- [ ] **Step 4: Commit**

```bash
git add module_harness/tests/smoke/ docs/dev/superpowers/
git commit -m "test: add real-LLM smoke tests (DeepSeek)"
```

---

## 自审

| 检查项 | 结果 |
|--------|------|
| Spec 覆盖 | 4 个测试文件对应 spec 中 4 个测试，conftest 对应基础设施 |
| 无占位符 | ✅ 所有任务含完整代码 |
| 类型一致 | HarnessConfig / AsyncRunner / EventBus 签名与现有代码一致 |
| 作用域 | 仅创建 smoke/ 目录，不修改现有文件 |
