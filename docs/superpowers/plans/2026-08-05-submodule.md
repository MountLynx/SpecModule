# Submodule 系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 submodule 系统——第一层开发者用 `SubModule` 类式声明 module（harnesses/scripts/commands/tasklist/spec_schema），`pack()` 导出发布目录；第二层用户用 `ModuleLoader` 加载后只写 spec 即可运行。

**Architecture:** `SubModule` 不平行实现执行——内部组合现有 `Module`（spec+tasklist → 校验 → TasklistTranslator → AsyncRunner）。新增四个文件：`submodule.py`（基类+@script+pack）、`loader.py`（ModuleLoader+异常）、`builtins.py`（内置 harness 集）、`tests/test_submodule.py`；小改 `config.py`/`command.py`（name 字段+序列化）、`spec.py`（SpecSchema）、`module.py`（keep_records 参数）、`__init__.py`（导出）。

**Tech Stack:** Python 3.13，pytest + unittest.mock（AsyncMock/MagicMock），tickflow（零修改），llm 客户端（mock 注入）。

**设计 spec:** `docs/superpowers/specs/2026-08-05-submodule-design.md`（已确认）。运行测试用 `python -m pytest module_harness/tests/<file> -q`。

**测试基础模式**（所有任务共用，来自现有 test_module.py）：

```python
from unittest.mock import AsyncMock, MagicMock
from llm.client import LLMResponse

@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client
# mock 返回：
mock_llm.complete.return_value = LLMResponse(
    content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
```

---

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `module_harness/config.py` | 修改 | `HarnessConfig` 加 `name` 字段 + `to_dict`/`from_dict` |
| `module_harness/command.py` | 修改 | `CommandConfig` 加 `name` 字段 + `to_dict`/`from_dict` |
| `module_harness/spec.py` | 修改 | 追加 `SpecSchema` 模型 + validate |
| `module_harness/module.py` | 修改 | `Module` 加 `keep_records` 构造参数 |
| `module_harness/builtins.py` | 新建 | 内置 harness 名表 + `register_builtin_harnesses` |
| `module_harness/submodule.py` | 新建 | `SubModule` 基类、`script` 装饰器、`SpecValidationError`、`pack()` |
| `module_harness/loader.py` | 新建 | `ModuleLoader`、`ModuleRequirementError`、`ModuleManifestError` |
| `module_harness/__init__.py` | 修改 | 导出新公共 API |
| `module_harness/tests/test_config.py` | 修改 | 序列化 round-trip 测试 |
| `module_harness/tests/test_spec.py` | 修改 | SpecSchema 测试 |
| `module_harness/tests/test_module.py` | 修改 | keep_records 测试 |
| `module_harness/tests/test_submodule.py` | 新建 | submodule/pack/loader 全套测试 |
| `docs/progress/module-roadmap.md` | 修改 | #5 标记完成 |

依赖顺序：Task 1→2 独立；Task 3（keep_records）先于 Task 5（SubModule.run 使用）；Task 4（builtins）先于 Task 5；Task 5 先于 Task 6（pack 方法在同一文件）、Task 7（loader 消费 pack 产物）。

---

### Task 1: HarnessConfig / CommandConfig 序列化 + name 字段

**Files:**
- Modify: `module_harness/config.py`
- Modify: `module_harness/command.py`
- Test: `module_harness/tests/test_config.py`

- [ ] **Step 1: 写失败测试**（追加到 `module_harness/tests/test_config.py` 末尾）

```python
class TestSerialization:
    def test_harness_config_roundtrip(self):
        cfg = HarnessConfig(
            name="translate",
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式", "casual": "随意"},
            output_format=OutputFormat(type="json_object"),
            notdo=["不要加解释"],
            model="deepseek-v4-flash",
            temperature=0.3,
            think=True,
            api_params={"extra": {"k": "v"}},
        )
        restored = HarnessConfig.from_dict(cfg.to_dict())
        assert restored == cfg

    def test_harness_config_roundtrip_no_output_format(self):
        cfg = HarnessConfig(prompt_core="x")
        assert HarnessConfig.from_dict(cfg.to_dict()) == cfg

    def test_command_config_roundtrip(self):
        cfg = CommandConfig(
            name="ls", command="ls -la", timeout=30, cwd="/tmp",
            env={"A": "1"}, capture_output=False, shell=False,
        )
        assert CommandConfig.from_dict(cfg.to_dict()) == cfg
```

