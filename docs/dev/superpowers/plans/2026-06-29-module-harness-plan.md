# Module Harness & Script 实现计划

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标:** 实现 module_harness 包，提供 harness（LLM 调用自动化）和 script（纯 Python 函数事件包裹）两个 tickflow 执行元件。

**架构:** 在 tickflow 零修改的前提下，通过 `HarnessRegistry(Registry)` 子类注册 harness/script body。harness body 为框架根据 HarnessConfig 自动生成的 async 闭包（渲染 prompt → 调 LLM → 校验输出 → 发事件），script body 为事件包裹的用户函数。EventBus 提供细粒度过程事件，与 tickflow hooks 分层互补。

**技术栈:** Python 3.10+, tickflow (已有), llm 客户端 (已有), dataclasses, asyncio, jsonschema, pytest + pytest-asyncio

## Global Constraints

- tickflow 零修改 — HarnessRegistry 继承 Registry，不改动 tickflow 任何文件
- llm 模块零修改 — harness 内部直接使用已有客户端接口
- harness body 为 async def，要求 AsyncRunner
- promptmode 选错 → KeyError，框架不兜底（设计原则："完全掌控"）
- EventBus 回调异常记录日志并吞掉（与 tickflow hooks 一致）
- 所有模块以 tickflow Body 类型约定为界：`(DictView) -> Any`

## 文件清单

| 操作 | 路径 | 职责 |
|------|------|------|
| 创建 | `module_harness/__init__.py` | 包导出 |
| 创建 | `module_harness/events.py` | EventBus + 9 种事件 dataclass |
| 创建 | `module_harness/outputfmt.py` | OutputFormat + OutputValidator（校验+自动提取） |
| 创建 | `module_harness/config.py` | HarnessConfig dataclass + from_task_definition() |
| 创建 | `module_harness/prompt.py` | PromptRenderer（三层 prompt 拼接+关键词替换） |
| 创建 | `module_harness/harness.py` | Harness 类（build_body 返回 async 闭包） |
| 创建 | `module_harness/registry.py` | HarnessRegistry(Registry) + script() 装饰器 |
| 创建 | `module_harness/tests/__init__.py` | 测试包标记 |
| 创建 | `module_harness/tests/test_events.py` | EventBus 测试 |
| 创建 | `module_harness/tests/test_outputfmt.py` | OutputValidator 测试 |
| 创建 | `module_harness/tests/test_config.py` | HarnessConfig 测试 |
| 创建 | `module_harness/tests/test_prompt.py` | PromptRenderer 测试 |
| 创建 | `module_harness/tests/test_harne1ss.py` | Harness body 集成测试 |
| 创建 | `module_harness/tests/test_registry.py` | HarnessRegistry 注册+事件测试 |

---

## 前置条件：安装依赖

```bash
pip install jsonschema pytest-asyncio
```

---

### Task 1: EventBus + 事件类型 (`events.py`)

**文件:**
- 创建: `module_harness/events.py`
- 创建: `module_harness/tests/test_events.py`

**接口:**
- 产生: `EventBus` 类（`subscribe`, `emit`, `on`, `null`）
- 产生: 事件 dataclass — `HarnessEvent`, `PromptRendered`, `LlmCallStarted`, `LlmToken`, `LlmCallCompleted`, `OutputValidated`, `HarnessFailed`, `ScriptEvent`, `ScriptStarted`, `ScriptCompleted`, `ScriptFailed`

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_events.py
import time
from module_harness.events import (
    EventBus,
    HarnessEvent, PromptRendered, LlmCallStarted, LlmToken,
    LlmCallCompleted, OutputValidated, HarnessFailed,
    ScriptEvent, ScriptStarted, ScriptCompleted, ScriptFailed,
)


class TestEventBus:
    def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []

        bus.subscribe(PromptRendered, lambda e: received.append(e))

        evt = PromptRendered(timestamp=1.0, node="A", tick=0, rendered="hello")
        bus.emit(evt)
        assert len(received) == 1
        assert received[0].rendered == "hello"

    def test_multiple_subscribers_same_type(self):
        bus = EventBus()
        results = []

        bus.subscribe(LlmToken, lambda e: results.append(("a", e.chunk)))
        bus.subscribe(LlmToken, lambda e: results.append(("b", e.chunk)))

        bus.emit(LlmToken(timestamp=1.0, node="X", tick=0, chunk="hi"))
        assert len(results) == 2
        assert ("a", "hi") in results
        assert ("b", "hi") in results

    def test_callback_exception_swallowed(self):
        bus = EventBus()
        received = []

        def bad_callback(e):
            raise RuntimeError("boom")

        bus.subscribe(PromptRendered, bad_callback)
        bus.subscribe(PromptRendered, lambda e: received.append(e))

        # Must not raise
        bus.emit(PromptRendered(timestamp=1.0, node="A", tick=0, rendered="ok"))
        assert len(received) == 1

    def test_on_decorator(self):
        bus = EventBus()
        received = []

        @bus.on(LlmCallStarted)
        def handle(e):
            received.append(e.model)

        bus.emit(LlmCallStarted(timestamp=1.0, node="B", tick=0, model="claude", prompt_chars=100))
        assert received == ["claude"]

    def test_null_bus_emit_does_not_raise(self):
        bus = EventBus.null()
        # Must not raise — no subscribers, emit is no-op
        bus.emit(PromptRendered(timestamp=1.0, node="A", tick=0, rendered="x"))

    def test_events_carry_all_fields(self):
        e = OutputValidated(
            timestamp=1.0, node="C", tick=1,
            passed=True, extracted=False, error=None,
        )
        assert e.passed is True
        assert e.extracted is False
        assert e.error is None


class TestEventTypes:
    def test_harness_event_base_fields(self):
        e = PromptRendered(timestamp=1.0, node="A", tick=0, rendered="p")
        assert e.timestamp == 1.0
        assert e.node == "A"
        assert e.tick == 0

    def test_script_event_base_fields(self):
        e = ScriptStarted(timestamp=2.0, node="B", tick=1)
        assert e.timestamp == 2.0
        assert e.node == "B"
        assert e.tick == 1

    def test_harness_failed_fields(self):
        e = HarnessFailed(timestamp=1.0, node="X", tick=0, reason="timeout", failure_type="infrastructure")
        assert e.reason == "timeout"
        assert e.failure_type == "infrastructure"

    def test_llm_token_fields(self):
        e = LlmToken(timestamp=1.0, node="Y", tick=0, chunk="Hello")
        assert e.chunk == "Hello"

    def test_script_completed_output_type(self):
        e = ScriptCompleted(timestamp=1.0, node="Z", tick=2, output_type="dict")
        assert e.output_type == "dict"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_events.py -v
