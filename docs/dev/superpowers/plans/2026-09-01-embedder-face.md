# 嵌入者消费面实施计划（call_harness task 级地板）

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地嵌入者消费面——task 级 API 地板 `call_harness`、ConsistencyReviewer 瘦身、嵌入者 import 契约标注、嵌入指南补全（spec: `docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md`）。

**Architecture:** 新文件 `module_harness/call.py` 提供自由函数 `call_harness(config, values, *, llm_client, ...)`，内部零新执行语义（复用 `Harness.build_body` + 一次 body 调用），Failure 统一翻译为类型化 `HarnessCallError`。`ConsistencyReviewer.review()` 重构为其消费者。文档与导出面随行。

**Tech Stack:** Python 3.13、pytest + pytest-asyncio（显式 `@pytest.mark.asyncio` 标记）+ `unittest.mock`（`MagicMock`/`AsyncMock`）、tickflow（`Failure`/`DictView`/`Resolved`）。测试命令一律 `python -m pytest module_harness/tests/ -q`（在仓库根目录运行）。

---

## File Structure

| 文件 | 动作 | 职责 |
|------|------|------|
| `module_harness/call.py` | 新建 | task 级地板：`HarnessCallResult` / `HarnessCallError` / `call_harness`（~70 行，单一职责） |
| `module_harness/tests/test_call.py` | 新建 | call_harness 全语义测试 + registry.llm_client + 公共导出断言 |
| `module_harness/registry.py` | 修改 | 加只读属性 `llm_client`（reviewer 瘦身硬前提） |
| `module_harness/consistency.py` | 修改 | `review()` 改走 `call_harness`，删仪式代码 |
| `module_harness/__init__.py` | 修改 | 导出 call 面 + 嵌入者最小面注释标注 |
| `docs/guides/embedding.md` | 修改 | 补「task 级调用」+「嵌入者分层纪律」两节 |
| `docs/dev/progress/module-roadmap.md` | 修改 | 三个 checkbox 勾掉 + 红线加 spec 指针 + 日期行 |
| `examples/embed_minimal/main.py` | 修改 | 加 task 级 `--mock` 冒烟段 |

任务顺序：1 call.py（TDD）→ 2 registry 属性（TDD）→ 3 reviewer 瘦身（既有测试保绿）→ 4 导出面（TDD）→ 5 文档 → 6 示例冒烟 + 全量验收。

---

### Task 1: `call.py` — task 级 API 地板

**Files:**
- Create: `module_harness/call.py`
- Test: `module_harness/tests/test_call.py`

- [ ] **Step 1: 写失败测试（完整测试文件）**

创建 `module_harness/tests/test_call.py`：