（`CommandConfig` 需在文件头追加 import：`from module_harness.command import CommandConfig`。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_config.py::TestSerialization -q`
Expected: FAIL，`AttributeError: 'HarnessConfig' object has no attribute 'to_dict'`

- [ ] **Step 3: 实现**——`module_harness/config.py`：

在文件头加 `import dataclasses`，然后在 `HarnessConfig` 类内（`api_params` 字段之后）追加：

```python
    # ── 注册信息（submodule 用）──
    name: str | None = None
    """注册名。submodule 的 harnesses 列表中必须提供。"""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写 dict（含 output_format）。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HarnessConfig":
        """从 to_dict() 输出还原。"""
        data = dict(d)
        of = data.pop("output_format", None)
        if of is not None:
            data["output_format"] = OutputFormat(**of)
        return cls(**data)
```

- [ ] **Step 4: 实现**——`module_harness/command.py`：

文件头加 `import dataclasses`，`CommandConfig` 类内（`shell` 字段之后）追加：

```python
    name: str | None = None
    """注册名。submodule 的 commands 列表中必须提供。"""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CommandConfig":
        return cls(**d)
```

（若 command.py 未 import `Any`，补 `from typing import Any`。）

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_config.py -q`
Expected: PASS（含既有测试）

- [ ] **Step 6: 提交**

```bash
git add module_harness/config.py module_harness/command.py module_harness/tests/test_config.py
git commit -m "feat: add name field + to_dict/from_dict to HarnessConfig and CommandConfig"
```

---

### Task 2: SpecSchema 模型 + 校验

**Files:**
- Modify: `module_harness/spec.py`
- Test: `module_harness/tests/test_spec.py`

- [ ] **Step 1: 写失败测试**（追加到 `module_harness/tests/test_spec.py` 末尾）

```python
class TestSpecSchema:
    def test_validate_passes(self):
        schema = SpecSchema(input={"a": "str", "n": "int", "b": "bool"})
        assert schema.validate({"a": "x", "n": 1, "b": True}) == []

    def test_validate_missing_field(self):
        schema = SpecSchema(input={"a": "str"})
        errors = schema.validate({})
        assert len(errors) == 1
        assert "a" in errors[0]

    def test_validate_wrong_type(self):
        schema = SpecSchema(input={"n": "int"})
        errors = schema.validate({"n": "1"})
        assert len(errors) == 1

    def test_bool_and_int_not_interchangeable(self):
        schema = SpecSchema(input={"n": "int", "b": "bool"})
        errors = schema.validate({"n": True, "b": 1})
        assert len(errors) == 2

    def test_any_type(self):
        schema = SpecSchema(input={"x": "any"})
        assert schema.validate({"x": object()}) == []

    def test_unknown_type_declared(self):
        schema = SpecSchema(input={"x": "date"})
        errors = schema.validate({"x": "2026-01-01"})
        assert len(errors) == 1

    def test_undeclared_fields_allowed(self):
        schema = SpecSchema(input={"a": "str"})
        assert schema.validate({"a": "x", "extra": 42}) == []

    def test_default_empty_schema(self):
        assert SpecSchema().validate({}) == []
```

（import 需包含 `from module_harness.spec import SpecSchema`；若 test_spec.py 用 `from module_harness.spec import ...` 形式，并入。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_spec.py::TestSpecSchema -q`
Expected: FAIL，`ImportError: cannot import name 'SpecSchema'`

- [ ] **Step 3: 实现**——`module_harness/spec.py` 末尾追加：

```python
_SCHEMA_TYPES: dict[str, type] = {
    "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict,
}


def _value_matches(value: Any, type_name: str) -> bool:
    """判断值是否满足类型声明。bool 与 int 严格区分。"""
    if type_name == "any":
        return True
    expected = _SCHEMA_TYPES.get(type_name)
    if expected is None:
        return False
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


@dataclass
class SpecSchema:
    """submodule 的 spec 契约：input 校验，output 仅声明。"""

    input: dict[str, str] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)

    def validate(self, spec: dict[str, Any]) -> list[str]:
        """校验 spec 是否满足契约。返回错误列表，空 = 通过。

        声明的字段必须存在且类型匹配；未声明的字段允许存在。
        """
        errors: list[str] = []
        for field_name, type_name in self.input.items():
            if field_name not in spec:
                errors.append(f"缺少字段 '{field_name}'（应为 {type_name}）")
                continue
            if not _value_matches(spec[field_name], type_name):
                errors.append(
                    f"字段 '{field_name}' 类型错误：期望 {type_name}，"
                    f"实际 {type(spec[field_name]).__name__}"
                )
        return errors