# 预期: 全部 FAIL — 模块不存在
```

- [ ] **Step 3: 编写 events.py**

```python
# module_harness/events.py
"""EventBus 与 harness/script 事件类型定义。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# ── Harness 事件基类 ──────────────────────────────────────────────

@dataclass
class HarnessEvent:
    timestamp: float
    node: str
    tick: int


@dataclass
class PromptRendered(HarnessEvent):
    rendered: str


@dataclass
class LlmCallStarted(HarnessEvent):
    model: str
    prompt_chars: int


@dataclass
class LlmToken(HarnessEvent):
    chunk: str


@dataclass
class LlmCallCompleted(HarnessEvent):
    content_chars: int
    usage: dict[str, int]
    finish_reason: str | None


@dataclass
class OutputValidated(HarnessEvent):
    passed: bool
    extracted: bool
    error: str | None


@dataclass
class HarnessFailed(HarnessEvent):
    reason: str
    failure_type: str  # "llm" | "infrastructure"


# ── Script 事件基类 ──────────────────────────────────────────────

@dataclass
class ScriptEvent:
    timestamp: float
    node: str
    tick: int


@dataclass
class ScriptStarted(ScriptEvent):
    pass


@dataclass
class ScriptCompleted(ScriptEvent):
    output_type: str


@dataclass
class ScriptFailed(ScriptEvent):
    error: str


# ── EventBus ──────────────────────────────────────────────────────

class EventBus:
    """同步发布/订阅。

    回调异常 → 记录日志并吞掉（与 tickflow hooks 行为一致）。
    使用 ``EventBus.null()`` 获取静默实例（嵌入式场景）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable]] = {}

    def subscribe(self, event_type: type, callback: Callable) -> None:
        """为某个事件类型注册回调。"""
        self._subscribers.setdefault(event_type, []).append(callback)

    def emit(self, event: HarnessEvent | ScriptEvent) -> None:
        """发布事件到所有匹配类型的订阅者。"""
        for event_type, callbacks in self._subscribers.items():
            if isinstance(event, event_type):
                for cb in callbacks:
                    try:
                        cb(event)
                    except Exception:
                        log.exception("EventBus callback raised; swallowed")

    def on(self, event_type: type):
        """装饰器方式订阅: ``@bus.on(LlmToken) def handle(e): ...``"""
        def deco(fn: Callable) -> Callable:
            self.subscribe(event_type, fn)
            return fn
        return deco

    @staticmethod
    def null() -> "EventBus":
        """返回一个静默 EventBus，emit 无操作。"""
        bus = EventBus()
        # 直接替换 emit 方法为 no-op，保留 subscribe 语义但无实际操作
        bus.emit = lambda event: None  # type: ignore[method-assign]
        return bus
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_events.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/events.py module_harness/tests/test_events.py module_harness/tests/__init__.py
git commit -m "feat(module_harness): add EventBus and event types"
```

---

### Task 2: OutputFormat + OutputValidator (`outputfmt.py`)

**文件:**
- 创建: `module_harness/outputfmt.py`
- 创建: `module_harness/tests/test_outputfmt.py`

**接口:**
- 依赖: `tickflow.Failure`
- 产生: `OutputFormat` dataclass, `OutputValidator` 类（`prompt_instruction()`, `validate()`, `register_extractor()`）

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_outputfmt.py
import json
import pytest
from tickflow import Failure
from module_harness.outputfmt import OutputFormat, OutputValidator


class TestOutputFormat:
    def test_defaults(self):
        fmt = OutputFormat(type="text")
        assert fmt.type == "text"
        assert fmt.schema is None
        assert fmt.instruction is None


class TestOutputValidatorText:
    def test_text_passthrough(self):
        v = OutputValidator(OutputFormat(type="text"))
        result = v.validate("任意文本")
        assert result == "任意文本"

    def test_prompt_instruction_text_is_empty(self):
        v = OutputValidator(OutputFormat(type="text"))
        assert v.prompt_instruction() == ""


class TestOutputValidatorJsonObject:
    def test_valid_json(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        result = v.validate('{"a": 1}')
        assert result == {"a": 1}

    def test_valid_json_array(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        result = v.validate('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_invalid_json_stripped_by_extractor(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        # markdown fence + trailing text
        raw = '```json\n{"name": "test"}\n```'
        result = v.validate(raw)
        assert result == {"name": "test"}

    def test_json_buried_in_text_extracted(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        raw = '解释：{"result": 42} 这是输出'
        result = v.validate(raw)
        assert result == {"result": 42}

    def test_trailing_junk_after_json(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        raw = '{"ok": true}。'
        result = v.validate(raw)
        assert result == {"ok": True}

    def test_completely_invalid_returns_failure(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        raw = '这不是 JSON 也不是任何可提取的内容'
        result = v.validate(raw)
        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert "输出格式校验失败" in result.error

    def test_register_custom_extractor(self):
        v = OutputValidator(OutputFormat(type="json_object"))

        def my_extractor(s: str) -> str | None:
            if s.startswith("RESULT:"):
                return s[7:].strip()
            return None

        v.register_extractor(my_extractor)
        result = v.validate("RESULT: [1,2]")
        assert result == [1, 2]

    def test_prompt_json_object_instruction(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        inst = v.prompt_instruction()
        assert "JSON" in inst


class TestOutputValidatorJsonSchema:
    def test_valid_against_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        result = v.validate('{"name": "Alice", "age": 30}')
        assert result == {"name": "Alice", "age": 30}

    def test_invalid_against_schema_returns_failure(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        result = v.validate('{"age": 30}')
        assert isinstance(result, Failure)
        assert result.type == "llm"

    def test_schema_extraction_then_validates(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "number"}},
            "required": ["x"],
        }
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        raw = '```json\n{"x": 3.14}\n```'
        result = v.validate(raw)
        assert result == {"x": 3.14}

    def test_prompt_instruction_includes_schema(self):
        schema = {"type": "object", "properties": {}}
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        inst = v.prompt_instruction()
        assert "schema" in inst.lower()


class TestOutputValidatorCustomInstruction:
    def test_custom_instruction_overrides_default(self):
        v = OutputValidator(OutputFormat(type="json_object", instruction="请返回 {'key': value} 格式"))
        inst = v.prompt_instruction()
        assert inst == "请返回 {'key': value} 格式"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_outputfmt.py -v
# 预期: 全部 FAIL — outputfmt 模块不存在
```

- [ ] **Step 3: 编写 outputfmt.py**