```python
# module_harness/tests/test_call.py
"""call_harness — task 级 API 地板：独立调用 harness（嵌入者消费面）。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMError, LLMResponse

from module_harness.call import HarnessCallError, HarnessCallResult, call_harness
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.events import EventBus, OutputValidated, PromptRendered
from module_harness.registry import HarnessRegistry


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestCallHarnessText:
    @pytest.mark.asyncio
    async def test_text_value_equals_raw(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="你好", usage={"input_tokens": 1}, finish_reason="end_turn",
        )
        result = await call_harness(
            HarnessConfig(prompt_core="翻译：{text}"),
            {"text": "hello"},
            llm_client=mock_llm,
        )
        assert isinstance(result, HarnessCallResult)
        assert result.value == "你好"
        assert result.raw == "你好"
        assert result.usage == {"input_tokens": 1}

    @pytest.mark.asyncio
    async def test_values_rendered_into_prompt(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="x", usage={}, finish_reason="end_turn",
        )
        await call_harness(
            HarnessConfig(prompt_core="翻译：{text}"),
            {"text": "hello"},
            llm_client=mock_llm,
        )
        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert prompt == "翻译：hello"


class TestCallHarnessJson:
    @pytest.mark.asyncio
    async def test_json_value_parsed_raw_literal(self, mock_llm):
        fenced = '```json\n{"a": 1}\n```'
        mock_llm.complete.return_value = LLMResponse(
            content=fenced, usage={}, finish_reason="end_turn",
        )
        result = await call_harness(
            HarnessConfig(
                prompt_core="输出 JSON：{x}",
                output_format=OutputFormat(type="json_object"),
            ),
            {"x": "1"},
            llm_client=mock_llm,
        )
        assert result.value == {"a": 1}   # 校验+提取后的解析值
        assert result.raw == fenced       # 原始输出（审计链）

    @pytest.mark.asyncio
    async def test_validation_failure_raises_with_chain(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="not json", usage={}, finish_reason="end_turn",
        )
        with pytest.raises(HarnessCallError) as exc_info:
            await call_harness(
                HarnessConfig(
                    prompt_core="P：{x}",
                    output_format=OutputFormat(type="json_object"),
                ),
                {"x": "1"},
                llm_client=mock_llm,
            )
        err = exc_info.value
        assert err.failure is not None
        assert err.failure.type == "llm"
        assert err.prompt == "P：1"
        assert err.raw == "not json"

    @pytest.mark.asyncio
    async def test_llm_error_infrastructure(self, mock_llm):
        mock_llm.complete.side_effect = LLMError("API 不可用")
        with pytest.raises(HarnessCallError) as exc_info:
            await call_harness(
                HarnessConfig(prompt_core="P"),
                {},
                llm_client=mock_llm,
            )
        err = exc_info.value
        assert err.failure.type == "infrastructure"
        assert err.raw is None
        assert "API 不可用" in str(err)


class TestCallHarnessPromptmode:
    @pytest.mark.asyncio
    async def test_promptmode_renders(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="x", usage={}, finish_reason="end_turn",
        )
        await call_harness(
            HarnessConfig(prompt_core="P：{x}", prompt_modes={"formal": "正式语域"}),
            {"x": "1"},
            llm_client=mock_llm,
            promptmode="formal",
        )
        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert "正式语域" in prompt

    @pytest.mark.asyncio
    async def test_promptmode_missing_key_raises_keyerror(self, mock_llm):
        """缺 promptmode key → KeyError 原样冒出（框架不猜）。"""
        with pytest.raises(KeyError):
            await call_harness(
                HarnessConfig(prompt_core="P", prompt_modes={"formal": "正式"}),
                {},
                llm_client=mock_llm,
                promptmode="casual",
            )


class TestCallHarnessEvents:
    @pytest.mark.asyncio
    async def test_events_collected_when_bus_passed(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="hi", usage={}, finish_reason="end_turn",
        )
        bus = EventBus()
        seen = []
        bus.subscribe(PromptRendered, lambda e: seen.append(e))
        bus.subscribe(OutputValidated, lambda e: seen.append(e))
        await call_harness(
            HarnessConfig(prompt_core="P：{x}"),
            {"x": "1"},
            llm_client=mock_llm,
            event_bus=bus,
        )
        kinds = [type(e).__name__ for e in seen]
        assert "PromptRendered" in kinds
        assert "OutputValidated" in kinds
        assert all(e.node == "__call__" for e in seen)  # 保留字面量

    @pytest.mark.asyncio
    async def test_no_bus_zero_cost(self, mock_llm):
        """不传 bus → EventBus.null()，静默零开销。"""
        mock_llm.complete.return_value = LLMResponse(
            content="hi", usage={}, finish_reason="end_turn",
        )
        result = await call_harness(
            HarnessConfig(prompt_core="P"), {}, llm_client=mock_llm,
        )
        assert result.value == "hi"
```

（`TestRegistryLlmClient` 不在本任务——registry 属性是 Task 2。）

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest module_harness/tests/test_call.py -q`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'module_harness.call'`

- [ ] **Step 3: 实现 `call.py`（最小实现）**

创建 `module_harness/call.py`：