```

（spec.py 已 import `dataclass, field, Any`，无需改头。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_spec.py -q`
Expected: PASS（含既有测试）

- [ ] **Step 5: 提交**

```bash
git add module_harness/spec.py module_harness/tests/test_spec.py
git commit -m "feat: add SpecSchema model with input validation"
```

---

### Task 3: Module.keep_records 参数

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_module.py`

- [ ] **Step 1: 写失败测试**（追加到 `module_harness/tests/test_module.py` 末尾，复用文件内 `mock_llm`/`setup_registry` fixture）

```python
    def test_keep_records_false(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="format_output", inputs={"data": "{spec.source_text}"})},
                flow="A",
            ),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="test_kr",
            registry=reg,
            review_harness=None,   # build_runner 不触发一致性审核
            keep_records=False,
        )
        runner = mod.build_runner()
        assert runner.run_state._keep_records is False
```

（需在文件头确认已 import `Tasklist, TaskDefinition`——已有。`inputs={"data": "__spec__"}` 引用 spec 本身，script 不读取它。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_module.py::TestModule::test_keep_records_false -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'keep_records'`

- [ ] **Step 3: 实现**——`module_harness/module.py`：

`Module.__init__` 签名（`review_harness` 参数后）追加：

```python
        keep_records: bool = True,
```

`__init__` 体内（`self.review_harness = review_harness` 附近）追加：

```python
        self.keep_records = keep_records
```

`_build_runner_async` 末尾返回值改为：

```python
        return AsyncRunner(graph, registry=reg, keep_records=self.keep_records)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_module.py -q`
Expected: PASS（默认 `keep_records=True`，既有测试不受影响）

- [ ] **Step 5: 提交**

```bash
git add module_harness/module.py module_harness/tests/test_module.py
git commit -m "feat: parameterize Module.keep_records for embedded mode"
```

---

### Task 4: 内置 harness 集（builtins.py）

**Files:**
- Create: `module_harness/builtins.py`
- Test: `module_harness/tests/test_submodule.py`（本任务先建文件，只含本任务测试）

- [ ] **Step 1: 写失败测试**——新建 `module_harness/tests/test_submodule.py`：

```python
"""SubModule / builtins / pack / ModuleLoader 测试。"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.command import CommandConfig
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.events import EventBus, ScriptCompleted
from module_harness.loader import ModuleLoader, ModuleManifestError, ModuleRequirementError
from module_harness.registry import HarnessRegistry
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SpecValidationError, SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestBuiltins:
    def test_names(self):
        assert BUILTIN_HARNESS_NAMES == frozenset({"spec_to_tasklist", "spec_tasklist_review"})

    def test_register_builtins(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus())
        register_builtin_harnesses(reg)
        for name in BUILTIN_HARNESS_NAMES:
            assert reg.harness_config(name) is not None

    def test_register_builtins_idempotent(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        register_builtin_harnesses(reg)  # 重复注册不抛异常
```

（`loader.py`/`submodule.py` 尚未创建，import 会失败——这正是 Step 2 的预期失败。为让本任务测试单独可跑，Step 1 先注释掉 Task 5-7 才会用到的 import 行 `from module_harness.loader import ...` 与 `from module_harness.submodule import ...`，Step 2 只断言 builtins 相关失败。后续任务逐步取消注释。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestBuiltins -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'module_harness.builtins'`

- [ ] **Step 3: 实现**——新建 `module_harness/builtins.py`：

```python
# module_harness/builtins.py
"""内置 harness 集 — requires 的默认提供方。"""

from __future__ import annotations

from .config import HarnessConfig, OutputFormat
from .consistency import register_review_harness
from .registry import HarnessRegistry

BUILTIN_HARNESS_NAMES: frozenset[str] = frozenset({"spec_to_tasklist", "spec_tasklist_review"})

# 翻译 harness 最小骨架；模板的 prompt_core 在翻译时覆盖（translator.py）
SPEC_TO_TASKLIST_CONFIG = HarnessConfig(
    name="spec_to_tasklist",
    prompt_core="根据 spec 生成 tasklist JSON。",
    output_format=OutputFormat(type="json_object"),
    temperature=0.3,
)


def register_builtin_harnesses(reg: HarnessRegistry) -> None:
    """注册内置 harness（spec_to_tasklist、spec_tasklist_review）。幂等，可重复调用。"""
    reg.harness("spec_to_tasklist", SPEC_TO_TASKLIST_CONFIG)
    register_review_harness(reg)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestBuiltins -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add module_harness/builtins.py module_harness/tests/test_submodule.py