```python
# module_harness/outputfmt.py
"""OutputFormat 输出格式约束定义 + OutputValidator 校验器。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from tickflow import Failure


@dataclass
class OutputFormat:
    """输出格式约束定义。

    ``type`` 为 "json_object" 时要求合法 JSON；
    "json_schema" 时还需 ``schema`` 校验通过；
    "text" 时不做校验，直接返回原文本。
    """
    type: Literal["json_object", "json_schema", "text"]
    schema: dict[str, Any] | None = None
    instruction: str | None = None


# ── 内置提取器 ──────────────────────────────────────────────────

def _strip_markdown_fences(raw: str) -> str | None:
    """去除 ```json ... ``` 包裹。"""
    pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
    m = re.search(pattern, raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _extract_first_json(raw: str) -> str | None:
    """匹配第一个完整 JSON 对象或数组。"""
    # 先尝试 match 对象
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
    if m:
        return m.group(0)
    # 再尝试 match 数组
    m = re.search(r'\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]', raw, re.DOTALL)
    if m:
        return m.group(0)
    return None


def _strip_trailing_junk(raw: str) -> str | None:
    """从末尾逐步截断非 JSON 字符，尝试解析。"""
    s = raw.strip()
    while s:
        try:
            json.loads(s)
            return s
        except json.JSONDecodeError:
            pass
        s = s[:-1]
    return None


# ── OutputValidator ──────────────────────────────────────────────

class OutputValidator:
    """输出格式校验器。

    校验流程：先直接解析，失败则逐个尝试提取器；提取再失败 → Failure(type="llm")。
    """

    def __init__(self, fmt: OutputFormat) -> None:
        self.fmt = fmt
        self._extractors: list[Callable[[str], str | None]] = [
            _strip_markdown_fences,
            _extract_first_json,
            _strip_trailing_junk,
        ]

    def prompt_instruction(self) -> str:
        """生成注入到 user prompt 的格式约束文本。"""
        if self.fmt.instruction is not None:
            return self.fmt.instruction
        if self.fmt.type == "text":
            return ""
        if self.fmt.type == "json_object":
            return "请输出合法 JSON，不要包含任何解释或其他文本。"
        if self.fmt.type == "json_schema":
            schema_text = json.dumps(self.fmt.schema, ensure_ascii=False, indent=2)
            return (
                "请严格按照以下 JSON Schema 输出，不要包含任何解释或其他文本：\n"
                f"```json\n{schema_text}\n```"
            )
        return ""

    def validate(self, raw: str) -> Any:
        """校验并返回解析值，或 Failure(type="llm")。"""
        if self.fmt.type == "text":
            return raw

        # Step 1: 直接解析
        parsed, error = self._try_parse(raw)
        if parsed is not None and error is None:
            return parsed

        # Step 2: 逐个尝试提取器
        for extractor in self._extractors:
            extracted = extractor(raw)
            if extracted is not None:
                parsed, error = self._try_parse(extracted)
                if parsed is not None and error is None:
                    return parsed

        return Failure(
            f"输出格式校验失败：{error or '所有提取器均未能修复输出'}",
            type="llm",
        )

    def register_extractor(self, fn: Callable[[str], str | None]) -> None:
        """注册自定义提取策略。插入到内置提取器之前（优先尝试）。"""
        self._extractors.insert(0, fn)

    def _try_parse(self, text: str) -> tuple[Any, str | None]:
        """尝试 json.loads + 可选的 schema 校验。返回 (parsed, error)。"""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return None, str(e)

        if self.fmt.type == "json_schema" and self.fmt.schema is not None:
            try:
                import jsonschema
                jsonschema.validate(parsed, self.fmt.schema)
            except ImportError:
                # jsonschema 未安装时跳过 schema 校验，仅保证是 JSON
                pass
            except jsonschema.ValidationError as e:
                return None, str(e)

        return parsed, None
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_outputfmt.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/outputfmt.py module_harness/tests/test_outputfmt.py
git commit -m "feat(module_harness): add OutputFormat and OutputValidator"
```

---

### Task 3: HarnessConfig (`config.py`)

**文件:**
- 创建: `module_harness/config.py`
- 创建: `module_harness/tests/test_config.py`

**接口:**
- 依赖: `module_harness.outputfmt.OutputFormat`
- 产生: `HarnessConfig` dataclass, `from_task_definition()` 类方法

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_config.py
from module_harness.config import HarnessConfig
from module_harness.outputfmt import OutputFormat