```python
# module_harness/call.py
"""task 级 API 地板 —— 独立调用 harness（嵌入者消费面）。

API 金字塔自此 task → graph → run：嵌入者一次函数调用即得 harness 节点的
全部执行语义（三层 prompt / 输出校验 / 事件），不经图与 run。
零新执行语义：内部即 Harness.build_body + 一次 body 调用，仅一份执行配方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tickflow import Failure
from tickflow.views import DictView, Resolved

from .config import HarnessConfig
from .events import EventBus
from .harness import Harness


@dataclass
class HarnessCallResult:
    """独立调用结果：校验后输出 + LLM 原始输出 + token 用量。"""

    value: Any  # 校验后的输出（json_object → 解析值；text → str）
    raw: str    # LLM 原始输出（审计链）
    usage: dict  # token 用量


class HarnessCallError(RuntimeError):
    """独立调用失败（LLM 错误 / 输出不合法）。异常即审计：携带诊断链。"""

    def __init__(
        self,
        failure: Failure,
        *,
        prompt: str | None = None,
        raw: str | None = None,
        usage: dict | None = None,
    ) -> None:
        self.failure = failure
        self.prompt = prompt
        self.raw = raw
        self.usage = usage
        super().__init__(failure.error)


async def call_harness(
    config: HarnessConfig,
    values: dict[str, Any],
    *,
    llm_client: Any,
    promptmode: str | None = None,
    prompt_extra: str | None = None,
    event_bus: EventBus | None = None,
) -> HarnessCallResult:
    """独立调用一个 harness：一次函数调用拿到校验后的输出。

    ``values``：prompt 占位符取值 {key: value}。task 层的占位符兜底就是它
    （无 spec_inputs / input_aliases —— 那些是图概念）。

    ``event_bus``：传则收全套 harness 事件（PromptRendered / LlmToken /
    OutputValidated / ...），不传零开销（EventBus.null()）。

    失败（LLM 错误 / 输出校验不通过）抛 HarnessCallError，携带 failure 与
    渲染 prompt / 原始输出 / usage 诊断链。task 层没有"下游跳过"概念，
    Failure 一律翻译为异常；promptmode 缺 key → KeyError 原样冒出。
    """
    bus = event_bus or EventBus.null()
    body = Harness(config, llm_client, bus).build_body(
        promptmode=promptmode,
        prompt_extra=prompt_extra,
    )
    state: dict[str, Any] = {}
    view = DictView(
        {key: Resolved(value=val, k=None) for key, val in values.items()},
        state=state,
        node="__call__",
    )
    result = await body(view)

    prompt = state.get("_prompt")
    raw = state.get("_llm_raw")
    usage = state.get("_usage")

    if isinstance(result, Failure):
        raise HarnessCallError(result, prompt=prompt, raw=raw, usage=usage)
    return HarnessCallResult(value=result, raw=raw, usage=usage)
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest module_harness/tests/test_call.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部通过，0 failed（新文件不影响既有面）

- [ ] **Step 6: Commit**

```bash
git add module_harness/call.py module_harness/tests/test_call.py
git commit -m "feat: call_harness task 级 API 地板——独立调用 harness，Failure 翻译为 HarnessCallError"
```

---

### Task 2: `HarnessRegistry.llm_client` 只读属性

**Files:**
- Modify: `module_harness/registry.py`
- Test: `module_harness/tests/test_call.py`（Task 1 已含 `TestRegistryLlmClient`，此刻应失败）

- [ ] **Step 1: 写失败测试（追加到 test_call.py 末尾）**

```python
class TestRegistryLlmClient:
    def test_readonly_accessor(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        assert reg.llm_client is mock_llm
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest module_harness/tests/test_call.py::TestRegistryLlmClient -q`
Expected: FAIL — `AttributeError: 'HarnessRegistry' object has no attribute 'llm_client'`

- [ ] **Step 3: 实现属性**

在 `module_harness/registry.py` 的「查询」段（`is_harness` 之前）加入：

```python
    @property
    def llm_client(self) -> Any:
        """注册表持有的 LLM 客户端（只读）。

        供不经图独立调用 harness 的场景使用（如 ConsistencyReviewer
        走 call_harness）。
        """
        return self._llm_client
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest module_harness/tests/test_call.py -q`
Expected: PASS（10 passed）

- [ ] **Step 5: Commit**

```bash
git add module_harness/registry.py module_harness/tests/test_call.py
git commit -m "feat: HarnessRegistry.llm_client 只读属性——独立调用场景取客户端"
```

---

### Task 3: ConsistencyReviewer 瘦身（改走 call_harness）

**Files:**
- Modify: `module_harness/consistency.py`
- Test: 既有 `module_harness/tests/test_consistency.py` 全程保绿（公共行为不变的证明）

- [ ] **Step 1: 建立绿色基线**

Run: `python -m pytest module_harness/tests/test_consistency.py -q`
Expected: 17 passed（基线必须全绿才开始重构）

- [ ] **Step 2: 重构 `review()`**

`module_harness/consistency.py` 全文替换为：

```python
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
```

要点：`Failure` / `DictView` / `Resolved` / `from typing import Any` 导入随之删除（不再使用）；`HarnessConfig` 仅剩 `REVIEW_HARNESS_CONFIG` 使用，保留。

- [ ] **Step 3: 运行既有测试验证仍绿**

Run: `python -m pytest module_harness/tests/test_consistency.py -q`
Expected: 17 passed（行为不变：全部既有断言不动一个字）

若 `test_review_raw_is_literal_llm_output` 之外的测试失败，先核对 `HarnessCallError → ValueError` 翻译文案（`match=` 前缀 "审核 harness 返回 Failure" 必须保留）。

- [ ] **Step 4: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部通过，0 failed

- [ ] **Step 5: Commit**

```bash
git add module_harness/consistency.py
git commit -m "refactor: ConsistencyReviewer 瘦身——手搓 DictView 仪式改走 call_harness"
```

---

### Task 4: 导出面 —— `__init__.py` + 嵌入者最小面标注

**Files:**
- Modify: `module_harness/__init__.py`
- Test: `module_harness/tests/test_call.py`（追加导出断言）

- [ ] **Step 1: 写失败测试（追加到 test_call.py 末尾）**

```python
class TestPublicExports:
    def test_call_face_exported(self):
        import module_harness

        assert module_harness.call_harness is not None
        assert module_harness.HarnessCallResult is not None
        assert module_harness.HarnessCallError is not None
        assert "call_harness" in module_harness.__all__
        assert "HarnessCallResult" in module_harness.__all__
        assert "HarnessCallError" in module_harness.__all__
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest module_harness/tests/test_call.py::TestPublicExports -q`
Expected: FAIL — `AttributeError: module 'module_harness' has no attribute 'call_harness'`

- [ ] **Step 3: 接线 `__init__.py`**

在 `module_harness/__init__.py`：

导入段（`from .harness import Harness` 一行之后）加：

```python
from .call import HarnessCallError, HarnessCallResult, call_harness
```

`__all__` 的 `# 核心` 段（`"Harness",` 之后）加：

```python
    # task 级 API 地板（嵌入者消费面，docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md）
    "HarnessCallResult",
    "HarnessCallError",
    "call_harness",
```

`__all__ = [` 行的上方加嵌入者最小面标注（注释即契约，不引入机器；正式冻结归 API 稳定化收口）：

```python
# 嵌入者最小面（用法见 docs/guides/embedding.md）：
#   task 级 = call_harness / HarnessCallResult / HarnessCallError
#   图级   = Module / HarnessRegistry + HarnessConfig / OutputFormat / EventBus
#             + register_builtin_harnesses
```

- [ ] **Step 4: 运行测试验证通过 + 全量回归**

Run: `python -m pytest module_harness/tests/test_call.py -q`
Expected: PASS（11 passed）

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部通过，0 failed

- [ ] **Step 5: Commit**

```bash
git add module_harness/__init__.py module_harness/tests/test_call.py
git commit -m "feat: 导出 call 面 + __all__ 标注嵌入者最小面"
```

---

### Task 5: 文档 —— embedding.md 两节 + roadmap 回写

**Files:**
- Modify: `docs/guides/embedding.md`
- Modify: `docs/dev/progress/module-roadmap.md`

- [ ] **Step 1: embedding.md 追加两节（文件末尾）**

在 `docs/guides/embedding.md` 的「与 submodule 嵌入的区别」一节之前插入：

````markdown
## task 级调用：一次函数调用 LLM 任务

嵌入者最小价值单位是**一次函数调用**，不是 run——单次结构化 LLM 任务（翻译 / 抽取 /
审核）不必建图：

```python
import asyncio
from module_harness import HarnessConfig, OutputFormat, call_harness
from llm import LLMConfig, create_llm_client

client = create_llm_client(LLMConfig.from_env())

result = asyncio.run(call_harness(
    HarnessConfig(
        prompt_core='从下列文本提取 JSON：{"translation": "..."}\n{text}',
        output_format=OutputFormat(type="json_object"),
    ),
    {"text": "Hello world"},
    llm_client=client,   # 显式必传：与 Module / HarnessRegistry 同一注入哲学
))
result.value   # 校验 + 自动提取后的解析值（json_object → dict）
result.raw     # LLM 原始输出（审计链）
result.usage   # token 用量
```

失败（LLM 错误 / 输出不合法）抛 `HarnessCallError`，携带 `failure / prompt / raw /
usage` 诊断链（异常即审计）。`promptmode` 传了但配置里没有该 key → `KeyError`
原样冒出（框架不猜）。

**保证边界**：task 层得到三层 prompt / 输出校验 / 事件流；得不到审计落盘 / 快照回滚 /
失败隔离 / 断点续跑——那些是 run 级保证，需要时往上爬一层建图（上文 Module 形态）。
红线：**task 级地板不许长成迷你引擎**——重试/落盘/条件分支属图，不在函数里重建。

## 嵌入者分层纪律

- **函数住 module_harness，调用方是应用层/模块层。** 宿主侧基础库（工具库、数据封装）
  想 import module_harness 即**分层警报**——该 LLM 调用应上移：由应用层 `call_harness`
  （基础库保持纯函数，数据进数据出），或在 module 里包成 script 节点（图编排）。
- **判定口诀：看 import 箭头**——高层 import 低层永远合法；箭头向上即警报。
- 完整论证见
  [`docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md`](../dev/superpowers/specs/2026-09-01-embedder-face-design.md) §2。
````

- [ ] **Step 2: roadmap 回写（五处精确编辑）**

`docs/dev/progress/module-roadmap.md`：

1. 日期行：
   `> 最后更新：2026-09-01（新增"三种消费形式"章节：补全嵌入者消费形式 + call_harness 待做）`
   →
   `> 最后更新：2026-09-01（嵌入者消费面落地：call_harness task 级地板 + reviewer 瘦身 + 嵌入者契约/指南）`

2. checkbox 1（`- [ ] **task 级 API 地板** ...`）→ `- [x]`，行尾追加：
   `（已实现，设计/论证见 docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md）`

3. checkbox 2：`- [ ] **API 稳定化冻结面纳入嵌入者契约**：\`__all__\` 明确嵌入者可 import 面（不只 store 枚举契约）`
   → `- [x] **API 稳定化冻结面纳入嵌入者契约**：\`__all__\` 注释标注嵌入者最小面（正式冻结归收口）`

4. checkbox 嵌入指南：`- [ ] **嵌入指南**（并入 repo-docs-tidy）`
   → `- [x] **嵌入指南**（embedding.md 补 task 级调用 + 嵌入者分层纪律；repo-docs-tidy 只需核对）`

5. 「不需要」节的红线条目 `- **分层方向别反**：函数住 module_harness，...而非底层反向依赖顶层`
   行尾追加：`（完整论证见 docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md §2）`

- [ ] **Step 3: Commit**

```bash
git add docs/guides/embedding.md docs/dev/progress/module-roadmap.md
git commit -m "docs: 嵌入指南补 task 级调用与分层纪律；roadmap 勾掉嵌入者面三 checkbox"
```

---

### Task 6: embed_minimal 冒烟段 + 全量验收

**Files:**
- Modify: `examples/embed_minimal/main.py`

- [ ] **Step 1: 加 task 级冒烟段**

`examples/embed_minimal/main.py`：

顶部 import 块 `from module_harness import (...)` 中 `HarnessFailed,` 之后加一行：

```python
    call_harness,
```

`main()` 中 `return 0` 之前加：

```python
    # 6. task 级地板：一次函数调用，不经图与 run（spec 2026-09-01-embedder-face-design）
    result = await call_harness(
        HarnessConfig(
            prompt_core="将以下文本翻译为中文：{text}",
            output_format=OutputFormat(type="json_object"),
        ),
        {"text": "SpecModule embeds as a task call."},
        llm_client=client,
        event_bus=bus,
    )
    print("=== task 级调用 call_harness ===")
    print(f"  value={result.value} raw={result.raw!r}")
```

文件头 docstring「验证点」列表追加一行：

```
    - task 级地板 call_harness：一次函数调用，不经图与 run
```

- [ ] **Step 2: 运行冒烟验证**

Run: `cd examples/embed_minimal && python main.py --mock`
Expected: 正常退出（exit 0），输出包含 `=== task 级调用 call_harness ===` 且
`value={'translation': '你好，世界！'}`（mock 客户端对该 prompt 返回固定 JSON）

- [ ] **Step 3: 全量验收**

Run（仓库根目录）: `python -m pytest module_harness/tests/ -q`
Expected: 全部通过，0 failed

- [ ] **Step 4: Commit**

```bash
git add examples/embed_minimal/main.py
git commit -m "feat: embed_minimal 加 call_harness task 级冒烟段"
```

---

## 完成定义

- spec §3-§6 全部落地：call.py / reviewer 瘦身 / 导出与标注 / 指南与 roadmap / 冒烟
- 全量测试绿；embed_minimal `--mock` 冒烟含 task 级段
- roadmap「嵌入者面：需要 🔜」三个 checkbox 全部 `[x]`