git commit -m "feat: add builtin harness set (spec_to_tasklist, spec_tasklist_review)"
```

---

### Task 5: SubModule 基类 + @script + run

**Files:**
- Create: `module_harness/submodule.py`
- Test: `module_harness/tests/test_submodule.py`（追加；恢复 `from module_harness.submodule import ...` 导入）

- [ ] **Step 1: 写失败测试**——`module_harness/tests/test_submodule.py` 恢复 submodule 导入行，并追加：

```python
class Translator(SubModule):
    """测试用固定翻译 submodule。"""

    name = "test_translator"
    version = "1.0.0"
    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )
    harnesses = [
        HarnessConfig(
            name="translate",
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式", "casual": "随意"},
            output_format=OutputFormat(type="json_object"),
        ),
    ]
    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                promptmode="{spec.style}",
                inputs={"text": "{spec.source_text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_output", inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )

    @script("format_output")
    def format_output(view):
        return {"translation": view.data.value["translation"].strip()}


class TestSubModule:
    def test_scripts_collected(self):
        assert set(Translator._scripts) == {"format_output"}

    def test_no_scripts_when_none_declared(self):
        class Empty(SubModule):
            name = "empty"
        assert Empty._scripts == {}

    @pytest.mark.asyncio
    async def test_run_fixed_tasklist(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        firings = await sm.run({"source_text": "Hello", "style": "formal"}, max_ticks=10)
        assert len(firings) >= 2
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"translation": "你好世界"}

    @pytest.mark.asyncio
    async def test_spec_validation_failure(self, mock_llm):
        sm = Translator(llm_client=mock_llm)
        with pytest.raises(SpecValidationError) as ei:
            await sm.run({"source_text": "Hello"})  # 缺 style
        assert "style" in str(ei.value)

    @pytest.mark.asyncio
    async def test_run_without_tasklist_raises(self, mock_llm):
        class NoTask(SubModule):
            name = "no_task"
        with pytest.raises(ValueError, match="tasklist"):
            await NoTask(llm_client=mock_llm).run({"a": 1})

    @pytest.mark.asyncio
    async def test_audit_mode_emits_events(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=True, max_ticks=10)
        assert any(isinstance(e, ScriptCompleted) for e in got)

    @pytest.mark.asyncio
    async def test_embedded_mode_no_events(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=False, max_ticks=10)
        assert got == []

    @pytest.mark.asyncio
    async def test_custom_tasklist_with_review(self, mock_llm):
        async def fake_complete(*args, **kwargs):
            return LLMResponse(
                content='{"consistent": true, "suggestions": ""}',
                usage={}, finish_reason="end_turn",
            )
        mock_llm.complete = AsyncMock(side_effect=fake_complete)
        sm = Translator(llm_client=mock_llm)
        custom = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                    outputformat={"type": "json_object"},
                ),
            },
            flow="A",
        )
        firings = await sm.run(
            {"source_text": "Hello", "style": "formal"}, tasklist=custom, max_ticks=10)
        assert any(f.node == "A" for f in firings)
```

（`test_custom_tasklist_with_review` 中审核 harness 与执行 harness 共用同一个 mock，返回同一条 JSON；审核读到 `consistent=true` 放行，执行节点输出该 JSON 本身。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestSubModule -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'module_harness.submodule'`

- [ ] **Step 3: 实现**——新建 `module_harness/submodule.py`：