class TestHarnessConfig:
    def test_minimal_config(self):
        cfg = HarnessConfig(prompt_core="你是翻译助手。")
        assert cfg.prompt_core == "你是翻译助手。"
        assert cfg.prompt_modes == {}
        assert cfg.output_format is None
        assert cfg.notdo == []
        assert cfg.model is None
        assert cfg.temperature is None
        assert cfg.think is None

    def test_full_config(self):
        fmt = OutputFormat(type="json_object")
        cfg = HarnessConfig(
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式风格", "casual": "随意风格"},
            output_format=fmt,
            notdo=["不要直译", "不要添加解释"],
            model="claude-sonnet-4-6",
            temperature=0.3,
            think=True,
        )
        assert cfg.prompt_modes["formal"] == "正式风格"
        assert cfg.output_format == fmt
        assert "不要直译" in cfg.notdo
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.temperature == 0.3
        assert cfg.think is True

    def test_default_factories_are_independent(self):
        a = HarnessConfig(prompt_core="A")
        b = HarnessConfig(prompt_core="B")
        a.notdo.append("不要做X")
        assert b.notdo == []  # 不共享

    def test_from_task_definition_basic(self):
        task = {
            "prompt_core": "你是助手",
            "prompt_modes": {"short": "简短回答"},
            "notdo": ["不要啰嗦"],
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.prompt_core == "你是助手"
        assert cfg.prompt_modes == {"short": "简短回答"}
        assert cfg.notdo == ["不要啰嗦"]

    def test_from_task_definition_with_output_format(self):
        task = {
            "prompt_core": "分析文本",
            "outputformat": {"type": "json_object"},
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.output_format is not None
        assert cfg.output_format.type == "json_object"

    def test_from_task_definition_with_schema(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        task = {
            "prompt_core": "提取信息",
            "outputformat": {"type": "json_schema", "schema": schema},
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.output_format.type == "json_schema"
        assert cfg.output_format.schema == schema

    def test_from_task_definition_model_override(self):
        task = {
            "prompt_core": "x",
            "model": "gpt-4o",
            "temperature": 0.1,
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.model == "gpt-4o"
        assert cfg.temperature == 0.1
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_config.py -v
# 预期: 全部 FAIL — config 模块不存在
```

- [ ] **Step 3: 编写 config.py**

```python
# module_harness/config.py
"""HarnessConfig — harness 节点的完整配置数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .outputfmt import OutputFormat


@dataclass
class HarnessConfig:
    """harness 节点的完整配置。

    对标 tasklist 中 Task 定义的字段。
    翻译层使用 :meth:`from_task_definition` 直接构造。
    """

    # ── 三层 prompt ──
    prompt_core: str
    """Layer 1：核心提示词模板，含 {key} 占位符。"""

    prompt_modes: dict[str, str] = field(default_factory=dict)
    """Layer 2：动态 prompt 选项集。{"formal": "...", "casual": "..."}。"""

    # ── 输出约束 ──
    output_format: OutputFormat | None = None
    """输出格式约束（None = 不约束）。"""

    notdo: list[str] = field(default_factory=list)
    """否定性约束列表，拼入 system prompt。"""

    # ── LLM 默认参数（Task 可逐项覆盖）──
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None

    @classmethod
    def from_task_definition(cls, task: dict[str, Any]) -> "HarnessConfig":
        """从 tasklist Task dict 构造 HarnessConfig。

        task 中预期的键：
        - prompt_core   → Layer 1
        - prompt_modes  → Layer 2
        - outputformat  → 输出格式（dict，含 type/schema/instruction）
        - notdo         → 否定性约束列表
        - model         → LLM 模型覆盖
        - temperature   → 温度覆盖
        - think         → 扩展思考覆盖
        """
        output_format = None
        of_data = task.get("outputformat")
        if of_data is not None:
            output_format = OutputFormat(
                type=of_data["type"],
                schema=of_data.get("schema"),
                instruction=of_data.get("instruction"),
            )

        return cls(
            prompt_core=task["prompt_core"],
            prompt_modes=task.get("prompt_modes", {}),
            output_format=output_format,
            notdo=task.get("notdo", []),
            model=task.get("model"),
            temperature=task.get("temperature"),
            think=task.get("think"),
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_config.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/config.py module_harness/tests/test_config.py
git commit -m "feat(module_harness): add HarnessConfig with from_task_definition"
```

---

### Task 4: PromptRenderer (`prompt.py`)

**文件:**
- 创建: `module_harness/prompt.py`
- 创建: `module_harness/tests/test_prompt.py`

**接口:**
- 依赖: `module_harness.config.HarnessConfig`, `tickflow.views.DictView`
- 产生: `PromptRenderer` 类（`render()` 方法返回 str）

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_prompt.py
from tickflow.views import DictView, Resolved
from module_harness.config import HarnessConfig
from module_harness.prompt import PromptRenderer


def _make_view(**inputs) -> DictView:
    """构造一个测试用 DictView。"""
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="test_node")


class TestPromptRenderer:
    def test_layer1_only(self):
        cfg = HarnessConfig(prompt_core="请翻译以下内容。")
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        assert result == "请翻译以下内容。"

    def test_layer1_with_keyword_substitution(self):
        cfg = HarnessConfig(prompt_core="翻译：{text}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view(text="Hello world"))
        assert result == "翻译：Hello world"

    def test_multiple_keywords(self):
        cfg = HarnessConfig(prompt_core="将 {source} 翻译为 {target}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view(source="Hello", target="Chinese"))
        assert "Hello" in result
        assert "Chinese" in result

    def test_layer2_selected_by_promptmode(self):
        cfg = HarnessConfig(
            prompt_core="任务：{input}",
            prompt_modes={"formal": "请使用正式语气。", "casual": "请使用日常语气。"},
        )
        r = PromptRenderer(cfg)
        result = r.render(_make_view(input="介绍"), promptmode="formal")
        assert "请使用正式语气。" in result
        assert result.index("任务") < result.index("请使用正式语气")

    def test_layer3_prompt_extra(self):
        cfg = HarnessConfig(prompt_core="翻译：{text}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view(text="Hi"), prompt_extra="特别注意：不要意译。")
        assert "特别注意：不要意译。" in result

    def test_all_three_layers(self):
        cfg = HarnessConfig(
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式风格。"},
        )
        r = PromptRenderer(cfg)
        result = r.render(
            _make_view(text="Hello"),
            promptmode="formal",
            prompt_extra="术语要准确。",
        )
        # Layer 1 在前，Layer 2 在中，Layer 3 在后
        assert result.index("翻译") < result.index("正式风格") < result.index("术语")

    def test_promptmode_keyerror_on_invalid(self):
        cfg = HarnessConfig(
            prompt_core="x",
            prompt_modes={"a": "mode a"},
        )
        r = PromptRenderer(cfg)
        import pytest
        with pytest.raises(KeyError):
            r.render(_make_view(), promptmode="nonexistent")

    def test_none_promptmode_skips_layer2(self):
        cfg = HarnessConfig(
            prompt_core="核心内容。",
            prompt_modes={"x": "不应该出现"},
        )
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        assert "不应该出现" not in result
        assert result == "核心内容。"

    def test_keyword_not_in_view_becomes_missing_text(self):
        cfg = HarnessConfig(prompt_core="值：{missing_key}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        # 未解析的占位符保持不变（或替换为空）
        # 此处根据设计：未匹配的 key 保留原样（不隐藏问题）
        assert "{missing_key}" in result or "Missing" in result

    def test_no_double_whitespace(self):
        cfg = HarnessConfig(prompt_core="核心。")
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        # 单一层不应有多余空行
        assert result == "核心。"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_prompt.py -v
# 预期: 全部 FAIL
```

- [ ] **Step 3: 编写 prompt.py**

```python
# module_harness/prompt.py
"""三层 prompt 渲染 + 关键词替换。"""

from __future__ import annotations

import re
from typing import Any

from tickflow.views import DictView, Missing

from .config import HarnessConfig


class PromptRenderer:
    """三层 prompt 拼接 + 关键词替换。

    数据来源：
      Layer 1: config.prompt_core       — 核心提示词模板，含 {key} 占位符
      Layer 2: config.prompt_modes[mode]  — 由 Task promptmode 选出的动态 prompt
      Layer 3: prompt_extra             — Task prompt 字段，人工注入部分

    关键词替换：模板中的 {key} 从 DictView 取值（view.key.value）。
    未匹配的 key 保留原样（不隐藏问题）。
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def render(
        self,
        view: DictView,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
    ) -> str:
        """渲染最终 user prompt。"""
        parts: list[str] = []

        # Layer 1: 核心提示词
        parts.append(self.config.prompt_core)

        # Layer 2: 由 promptmode 选出的动态 prompt
        if promptmode is not None:
            mode_text = self.config.prompt_modes[promptmode]
            parts.append(mode_text)

        # Layer 3: 人工注入
        if prompt_extra:
            parts.append(prompt_extra)

        combined = "\n\n".join(parts)
        return self._substitute(combined, view)

    def _substitute(self, template: str, view: DictView) -> str:
        """替换模板中的 {key} 占位符为 view 中的值。"""
        pattern = re.compile(r'\{(\w+)\}')

        def _replacer(m: re.Match) -> str:
            key = m.group(1)
            try:
                val = view[key].value
            except (KeyError, AttributeError):
                return m.group(0)  # 保留原样
            if val is Missing:
                return m.group(0)
            return str(val)

        return pattern.sub(_replacer, template)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_prompt.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/prompt.py module_harness/tests/test_prompt.py
git commit -m "feat(module_harness): add PromptRenderer with three-layer prompt"
```

---

### Task 5: Harness 类 (`harness.py`)

**文件:**
- 创建: `module_harness/harness.py`
- 创建: `module_harness/tests/test_harness.py`

**接口:**
- 依赖: `module_harness.{config, prompt, outputfmt, events}`, `llm.client`
- 产生: `Harness` 类（`build_body()` 返回 async body callable）

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_harness.py
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from tickflow import Failure
from tickflow.views import DictView, Resolved
from module_harness.config import HarnessConfig
from module_harness.outputfmt import OutputFormat
from module_harness.events import (
    EventBus, PromptRendered, LlmCallStarted, LlmToken,
    LlmCallCompleted, OutputValidated, HarnessFailed,
)
from module_harness.harness import Harness


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def basic_config():
    return HarnessConfig(prompt_core="翻译：{text}")


def _make_view(**inputs) -> DictView:
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="test_node")


class TestHarnessBuildBody:
    @pytest.mark.asyncio
    async def test_successful_call_returns_parsed_output(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content='{"result": "translated text"}',
            usage={"input_tokens": 10, "output_tokens": 5},
            finish_reason="end_turn",
        )
        bus = EventBus()
        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()

        result = await body(_make_view(text="Hello"))

        assert result == {"result": "translated text"}
        mock_llm.complete.assert_called_once()
        call_kwargs = mock_llm.complete.call_args.kwargs
        assert "Hello" in call_kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_validation_failure_returns_failure(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all, completely invalid {{{",
            usage={"input_tokens": 5, "output_tokens": 5},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(
            prompt_core="x",
            output_format=OutputFormat(type="json_object"),
        )
        bus = EventBus()
        h = Harness(cfg, mock_llm, bus)
        body = h.build_body()

        result = await body(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "llm"

    @pytest.mark.asyncio
    async def test_infrastructure_error_returns_abort_failure(self, mock_llm, basic_config):
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("网络超时")
        bus = EventBus()
        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()

        result = await body(_make_view(text="Hello"))

        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert "网络超时" in result.error

    @pytest.mark.asyncio
    async def test_events_emitted_on_success(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content='{"ok": true}',
            usage={"input_tokens": 5, "output_tokens": 3},
            finish_reason="end_turn",
        )
        events = []
        bus = EventBus()
        bus.subscribe(HarnessEvent, lambda e: events.append(type(e).__name__))
        bus.subscribe(PromptRendered, events.append)

        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()
        await body(_make_view(text="test"))

        event_names = [type(e).__name__ for e in events if not isinstance(e, str)]
        assert "PromptRendered" in event_names
        assert "LlmCallStarted" in event_names
        assert "LlmCallCompleted" in event_names
        assert "OutputValidated" in event_names

    @pytest.mark.asyncio
    async def test_harness_failed_event_on_infrastructure(self, mock_llm, basic_config):
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("API 鉴权失败")
        failed_events = []
        bus = EventBus()
        bus.subscribe(HarnessFailed, failed_events.append)

        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()
        await body(_make_view())

        assert len(failed_events) == 1
        assert failed_events[0].failure_type == "infrastructure"

    @pytest.mark.asyncio
    async def test_llm_token_events_emitted(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        chunks = ["Hello", " ", "World"]
        mock_llm.complete.return_value = LLMResponse(
            content="Hello World",
            usage={},
            finish_reason="end_turn",
        )

        # 模拟 on_token 回调
        token_callbacks = []

        async def fake_complete(*args, **kwargs):
            on_token = kwargs.get("on_token")
            if on_token:
                for c in chunks:
                    on_token(c)
            return mock_llm.complete.return_value

        mock_llm.complete = AsyncMock(side_effect=fake_complete)

        tokens = []
        bus = EventBus()
        bus.subscribe(LlmToken, lambda e: tokens.append(e.chunk))

        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()
        await body(_make_view(text="test"))

        assert tokens == ["Hello", " ", "World"]

    @pytest.mark.asyncio
    async def test_promptmode_passed_to_renderer(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content="plain text response",
            usage={},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(
            prompt_core="核心：{text}",
            prompt_modes={"extra": "额外指令"},
        )
        bus = EventBus()
        h = Harness(cfg, mock_llm, bus)
        body = h.build_body(promptmode="extra")

        await body(_make_view(text="test"))

        call_prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert "额外指令" in call_prompt

    @pytest.mark.asyncio
    async def test_notdo_passed_to_system(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content="ok",
            usage={},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(
            prompt_core="核心。",
            notdo=["不要废话", "不要重复"],
        )
        bus = EventBus()
        h = Harness(cfg, mock_llm, bus)
        body = h.build_body()

        await body(_make_view())

        sys = mock_llm.complete.call_args.kwargs.get("system") or ""
        assert "不要废话" in sys
        assert "不要重复" in sys
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_harness.py -v
# 预期: 全部 FAIL
```

- [ ] **Step 3: 编写 harness.py**

```python
# module_harness/harness.py
"""Harness 类 — 配置持有 + async body 生成。"""

from __future__ import annotations

import time
from typing import Any

from tickflow import Failure
from tickflow.views import DictView

from .config import HarnessConfig
from .prompt import PromptRenderer
from .outputfmt import OutputValidator
from .events import (
    EventBus,
    PromptRendered,
    LlmCallStarted,
    LlmToken,
    LlmCallCompleted,
    OutputValidated,
    HarnessFailed,
)


class Harness:
    """持有 HarnessConfig + LLM 客户端 + EventBus。

    由 HarnessRegistry 管理，用户不直接使用。
    """

    def __init__(
        self,
        config: HarnessConfig,
        llm_client: Any,
        event_bus: EventBus,
    ) -> None:
        self.config = config
        self.llm = llm_client
        self.bus = event_bus
        self._renderer = PromptRenderer(config)

    def build_body(
        self,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
    ):
        """返回一个 async body callable。

        body 执行流程：
          1. 渲染三层 prompt
          2. 调 LLM（流式 token 经 on_token 发射）
          3. 校验输出格式
          4. 发事件
        """
        config = self.config
        llm = self.llm
        bus = self.bus
        renderer = self._renderer
        validator = OutputValidator(config.output_format) if config.output_format else None

        async def body(view: DictView) -> Any:
            node = view.node
            now = time.monotonic()

            # 1. 渲染 prompt
            rendered = renderer.render(
                view,
                promptmode=promptmode,
                prompt_extra=prompt_extra,
            )
            bus.emit(PromptRendered(
                timestamp=time.monotonic(), node=node, tick=0,
                rendered=rendered,
            ))

            # 2. 调用 LLM
            bus.emit(LlmCallStarted(
                timestamp=time.monotonic(), node=node, tick=0,
                model=config.model or "default",
                prompt_chars=len(rendered),
            ))

            def on_token(chunk: str) -> None:
                bus.emit(LlmToken(
                    timestamp=time.monotonic(), node=node, tick=0,
                    chunk=chunk,
                ))

            try:
                from llm.client import LLMError

                # 准备 system prompt（notdo）
                system = None
                if config.notdo:
                    system = "不要做以下事项：\n" + "\n".join(
                        f"- {n}" for n in config.notdo
                    )

                response = await llm.complete(
                    prompt=rendered,
                    system=system,
                    model=config.model,
                    temperature=config.temperature,
                    think=config.think,
                    output_format=config.output_format.__dict__ if config.output_format else None,
                    notdo=config.notdo if config.notdo else None,
                    on_token=on_token,
                )
            except LLMError as e:
                bus.emit(HarnessFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    reason=str(e),
                    failure_type="infrastructure",
                ))
                return Failure(str(e), type="infrastructure")

            # 3. 校验输出
            bus.emit(LlmCallCompleted(
                timestamp=time.monotonic(), node=node, tick=0,
                content_chars=len(response.content),
                usage=response.usage,
                finish_reason=response.finish_reason,
            ))

            if validator is not None:
                result = validator.validate(response.content)
                if isinstance(result, Failure):
                    bus.emit(OutputValidated(
                        timestamp=time.monotonic(), node=node, tick=0,
                        passed=False,
                        extracted=False,
                        error=result.error,
                    ))
                    return result
                bus.emit(OutputValidated(
                    timestamp=time.monotonic(), node=node, tick=0,
                    passed=True,
                    extracted=_was_extracted(response.content, result),
                    error=None,
                ))
                return result

            return response.content

        return body


def _was_extracted(raw: str, result: Any) -> bool:
    """简单判断原始内容是否经过了提取处理（内容不直接相等）。"""
    if not isinstance(result, str):
        return True  # JSON 解析必然是提取
    return raw.strip() != result.strip()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_harness.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/harness.py module_harness/tests/test_harness.py
git commit -m "feat(module_harness): add Harness class with build_body"
```

---

### Task 6: HarnessRegistry (`registry.py`)

**文件:**
- 创建: `module_harness/registry.py`
- 创建: `module_harness/tests/test_registry.py`

**接口:**
- 依赖: `tickflow.Registry`, `module_harness.{harness, events}`
- 产生: `HarnessRegistry(Registry)` 类，`harness()` 注册方法，`script()` 装饰器

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_registry.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from tickflow import Registry
from tickflow.views import DictView, Resolved
from module_harness.config import HarnessConfig
from module_harness.events import (
    EventBus, ScriptStarted, ScriptCompleted, ScriptFailed,
)
from module_harness.registry import HarnessRegistry


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def reg(mock_llm):
    bus = EventBus()
    return HarnessRegistry(llm_client=mock_llm, event_bus=bus)


def _make_view(**inputs) -> DictView:
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="test_node")


class TestHarnessRegistryInheritance:
    def test_is_subclass_of_registry(self):
        assert issubclass(HarnessRegistry, Registry)

    def test_inherited_body_registration_works(self, reg):
        @reg.body("my_body")
        def my_body(view):
            return "result"

        fn = reg.get_body("my_body")
        assert fn is not None


class TestHarnessRegistration:
    def test_harness_registers_body(self, reg):
        cfg = HarnessConfig(prompt_core="翻译：{text}")
        reg.harness("translate", cfg)

        assert reg.has_body("translate")
        assert reg.is_harness("translate")
        assert not reg.is_script("translate")

    def test_harness_chain_calls(self, reg):
        cfg1 = HarnessConfig(prompt_core="A")
        cfg2 = HarnessConfig(prompt_core="B")
        reg.harness("a", cfg1).harness("b", cfg2)

        assert reg.has_body("a")
        assert reg.has_body("b")

    def test_harness_config_retrieval(self, reg):
        cfg = HarnessConfig(prompt_core="核心", model="gpt-4o")
        reg.harness("mine", cfg)

        retrieved = reg.harness_config("mine")
        assert retrieved is cfg
        assert retrieved.model == "gpt-4o"

    def test_harness_config_none_for_unknown(self, reg):
        assert reg.harness_config("nope") is None

    def test_is_harness_false_for_regular_body(self, reg):
        reg.body("regular", lambda v: "ok")
        assert not reg.is_harness("regular")

    @pytest.mark.asyncio
    async def test_harness_body_callable(self, reg):
        from llm.client import LLMResponse
        reg._llm_client.complete.return_value = LLMResponse(
            content="直接文本响应",
            usage={},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(prompt_core="测试：{input}")
        reg.harness("test_h", cfg)

        body = reg.get_body("test_h")
        result = await body(_make_view(input="hello"))

        assert result == "直接文本响应"


class TestScriptRegistration:
    def test_script_registers_body(self, reg):
        @reg.script("compute")
        def compute(view):
            return {"count": len(view.data.value)}

        assert reg.has_body("compute")
        assert reg.is_script("compute")
        assert not reg.is_harness("compute")

    def test_script_emits_start_and_complete(self, reg):
        events = []
        reg._event_bus.subscribe(ScriptStarted, lambda e: events.append("start"))
        reg._event_bus.subscribe(ScriptCompleted, lambda e: events.append("complete"))

        @reg.script("my_script")
        def my_script(view):
            return view.input.value * 2

        body = reg.get_body("my_script")
        result = body(_make_view(input=21))

        assert result == 42
        assert events == ["start", "complete"]

    def test_script_emits_failed_on_exception(self, reg):
        failures = []
        reg._event_bus.subscribe(ScriptFailed, lambda e: failures.append(e))

        @reg.script("bad")
        def bad(view):
            raise ValueError("故意的错误")

        body = reg.get_body("bad")
        with pytest.raises(ValueError, match="故意的错误"):
            body(_make_view())

        assert len(failures) == 1
        assert failures[0].error == "故意的错误"

    def test_script_with_no_event_bus_does_not_raise(self, mock_llm):
        reg2 = HarnessRegistry(llm_client=mock_llm, event_bus=None)

        @reg2.script("silent")
        def silent(view):
            return 1

        body = reg2.get_body("silent")
        result = body(_make_view())
        assert result == 1

    def test_is_script_false_for_regular_body(self, reg):
        reg.body("plain", lambda v: "x")
        assert not reg.is_script("plain")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_registry.py -v
# 预期: 全部 FAIL
```

- [ ] **Step 3: 编写 registry.py**

```python
# module_harness/registry.py
"""HarnessRegistry — tickflow Registry 子类，管理 harness/script 注册。"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable

from tickflow import Registry
from tickflow.views import DictView

from .config import HarnessConfig
from .harness import Harness
from .events import (
    EventBus,
    ScriptStarted,
    ScriptCompleted,
    ScriptFailed,
)


class HarnessRegistry(Registry):
    """tickflow Registry 子类。tickflow 零修改。

    Runner 只调 ``get_body()``，不感知 harness/script/body 的区别。
    """

    def __init__(
        self,
        *,
        llm_client: Any,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__()
        self._llm_client = llm_client
        self._event_bus = event_bus or EventBus.null()
        self._harness_cfgs: dict[str, HarnessConfig] = {}
        self._script_names: set[str] = set()

    # ── harness 注册 ──────────────────────────────────────────────

    def harness(
        self,
        name: str,
        config: HarnessConfig,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
    ) -> "HarnessRegistry":
        """注册一个 harness body。

        ``name`` 是 graph 中 ``node.body`` 引用的名称。
        返回 self，支持链式调用。
        """
        h = Harness(config, self._llm_client, self._event_bus)
        body = h.build_body(promptmode=promptmode, prompt_extra=prompt_extra)
        self.body(name, body)
        self._harness_cfgs[name] = config
        return self

    # ── script 注册 ───────────────────────────────────────────────

    def script(self, name: str):
        """装饰器：``@reg.script('name')`` — 包裹事件发射后注册为 body。

        body 执行时自动发射 ScriptStarted / ScriptCompleted / ScriptFailed。
        支持 sync 和 async 用户函数。
        """
        bus = self._event_bus

        def deco(fn: Callable) -> Callable:
            is_async = inspect.iscoroutinefunction(fn)

            if is_async:
                @functools.wraps(fn)
                async def wrapped(view: DictView) -> Any:
                    node = view.node
                    bus.emit(ScriptStarted(
                        timestamp=time.monotonic(), node=node, tick=0,
                    ))
                    try:
                        result = await fn(view)
                    except Exception as e:
                        bus.emit(ScriptFailed(
                            timestamp=time.monotonic(), node=node, tick=0,
                            error=str(e),
                        ))
                        raise
                    bus.emit(ScriptCompleted(
                        timestamp=time.monotonic(), node=node, tick=0,
                        output_type=type(result).__name__,
                    ))
                    return result
            else:
                @functools.wraps(fn)
                def wrapped(view: DictView) -> Any:
                    node = view.node
                    bus.emit(ScriptStarted(
                        timestamp=time.monotonic(), node=node, tick=0,
                    ))
                    try:
                        result = fn(view)
                    except Exception as e:
                        bus.emit(ScriptFailed(
                            timestamp=time.monotonic(), node=node, tick=0,
                            error=str(e),
                        ))
                        raise
                    bus.emit(ScriptCompleted(
                        timestamp=time.monotonic(), node=node, tick=0,
                        output_type=type(result).__name__,
                    ))
                    return result

            self.body(name, wrapped)
            self._script_names.add(name)
            return wrapped

        return deco

    # ── 查询 ──────────────────────────────────────────────────────

    def is_harness(self, name: str) -> bool:
        """name 是否通过 harness() 注册。"""
        return name in self._harness_cfgs

    def is_script(self, name: str) -> bool:
        """name 是否通过 script() 注册。"""
        return name in self._script_names

    def harness_config(self, name: str) -> HarnessConfig | None:
        """返回 harness 的配置，若不是 harness 返回 None。"""
        return self._harness_cfgs.get(name)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_registry.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/registry.py module_harness/tests/test_registry.py
git commit -m "feat(module_harness): add HarnessRegistry with harness() and script()"
```

---

### Task 7: 集成测试

**文件:**
- 创建: `module_harness/tests/test_integration.py`

**接口:**
- 依赖: 所有上述模块 + `tickflow.{Graph, parse, AsyncRunner}`

- [ ] **Step 1: 编写集成测试**

```python
# module_harness/tests/test_integration.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from tickflow import parse, AsyncRunner
from llm.client import LLMResponse
from module_harness.config import HarnessConfig
from module_harness.outputfmt import OutputFormat
from module_harness.events import EventBus, LlmToken, OutputValidated, ScriptCompleted
from module_harness.registry import HarnessRegistry


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _make_graph_text(harness_body_name: str = "translate", script_body_name: str = "process"):
    return f"""
    [A]-->B
    A.body: {harness_body_name}
    B.body: {script_body_name}
    B.inputs: output: A
    """


class TestHarnessScriptIntegration:
    @pytest.mark.asyncio
    async def test_harness_output_flows_to_script(self, mock_llm):
        """完整流程：harness 产出 JSON → script 处理 → 返回结果。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"text": "Hello World", "lang": "en"}',
            usage={"input_tokens": 10, "output_tokens": 8},
            finish_reason="end_turn",
        )

        bus = EventBus()
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        # 注册 harness
        reg.harness("translate", HarnessConfig(
            prompt_core="翻译：{text}",
            output_format=OutputFormat(type="json_object"),
        ))

        # 注册 script — 处理上游 JSON
        @reg.script("process")
        def process(view):
            data = view.output.value
            return {"char_count": len(data["text"])}

        graph = parse(_make_graph_text())
        runner = AsyncRunner(graph, registry=reg)

        firings = await runner.run_until_idle(max_ticks=10)

        # 两个节点都成功
        assert len(firings) == 2
        assert all(f.status == "ok" for f in firings)

        # script 节点得到正确的计算结果
        b_firing = [f for f in firings if f.node == "B"][0]
        assert b_firing.output == {"char_count": 11}

    @pytest.mark.asyncio
    async def test_harness_failure_halts_downstream(self, mock_llm):
        """harness 返回 llm 级别 Failure 时，下游 AND-join 不应触发。"""
        # 返回无法解析的内容 + json_object 约束 → Failure(type="llm")
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all {{{",
            usage={"input_tokens": 5, "output_tokens": 3},
            finish_reason="end_turn",
        )

        bus = EventBus()
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        reg.harness("translate", HarnessConfig(
            prompt_core="x",
            output_format=OutputFormat(type="json_object"),
        ))

        executed = []
        @reg.script("process")
        def process(view):
            executed.append(True)
            return "should not run"

        graph = parse(_make_graph_text())
        runner = AsyncRunner(graph, registry=reg)
        firings = await runner.run_until_idle(max_ticks=10)

        # 只有 node A 触发过
        assert len(firings) == 1
        assert firings[0].node == "A"
        assert firings[0].status == "failed"
        assert len(executed) == 0  # B 从未触发

    @pytest.mark.asyncio
    async def test_infrastructure_failure_aborts_runner(self, mock_llm):
        """LLMError → ABORTED。"""
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("API 不可用")

        bus = EventBus()
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        reg.harness("translate", HarnessConfig(prompt_core="x"))

        @reg.script("process")
        def process(view):
            return view.output.value

        graph = parse(_make_graph_text())
        runner = AsyncRunner(graph, registry=reg)
        firings = await runner.run_until_idle(max_ticks=10)

        assert runner.status.value == "aborted"
        assert firings[0].status == "aborted"

    @pytest.mark.asyncio
    async def test_events_collected_during_run(self, mock_llm):
        """EventBus 事件在整个 run 中被正确收集。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"x": 1}',
            usage={},
            finish_reason="end_turn",
        )

        bus = EventBus()
        harness_events = []
        script_events = []
        bus.subscribe(LlmToken, lambda e: harness_events.append(("token", e.chunk)))
        bus.subscribe(OutputValidated, lambda e: harness_events.append(("validated", e.passed)))
        bus.subscribe(ScriptCompleted, lambda e: script_events.append(e))

        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)
        reg.harness("translate", HarnessConfig(
            prompt_core="x",
            output_format=OutputFormat(type="json_object"),
        ))

        @reg.script("process")
        def process(view):
            return view.output.value["x"] * 2

        graph = parse(_make_graph_text())
        runner = AsyncRunner(graph, registry=reg)
        await runner.run_until_idle(max_ticks=10)

        # EventBus 收集到 harness 和 script 事件
        validated = [e for e in harness_events if isinstance(e, tuple) and e[0] == "validated"]
        assert len(validated) == 1
        assert validated[0][1] is True  # passed
        assert len(script_events) == 1

    @pytest.mark.asyncio
    async def test_multiple_ticks_independent_events(self, mock_llm):
        """两个串行 harness 节点各自独立生成事件。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"step1": "done"}',
            usage={},
            finish_reason="end_turn",
        )

        rendered = []
        bus = EventBus()
        from module_harness.events import PromptRendered
        bus.subscribe(PromptRendered, lambda e: rendered.append(e.node))

        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)
        reg.harness("step1", HarnessConfig(prompt_core="第一步"))
        reg.harness("step2", HarnessConfig(prompt_core="第二步"))

        graph = parse("""
        [A]-->B
        A.body: step1
        B.body: step2
        B.inputs: output: A
        """)

        runner = AsyncRunner(graph, registry=reg)
        firings = await runner.run_until_idle(max_ticks=10)

        assert len(firings) == 2
        assert rendered == ["A", "B"]
```

- [ ] **Step 2: 运行集成测试**

```bash
python -m pytest module_harness/tests/test_integration.py -v
# 预期: 全部 PASS
```

- [ ] **Step 3: 提交**

```bash
git add module_harness/tests/test_integration.py
git commit -m "test(module_harness): add integration tests for harness + script + tickflow"
```

---

### Task 8: Package 导出 (`__init__.py`)

**文件:**
- 创建: `module_harness/__init__.py`

- [ ] **Step 1: 编写 __init__.py**

```python
# module_harness/__init__.py
"""ModuleHarness — tickflow 上层抽象：harness 与 script 执行元件。"""

from .config import HarnessConfig
from .outputfmt import OutputFormat, OutputValidator
from .prompt import PromptRenderer
from .events import (
    EventBus,
    HarnessEvent,
    PromptRendered,
    LlmCallStarted,
    LlmToken,
    LlmCallCompleted,
    OutputValidated,
    HarnessFailed,
    ScriptEvent,
    ScriptStarted,
    ScriptCompleted,
    ScriptFailed,
)
from .harness import Harness
from .registry import HarnessRegistry

__all__ = [
    # 配置
    "HarnessConfig",
    # 输出格式
    "OutputFormat",
    "OutputValidator",
    # Prompt
    "PromptRenderer",
    # 事件
    "EventBus",
    "HarnessEvent",
    "PromptRendered",
    "LlmCallStarted",
    "LlmToken",
    "LlmCallCompleted",
    "OutputValidated",
    "HarnessFailed",
    "ScriptEvent",
    "ScriptStarted",
    "ScriptCompleted",
    "ScriptFailed",
    # 核心
    "Harness",
    "HarnessRegistry",
]
```

- [ ] **Step 2: 验证导入**

```bash
python -c "import module_harness; print(module_harness.__all__)"
# 预期: 打印所有公开符号列表，无 ImportError
```

- [ ] **Step 3: 运行全部测试**

```bash
python -m pytest module_harness/tests/ -v
# 预期: 全部 PASS（约 40+ 个测试）
```

- [ ] **Step 4: 提交**

```bash
git add module_harness/__init__.py
git commit -m "feat(module_harness): add package exports"
```

---

## 任务依赖图

```
Task 1 (events) ─────────────────────────────────────────────────────────────┐
     │                                                                       │
Task 2 (outputfmt) ──────────────────────────────────────────────────────┐  │
     │                                                                   │  │
Task 3 (config) ────────────────────────────────────────────────────┐   │  │
     │                                                               │   │  │
Task 4 (prompt) ────────────────────────────────────────────────┐   │   │  │
     │                                                           │   │   │  │
Task 5 (harness) ← depends on 1,2,3,4 ──────────────────────────┤   │   │  │
     │                                                           │   │   │  │
Task 6 (registry) ← depends on 1,5 ─────────────────────────────┤   │   │  │
     │                                                           │   │   │  │
Task 7 (integration) ← depends on 1-6 ──────────────────────────┤   │   │  │
     │                                                           │   │   │  │
Task 8 (__init__.py) ← depends on 1-6 ───────────────────────────┘   ┘   ┘  ┘
```

Task 1-6 必须按顺序执行（线性依赖）。
Task 7 在 1-6 全部完成后执行。
Task 8 可与 Task 7 并行或在 Task 7 之后执行。

---

## 验证清单

- [x] 所有模块可独立导入，无循环依赖
- [x] tickflow 零修改（HarnessRegistry 为子类）
- [x] harness body 返回 ``(DictView) -> Any``，符合 tickflow Body 类型
- [x] script wrapper 同时支持 sync/async 用户函数
- [x] EventBus 回调异常记录日志不向上抛
- [x] promptmode 选错 → KeyError（框架不兜底）
- [x] output_format 校验失败 → Failure(type="llm")
- [x] LLMError → Failure(type="infrastructure") → Runner ABORTED
- [x] EventBus.null() 供无事件场景使用
- [x] 集成测试覆盖完整 harness → script → tickflow 链路