```python
# module_harness/submodule.py
"""SubModule — 类式 submodule 定义 + 嵌入/完整运行 + pack 导出。"""

from __future__ import annotations

import inspect
import json
import textwrap
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from llm import LLMConfig, create_llm_client

from .builtins import register_builtin_harnesses
from .command import CommandConfig
from .config import HarnessConfig
from .events import EventBus
from .module import Module
from .registry import HarnessRegistry
from .spec import SpecSchema, Tasklist


class SpecValidationError(Exception):
    """spec 不满足 spec_schema 契约。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("spec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))


def script(name: str):
    """类内 script 标记装饰器：标记函数，__init_subclass__ 时收集。

    脚本是类体内普通函数（不绑定 self），与 @reg.script 语义一致。
    """

    def deco(fn: Callable) -> Callable:
        fn._submodule_script_name = name  # type: ignore[attr-defined]
        return fn

    return deco


class SubModule:
    """类式 submodule 定义。类属性 = 注册信息，@script 收集脚本。

    run() 内部组合 Module：注册 provides → 构造 Module → 运行。
    pack() 导出发布目录（module.json + harnesses/ + scripts/ + commands/）。
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    spec_schema: SpecSchema = SpecSchema()
    harnesses: list[HarnessConfig] = []
    commands: list[CommandConfig] = []
    requires: list[str] = []
    tasklist: Tasklist | None = None
    _scripts: dict[str, Callable] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        inherited = dict(getattr(cls, "_scripts", {}))
        collected = {
            n: fn for n, fn in cls.__dict__.items()
            if callable(fn) and getattr(fn, "_submodule_script_name", None)
        }
        cls._scripts = inherited
        cls._scripts.update(collected)

    def __init__(
        self,
        llm_client: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._event_bus = event_bus

    def _ensure_client(self) -> Any:
        """直接类使用（未注入 client）时从 env 懒创建。"""
        if self._llm_client is None:
            self._llm_client = create_llm_client(LLMConfig.from_env())
        return self._llm_client

    def _module_id(self) -> str:
        return f"{self.name}_{uuid.uuid4().hex[:6]}"

    def _build_registry(self, audit: bool) -> HarnessRegistry:
        bus = self._event_bus if audit else EventBus.null()
        reg = HarnessRegistry(llm_client=self._ensure_client(), event_bus=bus)
        for hc in self.harnesses:
            if not hc.name:
                raise ValueError(f"harnesses 配置缺少 name: {hc}")
            reg.harness(hc.name, hc)
        for cc in self.commands:
            if not cc.name:
                raise ValueError(f"commands 配置缺少 name: {cc}")
            reg.command(cc.name, cc)
        for sname, fn in self._scripts.items():
            reg.script(sname)(fn)
        register_builtin_harnesses(reg)
        return reg

    async def run(
        self,
        spec: dict[str, Any],
        *,
        tasklist: Tasklist | dict[str, Any] | None = None,
        audit: bool = False,
        max_ticks: int = 100,
    ):
        """执行 submodule。

        - tasklist=None：用自身固定 tasklist，不触发一致性审核（发布前已验证）
        - 传入自定义 tasklist：与 Module 一致，校验 + 一致性审核
        - audit=False（默认）：嵌入模式，EventBus.null() + keep_records=False
        """
        errors = self.spec_schema.validate(spec)
        if errors:
            raise SpecValidationError(errors)
        if self.tasklist is None and tasklist is None:
            raise ValueError(f"submodule '{self.name}' 未定义 tasklist")
        use_tasklist = self.tasklist if tasklist is None else tasklist
        if isinstance(use_tasklist, dict):
            use_tasklist = Tasklist.from_json(use_tasklist)
        review = None if tasklist is None else "spec_tasklist_review"
        reg = self._build_registry(audit)
        module = Module(
            spec=spec,
            tasklist=use_tasklist,
            llm_client=self._ensure_client(),
            event_bus=self._event_bus,
            module_id=self._module_id(),
            registry=reg,
            review_harness=review,
            keep_records=audit,
        )
        return await module.run(max_ticks=max_ticks)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py -q`
Expected: PASS（TestBuiltins + TestSubModule 全过）

- [ ] **Step 5: 提交**

```bash
git add module_harness/submodule.py module_harness/tests/test_submodule.py
git commit -m "feat: add SubModule base class with @script collection and run"
```

---

### Task 6: pack() 导出发布目录

**Files:**
- Modify: `module_harness/submodule.py`（追加 pack 方法）
- Test: `module_harness/tests/test_submodule.py`（追加）

- [ ] **Step 1: 写失败测试**——`module_harness/tests/test_submodule.py` 追加：

```python
class TestPack:
    def test_pack_structure(self, tmp_path):
        out = Translator().pack(tmp_path / "dist")
        assert (out / "module.json").is_file()
        assert (out / "harnesses" / "translate.json").is_file()
        assert (out / "scripts" / "format_output.py").is_file()

    def test_pack_manifest_content(self, tmp_path):
        out = Translator().pack(tmp_path / "dist")
        manifest = json.loads((out / "module.json").read_text(encoding="utf-8"))
        assert manifest["name"] == "test_translator"
        assert manifest["submodule"] is True
        assert manifest["spec_schema"] == {
            "input": {"source_text": "str", "style": "str"},
            "output": {"translation": "str"},
        }
        assert set(manifest["tasklist"]["Tasks"]) == {"A", "B"}
        assert manifest["tasklist"]["Flow"] == "A --> B"

    def test_pack_script_source_executable(self, tmp_path):
        out = Translator().pack(tmp_path / "dist")
        src = (out / "scripts" / "format_output.py").read_text(encoding="utf-8")
        ns: dict = {}
        exec(compile(src, "format_output.py", "exec"), ns)
        assert callable(ns["format_output"])
        # 导出后的函数签名与类内一致（纯函数，无 self）
        import inspect as _inspect
        assert "self" not in _inspect.signature(ns["format_output"]).parameters

    def test_pack_requires_missing_name(self, tmp_path):
        class NoName(SubModule):
            name = ""
            tasklist = Translator.tasklist
        with pytest.raises(ValueError, match="name"):
            NoName().pack(tmp_path / "dist")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestPack -q`
Expected: FAIL，`AttributeError: 'Translator' object has no attribute 'pack'`

- [ ] **Step 3: 实现**——`module_harness/submodule.py` 的 `SubModule` 类内（`run` 方法之后）追加：

```python
    def pack(self, out_dir: str | Path) -> Path:
        """导出发布目录：module.json + harnesses/ + scripts/ + commands/。

        scripts/*.py = 函数源码 + 必要 import（含 @script 装饰器行），
        加载时 exec 后按函数名取注册，pack/load round-trip 无签名改写。
        """
        if not self.name:
            raise ValueError("submodule 缺少 name，无法打包")
        if self.tasklist is None:
            raise ValueError(f"submodule '{self.name}' 未定义 tasklist，无法打包")
        p = Path(out_dir)
        (p / "harnesses").mkdir(parents=True, exist_ok=True)
        (p / "scripts").mkdir(exist_ok=True)
        (p / "commands").mkdir(exist_ok=True)
        manifest = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "submodule": True,
            "spec_schema": asdict(self.spec_schema),
            "requires": list(self.requires),
            "tasklist": {
                "Tasks": {k: asdict(t) for k, t in self.tasklist.tasks.items()},
                "Flow": self.tasklist.flow,
            },
        }
        (p / "module.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for hc in self.harnesses:
            if not hc.name:
                raise ValueError(f"harnesses 配置缺少 name: {hc}")
            (p / "harnesses" / f"{hc.name}.json").write_text(
                json.dumps(hc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for cc in self.commands:
            if not cc.name:
                raise ValueError(f"commands 配置缺少 name: {cc}")
            (p / "commands" / f"{cc.name}.json").write_text(
                json.dumps(cc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for sname, fn in self._scripts.items():
            src = textwrap.dedent(inspect.getsource(fn))
            header = "from __future__ import annotations\nfrom module_harness.submodule import script\n\n"
            (p / "scripts" / f"{sname}.py").write_text(header + src, encoding="utf-8")
        return p
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add module_harness/submodule.py module_harness/tests/test_submodule.py
git commit -m "feat: add SubModule.pack() to export publishable module directory"
```

---

### Task 7: ModuleLoader

**Files:**
- Create: `module_harness/loader.py`
- Test: `module_harness/tests/test_submodule.py`（追加；恢复 `from module_harness.loader import ...` 导入）

- [ ] **Step 1: 写失败测试**——`module_harness/tests/test_submodule.py` 恢复 loader 导入行，并追加：

```python
class TestModuleLoader:
    def test_load_returns_instance(self, tmp_path, mock_llm):
        out = Translator().pack(tmp_path / "dist")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        assert isinstance(module, SubModule)
        assert module.name == "test_translator"
        assert set(module._scripts) == {"format_output"}

    @pytest.mark.asyncio
    async def test_load_roundtrip_run(self, tmp_path, mock_llm):
        out = Translator().pack(tmp_path / "dist")
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        firings = await module.run({"source_text": "Hello", "style": "formal"}, max_ticks=10)
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"translation": "你好世界"}

    def test_requires_missing(self, tmp_path, mock_llm):
        class NeedsX(Translator):
            name = "needs_x"
            requires = ["does_not_exist"]
        out = NeedsX().pack(tmp_path / "dist")
        with pytest.raises(ModuleRequirementError) as ei:
            ModuleLoader(llm_client=mock_llm).load(out)
        assert "does_not_exist" in str(ei.value)

    def test_requires_builtin_ok(self, tmp_path, mock_llm):
        class NeedsReview(Translator):
            name = "needs_review"
            requires = ["spec_tasklist_review"]
        out = NeedsReview().pack(tmp_path / "dist")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        assert module.name == "needs_review"

    def test_missing_manifest(self, tmp_path, mock_llm):
        with pytest.raises(ModuleManifestError):
            ModuleLoader(llm_client=mock_llm).load(tmp_path / "nope")

    def test_bad_manifest_json(self, tmp_path, mock_llm):
        d = tmp_path / "bad"
        d.mkdir()
        (d / "module.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ModuleManifestError):
            ModuleLoader(llm_client=mock_llm).load(d)

    def test_manifest_missing_name(self, tmp_path, mock_llm):
        d = tmp_path / "noname"
        d.mkdir()
        (d / "module.json").write_text(
            json.dumps({"tasklist": {"Tasks": {}, "Flow": ""}}), encoding="utf-8")
        with pytest.raises(ModuleManifestError, match="name"):
            ModuleLoader(llm_client=mock_llm).load(d)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestModuleLoader -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'module_harness.loader'`

- [ ] **Step 3: 实现**——新建 `module_harness/loader.py`：

```python
# module_harness/loader.py
"""ModuleLoader — 加载发布目录为 SubModule 实例（第二层用户入口）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm import LLMConfig, create_llm_client

from .builtins import BUILTIN_HARNESS_NAMES
from .command import CommandConfig
from .config import HarnessConfig
from .events import EventBus
from .spec import SpecSchema, Tasklist
from .submodule import SubModule


class ModuleRequirementError(Exception):
    """requires 声明的名字无法在「内置集 ∪ provides」中解析。"""

    def __init__(self, missing: list[str], available: list[str]) -> None:
        self.missing = missing
        self.available = available
        super().__init__(
            "requires 无法解析: " + ", ".join(missing)
            + "\n可用: " + ", ".join(available)
        )


class ModuleManifestError(Exception):
    """module.json 缺失、损坏或缺少必需字段。"""


class ModuleLoader:
    """第二层用户入口：加载发布目录，返回可运行的 SubModule 实例。

    llm_client 优先（测试/注入用）；否则由 llm_config（None → from_env）创建。
    """

    def __init__(
        self,
        llm_config: LLMConfig | None = None,
        *,
        llm_client: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._llm_config = llm_config or LLMConfig.from_env()
        self._event_bus = event_bus

    def _client(self) -> Any:
        if self._llm_client is None:
            self._llm_client = create_llm_client(self._llm_config)
        return self._llm_client

    def load(self, path: str | Path) -> SubModule:
        """解析 module.json → 注册 provides → 校验 requires → 返回 SubModule。"""
        p = Path(path)
        manifest_path = p / "module.json"
        if not manifest_path.is_file():
            raise ModuleManifestError(f"缺少 module.json: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ModuleManifestError(f"module.json 解析失败: {e}") from e

        name = manifest.get("name")
        tasklist_data = manifest.get("tasklist")
        if not name:
            raise ModuleManifestError("module.json 缺少 'name'")
        if tasklist_data is None:
            raise ModuleManifestError("module.json 缺少 'tasklist'")
        try:
            tasklist = Tasklist.from_json(tasklist_data)
        except (ValueError, TypeError) as e:
            raise ModuleManifestError(f"tasklist 无效: {e}") from e

        harnesses = self._load_harnesses(p)
        commands = self._load_commands(p)
        scripts = self._load_scripts(p)

        schema_data = manifest.get("spec_schema", {}) or {}
        spec_schema = SpecSchema(
            input=schema_data.get("input", {}) or {},
            output=schema_data.get("output", {}) or {},
        )
        requires = list(manifest.get("requires", []) or [])

        provides = {h.name for h in harnesses} | {c.name for c in commands} | set(scripts)
        missing = [r for r in requires if r not in BUILTIN_HARNESS_NAMES and r not in provides]
        if missing:
            raise ModuleRequirementError(missing, sorted(BUILTIN_HARNESS_NAMES | provides))

        cls = type(name, (SubModule,), {
            "name": name,
            "version": manifest.get("version", "0.1.0"),
            "description": manifest.get("description", ""),
            "spec_schema": spec_schema,
            "requires": requires,
            "tasklist": tasklist,
            "harnesses": harnesses,
            "commands": commands,
            "_scripts": scripts,
        })
        return cls(llm_client=self._client(), event_bus=self._event_bus)

    def _load_harnesses(self, p: Path) -> list[HarnessConfig]:
        result: list[HarnessConfig] = []
        for f in sorted((p / "harnesses").glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cfg = HarnessConfig.from_dict(data)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                raise ModuleManifestError(f"{f} 无效: {e}") from e
            if not cfg.name:
                raise ModuleManifestError(f"{f} 缺少 'name'")
            result.append(cfg)
        return result

    def _load_commands(self, p: Path) -> list[CommandConfig]:
        result: list[CommandConfig] = []
        for f in sorted((p / "commands").glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cfg = CommandConfig.from_dict(data)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                raise ModuleManifestError(f"{f} 无效: {e}") from e
            if not cfg.name:
                raise ModuleManifestError(f"{f} 缺少 'name'")
            result.append(cfg)
        return result

    def _load_scripts(self, p: Path) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for f in sorted((p / "scripts").glob("*.py")):
            ns: dict[str, Any] = {}
            try:
                exec(compile(f.read_text(encoding="utf-8"), str(f), "exec"), ns)
            except Exception as e:  # 脚本自身报错视为清单错误
                raise ModuleManifestError(f"{f} 加载失败: {e}") from e
            fn = ns.get(f.stem)
            if not callable(fn):
                raise ModuleManifestError(f"{f} 未定义函数 {f.stem}")
            result[f.stem] = fn
        return result
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py -q`
Expected: PASS（TestBuiltins + TestSubModule + TestPack + TestModuleLoader 全过）

- [ ] **Step 5: 提交**

```bash
git add module_harness/loader.py module_harness/tests/test_submodule.py
git commit -m "feat: add ModuleLoader with manifest parsing and requires validation"
```

---

### Task 8: __init__ 导出 + 全量回归 + roadmap 更新

**Files:**
- Modify: `module_harness/__init__.py`
- Modify: `docs/progress/module-roadmap.md`
- Test: 全量 `module_harness/tests/`

- [ ] **Step 1: 读 `module_harness/__init__.py` 现状**，按其现有结构追加导出。

- [ ] **Step 2: 写导出**——在 `__init__.py` 的 import 与 `__all__` 中追加：

```python
from .submodule import SubModule, SpecValidationError, script
from .loader import ModuleLoader, ModuleManifestError, ModuleRequirementError
from .spec import SpecSchema
```

（`__all__` 加入：`"SubModule", "script", "SpecValidationError", "ModuleLoader", "ModuleManifestError", "ModuleRequirementError", "SpecSchema"`。若 `SpecSchema` 已在 `__all__` 则跳过。）

- [ ] **Step 3: 验证导出可用**

Run: `python -c "from module_harness import SubModule, ModuleLoader, SpecSchema, script; print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 4: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部 PASS（含既有 14 个测试文件 + test_submodule.py）

- [ ] **Step 5: 更新 roadmap**——`docs/progress/module-roadmap.md`：

1. 「完成度速览」`已实现：14 / 待实现：5` → `已实现：15 / 待实现：4`
2. 把「待实现 🔲」中的「### 4. submodule（含模块打包/发布）」整段移至「已实现 ✅」，表格行：

```markdown
| **submodule — 类式定义 + 打包发布** | `SubModule`（类式声明 + `@script` + `pack()` 导出）+ `ModuleLoader`（加载 + requires 校验）+ 内置 harness 集 | `submodule.py`, `loader.py`, `builtins.py` |
```

3. 「实现顺序建议」中 `│ 5. submodule + 打包/发布│  ← 依赖 #1，含 module.json + ModuleLoader` 改为 `│ 5. submodule + 打包/发布│  ✅ 已完成`，并在 `#1 → #4 → #5 → ...` 依赖链注释中把 #5 标注完成。

- [ ] **Step 6: 提交**

```bash
git add module_harness/__init__.py docs/progress/module-roadmap.md
git commit -m "docs: mark submodule (#5) done; export SubModule/ModuleLoader API"
```

---

## 自查清单

- **Spec 覆盖**：spec 的每个条目（SubModule / pack / ModuleLoader / 序列化 / SpecSchema / 内置集 / keep_records / 嵌入模式 / 错误处理 / 测试计划 / 文件清单）都能对应到 Task 1-8。
- **类型一致性**：`HarnessConfig.name`、`CommandConfig.name` 在 Task 1 定义，Task 5/6/7 使用；`SubModule.run(spec, *, tasklist, audit, max_ticks)` 签名在 Task 5 定义，Task 5/7 测试按此调用；`SpecValidationError`/`ModuleRequirementError`/`ModuleManifestError` 定义与使用一致。
- **无占位符**：所有步骤含完整代码与命令。
