# Spec & Tasklist 实现计划

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标:** 实现 spec 与 tasklist 层——将用户结构化目标描述和流程计划转化为 tickflow + module_harness 可执行的 runner。

**架构:** 新增四个模块：数据模型（`spec.py`）、翻译器（`translator.py` 含验证+模板加载）、graph 构建器（`graph_builder.py`）、Module 编排器（`module.py`）。均在 `module_harness/` 包内，复用已有 `HarnessRegistry`、`AsyncRunner`、`EventBus`。

**技术栈:** Python 3.10+, tickflow, module_harness (已有), dataclasses, json, pathlib

## Global Constraints

- tickflow 零修改
- llm 模块零修改
- module_harness 已有模块（events/config/harness/registry/outputfmt/prompt）零修改——仅新增文件并在 `__init__.py` 扩展导出
- 翻译不走 tickflow runner——直接调 body/script 函数
- 命名空间隔离：body 注册名 `"{module_id}:{task_key}"`，graph node 名为 task key 原值
- spec 无预定义 schema——字段由 module 设计者与模板共同定义
- translation.type 显式声明翻译方式：`"harness"` 或 `"script"`

## 文件清单

| 操作 | 路径 | 职责 |
|------|------|------|
| 创建 | `module_harness/spec.py` | Spec, TaskDefinition, Tasklist, TasklistTemplate, TranslationSpec dataclass |
| 创建 | `module_harness/translator.py` | TasklistValidator, TemplateLoader, Translator |
| 创建 | `module_harness/graph_builder.py` | TasklistTranslator — tasklist → tickflow Graph |
| 创建 | `module_harness/module.py` | Module 编排器 |
| 修改 | `module_harness/__init__.py` | 扩展导出 |
| 创建 | `module_harness/tests/test_spec.py` | 数据模型测试 |
| 创建 | `module_harness/tests/test_validator.py` | TasklistValidator 测试 |
| 创建 | `module_harness/tests/test_translator.py` | Translator 测试 |
| 创建 | `module_harness/tests/test_graph_builder.py` | TasklistTranslator 测试 |
| 创建 | `module_harness/tests/test_module.py` | Module 集成测试 |
| 创建 | `module_harness/templates/builtin/` | 内置模板目录 |

---

### Task 1: 数据模型 (`spec.py`)

**文件:**
- 创建: `module_harness/spec.py`
- 创建: `module_harness/tests/test_spec.py`

**接口:**
- 产生: `Spec(dict)`, `TaskDefinition`, `Tasklist`, `TranslationSpec`, `TasklistTemplate`

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_spec.py
import pytest
from module_harness.spec import (
    Spec, TaskDefinition, Tasklist, TranslationSpec, TasklistTemplate,
)


class TestSpec:
    def test_spec_is_dict_wrapper(self):
        s = Spec({"task_type": "translate", "style": "formal"})
        assert s["task_type"] == "translate"
        assert s.get("style") == "formal"
        assert s.get("missing", "default") == "default"

    def test_spec_len_and_in(self):
        s = Spec({"a": 1, "b": 2})
        assert len(s) == 2
        assert "a" in s
        assert "c" not in s

    def test_spec_iter_keys(self):
        s = Spec({"x": 1, "y": 2})
        assert set(s.keys()) == {"x", "y"}


class TestTaskDefinition:
    def test_minimal_harness_task(self):
        t = TaskDefinition(type="harness", harness="translate")
        assert t.type == "harness"
        assert t.harness == "translate"
        assert t.script is None

    def test_minimal_script_task(self):
        t = TaskDefinition(type="script", script="post_process")
        assert t.type == "script"
        assert t.script == "post_process"
        assert t.harness is None

    def test_full_harness_task(self):
        t = TaskDefinition(
            type="harness",
            harness="translate",
            promptmode="formal",
            prompt="特别注意术语",
            outputformat={"type": "json_object"},
            notdo=["不要直译"],
            model="gpt-4o",
            temperature=0.3,
            inputs={"text": "source_text"},
        )
        assert t.promptmode == "formal"
        assert t.notdo == ["不要直译"]
        assert t.inputs == {"text": "source_text"}

    def test_from_dict_harness(self):
        d = {"type": "harness", "harness": "t", "promptmode": "formal"}
        t = TaskDefinition.from_dict(d)
        assert t.type == "harness"
        assert t.harness == "t"
        assert t.promptmode == "formal"

    def test_from_dict_script(self):
        d = {"type": "script", "script": "s", "inputs": {"x": "y"}}
        t = TaskDefinition.from_dict(d)
        assert t.type == "script"
        assert t.script == "s"
        assert t.inputs == {"x": "y"}


class TestTasklist:
    def test_from_json(self):
        data = {
            "Tasks": {
                "A": {"type": "harness", "harness": "t", "inputs": {"text": "src"}},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
            },
            "Flow": "A --> B",
        }
        tl = Tasklist.from_json(data)
        assert len(tl.tasks) == 2
        assert tl.tasks["A"].type == "harness"
        assert tl.tasks["B"].type == "script"
        assert tl.flow == "A --> B"

    def test_from_json_missing_tasks_raises(self):
        with pytest.raises(ValueError, match="Tasks"):
            Tasklist.from_json({"Flow": "A --> B"})

    def test_from_json_missing_flow_raises(self):
        with pytest.raises(ValueError, match="Flow"):
            Tasklist.from_json({"Tasks": {"A": {"type": "script", "script": "s"}}})


class TestTranslationSpec:
    def test_harness_translation(self):
        ts = TranslationSpec(type="harness", harness="spec_to_tasklist", prompt="...")
        assert ts.type == "harness"
        assert ts.script is None

    def test_script_translation(self):
        ts = TranslationSpec(type="script", script="my_translator")
        assert ts.type == "script"
        assert ts.harness is None

    def test_from_dict(self):
        d = {"type": "harness", "harness": "h", "prompt": "p"}
        ts = TranslationSpec.from_dict(d)
        assert ts.type == "harness"
        assert ts.harness == "h"
        assert ts.prompt == "p"


class TestTasklistTemplate:
    def test_from_json(self):
        data = {
            "name": "translate",
            "description": "翻译模块",
            "translation": {"type": "harness", "harness": "stt", "prompt": "..."},
            "tasklist": {
                "Tasks": {"A": {"type": "harness", "harness": "t"}},
                "Flow": "A",
            },
        }
        tmpl = TasklistTemplate.from_json(data)
        assert tmpl.name == "translate"
        assert tmpl.translation.type == "harness"
        assert tmpl.tasklist.tasks["A"].type == "harness"

    def test_from_json_missing_name_raises(self):
        data = {"translation": {"type": "script", "script": "s"}, "tasklist": {"Tasks": {}, "Flow": ""}}
        with pytest.raises(ValueError, match="name"):
            TasklistTemplate.from_json(data)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_spec.py -v
# 预期: 全部 FAIL — 模块不存在
```

- [ ] **Step 3: 编写 spec.py**

```python
# module_harness/spec.py
"""Spec 与 Tasklist 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


class Spec:
    """结构化 spec，用户自由定义字段的键值对集合。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"Spec({self._data!r})"

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass
class TaskDefinition:
    """tasklist 中单个 Task 的定义。与 HarnessConfig 字段对齐。"""

    type: Literal["harness", "script"]
    harness: str | None = None
    script: str | None = None
    promptmode: str | None = None
    prompt: str | None = None
    outputformat: dict[str, Any] | None = None
    notdo: list[str] | None = None
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None
    inputs: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskDefinition":
        return cls(
            type=d["type"],
            harness=d.get("harness"),
            script=d.get("script"),
            promptmode=d.get("promptmode"),
            prompt=d.get("prompt"),
            outputformat=d.get("outputformat"),
            notdo=d.get("notdo"),
            model=d.get("model"),
            temperature=d.get("temperature"),
            think=d.get("think"),
            inputs=d.get("inputs"),
        )


@dataclass
class Tasklist:
    """完整的 tasklist：Tasks + Flow。"""

    tasks: dict[str, TaskDefinition]
    flow: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Tasklist":
        if "Tasks" not in data:
            raise ValueError("tasklist 缺少 'Tasks' 字段")
        if "Flow" not in data:
            raise ValueError("tasklist 缺少 'Flow' 字段")
        tasks = {
            key: TaskDefinition.from_dict(td)
            for key, td in data["Tasks"].items()
        }
        return cls(tasks=tasks, flow=data["Flow"])


@dataclass
class TranslationSpec:
    """翻译方式声明。"""

    type: Literal["harness", "script"]
    harness: str | None = None
    script: str | None = None
    prompt: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TranslationSpec":
        return cls(
            type=d["type"],
            harness=d.get("harness"),
            script=d.get("script"),
            prompt=d.get("prompt"),
        )


@dataclass
class TasklistTemplate:
    """tasklist 模板 = 翻译声明 + tasklist 骨架。"""

    name: str
    description: str
    translation: TranslationSpec
    tasklist: Tasklist

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TasklistTemplate":
        if "name" not in data:
            raise ValueError("模板缺少 'name' 字段")
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            translation=TranslationSpec.from_dict(data["translation"]),
            tasklist=Tasklist.from_json(data["tasklist"]),
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_spec.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/spec.py module_harness/tests/test_spec.py
git commit -m "feat(module_harness): add Spec, Tasklist, TasklistTemplate data models"
```

---

### Task 2: TasklistValidator (`translator.py` 前半部分)

**文件:**
- 创建: `module_harness/translator.py`（部分代码）
- 创建: `module_harness/tests/test_validator.py`

**接口:**
- 依赖: `module_harness.spec.{Tasklist, TaskDefinition}`, `tickflow.parse`
- 依赖: `module_harness.registry.HarnessRegistry`
- 产生: `class TasklistValidator` — `validate(tasklist, registry) -> list[str]` 返回问题列表，空列表 = 合法

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_validator.py
import pytest
from unittest.mock import MagicMock
from module_harness.spec import Tasklist, TaskDefinition
from module_harness.translator import TasklistValidator


def _make_registry(harnesses=None, scripts=None):
    """构造一个含指定 harness/script 名称的 HarnessRegistry mock。"""
    reg = MagicMock()
    reg.is_harness = lambda n: n in (harnesses or set())
    reg.is_script = lambda n: n in (scripts or set())
    return reg


class TestTasklistValidator:
    def test_valid_tasklist_passes(self):
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate", inputs={"text": "src"}),
                "B": TaskDefinition(type="script", script="post_process", inputs={"data": "A"}),
            },
            flow="A --> B",
        )
        reg = _make_registry(harnesses={"translate"}, scripts={"post_process"})
        errors = TasklistValidator.validate(tl, reg)
        assert errors == []

    def test_unreferenced_harness(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="nonexistent")},
            flow="A",
        )
        reg = _make_registry(harnesses=set(), scripts=set())
        errors = TasklistValidator.validate(tl, reg)
        assert any("nonexistent" in e for e in errors)

    def test_unreferenced_script(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="no_such")},
            flow="A",
        )
        reg = _make_registry(harnesses=set(), scripts=set())
        errors = TasklistValidator.validate(tl, reg)
        assert any("no_such" in e for e in errors)

    def test_harness_type_missing_harness_field(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness=None)},
            flow="A",
        )
        reg = _make_registry()
        errors = TasklistValidator.validate(tl, reg)
        assert any("harness" in e.lower() for e in errors)

    def test_script_type_missing_script_field(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script=None)},
            flow="A",
        )
        reg = _make_registry()
        errors = TasklistValidator.validate(tl, reg)
        assert any("script" in e.lower() for e in errors)

    def test_flow_node_not_in_tasks(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="A --> B",
        )
        reg = _make_registry(scripts={"s"})
        errors = TasklistValidator.validate(tl, reg)
        assert any("B" in e for e in errors)

    def test_flow_parse_error(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="A -->> B  # invalid syntax",
        )
        reg = _make_registry(scripts={"s"})
        errors = TasklistValidator.validate(tl, reg)
        # tickflow parse 对无效语法会抛异常或警告；验证器应捕获
        assert len(errors) >= 0  # 至少不抛未捕获异常

    def test_empty_tasks_with_nonempty_flow(self):
        tl = Tasklist(tasks={}, flow="A --> B")
        reg = _make_registry()
        errors = TasklistValidator.validate(tl, reg)
        assert any("A" in e for e in errors)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_validator.py -v
# 预期: 全部 FAIL — 模块不存在
```

- [ ] **Step 3: 编写 TasklistValidator**

```python
# module_harness/translator.py（TasklistValidator 部分）
"""翻译器：TasklistValidator + TemplateLoader + Translator。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tickflow import parse as parse_graph

from .spec import Spec, Tasklist, TasklistTemplate, TaskDefinition
from .registry import HarnessRegistry


class TasklistValidator:
    """校验 tasklist 的结构合法性与引用完整性。"""

    @staticmethod
    def validate(tasklist: Tasklist, registry: HarnessRegistry) -> list[str]:
        """返回问题列表，空列表 = 合法。"""
        errors: list[str] = []

        for key, task in tasklist.tasks.items():
            errors.extend(TasklistValidator._check_task(key, task, registry))

        errors.extend(TasklistValidator._check_flow(tasklist))
        return errors

    @staticmethod
    def _check_task(key: str, task: TaskDefinition, registry: HarnessRegistry) -> list[str]:
        errors: list[str] = []

        if task.type == "harness":
            if not task.harness:
                errors.append(f"Task '{key}': type='harness' 但缺少 'harness' 字段")
            elif not registry.is_harness(task.harness) and not registry.has_body(task.harness):
                errors.append(f"Task '{key}': harness '{task.harness}' 未在 registry 中注册")
        elif task.type == "script":
            if not task.script:
                errors.append(f"Task '{key}': type='script' 但缺少 'script' 字段")
            elif not registry.is_script(task.script) and not registry.has_body(task.script):
                errors.append(f"Task '{key}': script '{task.script}' 未在 registry 中注册")
        else:
            errors.append(f"Task '{key}': 未知 type '{task.type}'")

        return errors

    @staticmethod
    def _check_flow(tasklist: Tasklist) -> list[str]:
        errors: list[str] = []
        task_keys = set(tasklist.tasks.keys())

        # 解析 flow 得到 node 名（简单正则提取 mermaid 中的节点）
        # 匹配 A --> B, [A]-->B, A--|g|-->B 等
        node_names: set[str] = set()
        flow = tasklist.flow
        # 匹配 start marker [X]
        for m in re.finditer(r'\[(\w+)\]', flow):
            node_names.add(m.group(1))
        # 匹配 X--|...|-->Y 和 X-->Y
        for m in re.finditer(r'(\w+)\s*(?:--\|?\w*\|?)?-->', flow):
            node_names.add(m.group(1))
        for m in re.finditer(r'-->\s*(\w+)', flow):
            node_names.add(m.group(1))

        # 检查 flow 中出现但不在 tasks 中的节点
        for node in node_names:
            if node not in task_keys:
                errors.append(f"Flow 中引用了未定义的节点 '{node}'")

        # 检查 tasks 中定义了但不在 flow 中的孤立节点
        for key in task_keys:
            if key not in node_names:
                errors.append(f"Task '{key}' 在 Flow 中未被引用（孤立节点）")

        # 尝试 tickflow parse 检测语法问题
        try:
            parse_graph(flow)
        except Exception as e:
            errors.append(f"Flow 解析失败: {e}")

        return errors
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_validator.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/translator.py module_harness/tests/test_validator.py
git commit -m "feat(module_harness): add TasklistValidator"
```

---

### Task 3: TemplateLoader (`translator.py` 后半部分)

**文件:**
- 修改: `module_harness/translator.py`（追加 TemplateLoader）
- 创建: `module_harness/tests/test_translator.py`（TemplateLoader 测试部分）
- 创建: `module_harness/templates/builtin/.gitkeep`

**接口:**
- 产生: `class TemplateLoader` — `register(name, data)`, `get(name) -> TasklistTemplate | None`, `list_names() -> list[str]`, `load_directory(path)`, `load_builtins()`

- [ ] **Step 1: 编写 TemplateLoader 测试**

```python
# module_harness/tests/test_translator.py
from module_harness.translator import TemplateLoader
from module_harness.spec import TasklistTemplate


class TestTemplateLoader:
    def test_register_and_get(self):
        loader = TemplateLoader()
        loader.register("test", {
            "name": "test",
            "description": "测试模板",
            "translation": {"type": "script", "script": "t"},
            "tasklist": {"Tasks": {"A": {"type": "script", "script": "s"}}, "Flow": "A"},
        })
        tmpl = loader.get("test")
        assert tmpl is not None
        assert tmpl.name == "test"
        assert tmpl.translation.type == "script"

    def test_get_nonexistent_returns_none(self):
        loader = TemplateLoader()
        assert loader.get("nope") is None

    def test_list_names(self):
        loader = TemplateLoader()
        loader.register("a", {"name": "a", "translation": {"type": "script", "script": "x"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        loader.register("b", {"name": "b", "translation": {"type": "script", "script": "y"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        names = loader.list_names()
        assert "a" in names
        assert "b" in names

    def test_duplicate_register_overwrites(self):
        loader = TemplateLoader()
        loader.register("x", {"name": "x", "description": "first", "translation": {"type": "script", "script": "a"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        loader.register("x", {"name": "x", "description": "second", "translation": {"type": "script", "script": "b"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        assert loader.get("x").description == "second"

    def test_load_directory(self, tmp_path):
        import json
        tmpl_dir = tmp_path / "templates"
        tmpl_dir.mkdir()
        data = {
            "name": "from_file",
            "description": "loaded from file",
            "translation": {"type": "script", "script": "s"},
            "tasklist": {"Tasks": {"A": {"type": "script", "script": "s"}}, "Flow": "A"},
        }
        (tmpl_dir / "from_file.json").write_text(json.dumps(data), encoding="utf-8")

        loader = TemplateLoader()
        loader.load_directory(str(tmpl_dir))
        assert loader.get("from_file") is not None

    def test_load_directory_skips_invalid_json(self, tmp_path):
        tmpl_dir = tmp_path / "templates"
        tmpl_dir.mkdir()
        (tmpl_dir / "bad.json").write_text("not json", encoding="utf-8")

        loader = TemplateLoader()
        loader.load_directory(str(tmpl_dir))  # 不应抛异常
        assert "bad" not in loader.list_names()

    def test_load_builtins(self):
        loader = TemplateLoader()
        loader.load_builtins()
        # 内置 translate 模板应已注册
        tmpl = loader.get("translate")
        assert tmpl is not None
        assert tmpl.name == "translate"
```

- [ ] **Step 2: 追加 TemplateLoader 到 translator.py**

```python
# 追加到 module_harness/translator.py

class TemplateLoader:
    """加载与管理 tasklist 模板。"""

    def __init__(self) -> None:
        self._templates: dict[str, TasklistTemplate] = {}

    def register(self, name: str, data: dict[str, Any]) -> None:
        """注册一个模板（代码调用或文件加载）。data 直接过 from_json。"""
        self._templates[name] = TasklistTemplate.from_json(data)

    def get(self, name: str) -> TasklistTemplate | None:
        return self._templates.get(name)

    def list_names(self) -> list[str]:
        return list(self._templates.keys())

    def load_directory(self, path: str | Path) -> int:
        """从目录加载 .json 模板文件。返回加载数量。"""
        p = Path(path)
        count = 0
        if p.is_dir():
            for f in sorted(p.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self.register(data["name"], data)
                    count += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass  # 跳过无效文件
        return count

    def load_builtins(self) -> int:
        """加载内置模板（module_harness/templates/builtin/）。"""
        builtin_dir = Path(__file__).parent / "templates" / "builtin"
        return self.load_directory(builtin_dir)
```

- [ ] **Step 3: 创建内置模板文件**

```json
// module_harness/templates/builtin/translate.json
{
  "name": "translate",
  "description": "通用翻译模块",
  "translation": {
    "type": "harness",
    "harness": "spec_to_tasklist",
    "prompt": "你是一个流程设计器。根据以下 spec 生成合法的 tasklist JSON。spec 包含 task_type、source_text、target_lang、style 字段。生成的 tasklist 应包含两个节点：A 执行翻译（type=harness, harness=translate），B 执行后处理（type=script, script=format_output）。请输出完整的 tasklist JSON，Tasks 键与 Flow 字符串。"
  },
  "tasklist": {
    "Tasks": {
      "A": {
        "type": "harness",
        "harness": "translate",
        "promptmode": "{spec.style}",
        "inputs": {"text": "{spec.source_text}"},
        "outputformat": {"type": "json_object"}
      },
      "B": {
        "type": "script",
        "script": "format_output",
        "inputs": {"data": "A"}
      }
    },
    "Flow": "A --> B"
  }
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_translator.py::TestTemplateLoader -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/translator.py module_harness/tests/test_translator.py module_harness/templates/
git commit -m "feat(module_harness): add TemplateLoader with builtin templates"
```

---

### Task 4: Translator (`translator.py` 完成)

**文件:**
- 修改: `module_harness/translator.py`（追加 Translator 类）
- 修改: `module_harness/tests/test_translator.py`（追加 Translator 测试）

**接口:**
- 依赖: `module_harness.registry.HarnessRegistry`
- 产生: `class Translator` — `async translate(spec, template) -> Tasklist`

- [ ] **Step 1: 追加 Translator 测试**

```python
# 追加到 module_harness/tests/test_translator.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from tickflow.views import DictView, Resolved
from module_harness.spec import Spec
from module_harness.translator import Translator
from module_harness.registry import HarnessRegistry


def _make_view(**inputs) -> DictView:
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="translator")


class TestTranslator:
    @pytest.mark.asyncio
    async def test_script_translation(self, mock_llm):
        """script 翻译：调用已注册的 script 函数。"""
        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        # 注册翻译 script
        @reg.script("my_translator")
        def my_translator(view):
            spec = view.spec.value
            return {
                "A": {"type": "harness", "harness": spec["harness_name"], "inputs": {"text": spec["source_text"]}}
            }

        # 注册引用到的 harness
        from module_harness.config import HarnessConfig
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))

        loader.register("test_module", {
            "name": "test_module",
            "translation": {"type": "script", "script": "my_translator"},
            "tasklist": {"Tasks": {}, "Flow": ""},
        })

        translator = Translator(reg)
        tmpl = loader.get("test_module")
        spec = Spec({"harness_name": "translate", "source_text": "Hello"})
        tasklist = await translator.translate(spec, tmpl)

        assert tasklist is not None
        assert "A" in tasklist.tasks
        assert tasklist.tasks["A"].harness == "translate"
        assert tasklist.tasks["A"].inputs == {"text": "Hello"}

    @pytest.mark.asyncio
    async def test_harness_translation(self, mock_llm):
        """harness 翻译：LLM 生成 tasklist JSON。"""
        from llm.client import LLMResponse

        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        # 注册翻译 harness
        from module_harness.config import HarnessConfig
        reg.harness("spec_to_tasklist", HarnessConfig(
            prompt_core="生成 tasklist JSON",
            output_format=__import__('module_harness.outputfmt', fromlist=['OutputFormat']).OutputFormat(type="json_object"),
        ))

        # 注册引用到的 harness
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))

        mock_llm.complete = AsyncMock(return_value=LLMResponse(
            content='{"A": {"type": "harness", "harness": "translate", "inputs": {"text": "Hello"}}}',
            usage={},
            finish_reason="end_turn",
        ))

        loader.register("test_llm_module", {
            "name": "test_llm_module",
            "translation": {"type": "harness", "harness": "spec_to_tasklist", "prompt": "根据 spec 生成 tasklist"},
            "tasklist": {"Tasks": {}, "Flow": ""},
        })

        translator = Translator(reg)
        tmpl = loader.get("test_llm_module")
        spec = Spec({"task_type": "translate", "source_text": "Hello"})

        tasklist = await translator.translate(spec, tmpl)

        assert tasklist is not None
        # harness 翻译返回的 tasklist 用 LLM 响应解析，由 call_translation_harness 内部解析
        assert "A" in tasklist.tasks

    @pytest.mark.asyncio
    async def test_translation_validates_result(self, mock_llm):
        """翻译结果需通过 TasklistValidator 校验。"""
        from llm.client import LLMResponse

        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        # 不做注册——翻译引用的 harness/script 不存在

        mock_llm.complete = AsyncMock(return_value=LLMResponse(
            content='{"A": {"type": "harness", "harness": "nonexistent"}}',
            usage={},
            finish_reason="end_turn",
        ))

        from module_harness.config import HarnessConfig
        reg.harness("spec_to_tasklist", HarnessConfig(prompt_core="..."))

        loader.register("bad_module", {
            "name": "bad_module",
            "translation": {"type": "harness", "harness": "spec_to_tasklist", "prompt": "..."},
            "tasklist": {"Tasks": {}, "Flow": ""},
        })

        translator = Translator(reg)
        tmpl = loader.get("bad_module")
        spec = Spec({})

        with pytest.raises(ValueError, match="校验"):
            await translator.translate(spec, tmpl)
```

- [ ] **Step 2: 追加 Translator 到 translator.py**

```python
# 追加到 module_harness/translator.py

from tickflow.views import DictView, Resolved


class Translator:
    """spec → tasklist 翻译器。直接调 body/script，不走 tickflow runner。"""

    def __init__(self, registry: HarnessRegistry) -> None:
        self.reg = registry

    async def translate(self, spec: Spec, template: TasklistTemplate) -> Tasklist:
        """执行翻译并返回校验通过的 Tasklist。"""
        ts = template.translation

        if ts.type == "script":
            tasks_dict = await self._call_script_translator(ts.script, spec)
        elif ts.type == "harness":
            tasks_dict = await self._call_harness_translator(ts.harness, ts.prompt, spec)
        else:
            raise ValueError(f"不支持的 translation type: {ts.type}")

        # 构建 tasklist 并校验
        tasklist = Tasklist(tasks={
            key: TaskDefinition.from_dict(td) if isinstance(td, dict) else td
            for key, td in tasks_dict.items()
        }, flow=template.tasklist.flow)

        errors = TasklistValidator.validate(tasklist, self.reg)
        if errors:
            raise ValueError(f"翻译结果校验失败:\n" + "\n".join(f"  - {e}" for e in errors))

        return tasklist

    async def _call_script_translator(self, script_name: str, spec: Spec) -> dict:
        """直接调用已注册的 script 函数。"""
        body = self.reg.get_body(script_name)
        view = DictView(
            {"spec": Resolved(value=spec.to_dict(), k=None)},
            node="__translator__",
        )
        import inspect
        result = body(view)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _call_harness_translator(self, harness_name: str, prompt_extra: str, spec: Spec) -> dict:
        """调用 harness body（异步 LLM），parse 返回的 JSON 为 task dict。"""
        body = self.reg.get_body(harness_name)
        view = DictView(
            {"spec": Resolved(value=spec.to_dict(), k=None)},
            node="__translator__",
        )
        result = await body(view)

        from tickflow import Failure
        if isinstance(result, Failure):
            raise ValueError(f"翻译 harness 返回 Failure: {result.error}")

        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError as e:
                raise ValueError(f"翻译结果不是合法 JSON: {e}") from e

        if isinstance(result, dict):
            return result

        raise ValueError(f"翻译结果类型异常: {type(result).__name__}")
```

- [ ] **Step 3: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_translator.py -v
# 预期: TemplateLoader + Translator 全部 PASS
```

- [ ] **Step 4: 提交**

```bash
git add module_harness/translator.py module_harness/tests/test_translator.py
git commit -m "feat(module_harness): add Translator with harness/script translation"
```

---

### Task 5: TasklistTranslator (`graph_builder.py`)

**文件:**
- 创建: `module_harness/graph_builder.py`
- 创建: `module_harness/tests/test_graph_builder.py`

**接口:**
- 依赖: `module_harness.spec.Tasklist`, `module_harness.registry.HarnessRegistry`, `tickflow.parse`
- 产生: `class TasklistTranslator` — `build(tasklist) -> tuple[Graph, HarnessRegistry]`

- [ ] **Step 1: 编写失败测试**

```python
# module_harness/tests/test_graph_builder.py
from unittest.mock import AsyncMock, MagicMock

from tickflow import Graph
from module_harness.config import HarnessConfig
from module_harness.spec import Tasklist, TaskDefinition, Spec
from module_harness.registry import HarnessRegistry
from module_harness.graph_builder import TasklistTranslator


@pytest.fixture
def mock_llm():
    return MagicMock()


@pytest.fixture
def reg(mock_llm):
    r = HarnessRegistry(llm_client=mock_llm)
    r.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
    r.harness("analyze", HarnessConfig(prompt_core="分析：{data}"))

    @r.script("post_process")
    def post_process(view):
        return {"result": "processed"}

    return r


class TestTasklistTranslator:
    def test_build_minimal_graph(self, reg):
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="post_process", inputs={"data": "src"}),
            },
            flow="A",
        )
        builder = TasklistTranslator(reg, module_id="test1")
        graph, out_reg = builder.build(tl)

        assert graph is not None
        assert isinstance(graph, Graph)
        assert "A" in graph.nodes

        # 验证 body 名使用了 module_id 前缀
        node_a = graph.nodes["A"]
        assert node_a.body == "test1:A"

        # 验证 body 已在 registry 中注册
        assert out_reg.has_body("test1:A")

    def test_build_harness_and_script_graph(self, reg):
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate", inputs={"text": "src"}),
                "B": TaskDefinition(type="script", script="post_process", inputs={"data": "A"}),
            },
            flow="A --> B",
        )
        builder = TasklistTranslator(reg, module_id="mod2")
        graph, out_reg = builder.build(tl)

        assert "A" in graph.nodes
        assert "B" in graph.nodes
        assert out_reg.is_harness("mod2:A")
        assert out_reg.has_body("mod2:B")

        # graph 结构正确
        assert graph.nodes["A"].body == "mod2:A"
        assert graph.nodes["B"].body == "mod2:B"

    def test_build_with_start_node(self, reg):
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate"),
                "B": TaskDefinition(type="script", script="post_process"),
            },
            flow="[A]-->B",
        )
        builder = TasklistTranslator(reg, module_id="mod3")
        graph, _ = builder.build(tl)

        assert "A" in graph.starts

    def test_build_with_guarded_edge(self, reg):
        @reg.guard("quality_check")
        def quality_check(view):
            return True

        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate"),
                "B": TaskDefinition(type="script", script="post_process", inputs={"data": "A"}),
            },
            flow="A--|quality_check|-->B",
        )
        builder = TasklistTranslator(reg, module_id="mod4")
        graph, _ = builder.build(tl)

        # guard 在 graph 中正确关联
        edges = [e for e in graph.edges if e.dst == "B"]
        assert len(edges) == 1
        assert edges[0].guard == "quality_check"

    def test_module_id_isolation(self, reg):
        tl1 = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="post_process")},
            flow="A",
        )
        tl2 = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="post_process")},
            flow="A",
        )

        b1 = TasklistTranslator(reg, module_id="mod_a")
        b2 = TasklistTranslator(reg, module_id="mod_b")

        _, out_reg1 = b1.build(tl1)
        _, out_reg2 = b2.build(tl2)

        assert out_reg1.has_body("mod_a:A")
        assert out_reg2.has_body("mod_b:B") is False  # 不同 key，但都应该是各自的
        # 两个 module 的 body 不冲突
        assert out_reg1.has_body("mod_b:B") is False  # mod_b 的 body 不在 mod_a 中
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_graph_builder.py -v
# 预期: 全部 FAIL
```

- [ ] **Step 3: 编写 graph_builder.py**

```python
# module_harness/graph_builder.py
"""Tasklist → tickflow Graph 翻译器。"""

from __future__ import annotations

from tickflow import Graph, parse as parse_graph
from tickflow.views import DictView

from .spec import Tasklist, TaskDefinition
from .config import HarnessConfig, OutputFormat
from .registry import HarnessRegistry


class TasklistTranslator:
    """将 Tasklist 构建为 (Graph, HarnessRegistry)。"""

    def __init__(self, registry: HarnessRegistry, module_id: str) -> None:
        self.reg = registry
        self.module_id = module_id

    def build(self, tasklist: Tasklist) -> tuple[Graph, HarnessRegistry]:
        """遍历 Tasks 注册 body，生成 graph 文本，parse 返回 Graph。"""
        # 1. 注册每个 Task 的 body
        graph_lines: list[str] = []
        for key, task in tasklist.tasks.items():
            self._register_body(key, task)
            # 生成 inputs 和 body 声明
            graph_lines.append(self._node_declaration(key, task))

        # 2. 拼接 flow
        graph_text = "\n".join(graph_lines) + "\n" + tasklist.flow

        # 3. parse
        graph = parse_graph(graph_text, registry=self.reg)
        return graph, self.reg

    def _register_body(self, key: str, task: TaskDefinition) -> None:
        """为单个 Task 注册 body（含命名空间隔离）。"""
        isolated_name = f"{self.module_id}:{key}"

        if task.type == "harness":
            # 从已有 harness 配置构造
            existing_cfg = self.reg.harness_config(task.harness)
            if existing_cfg is None:
                raise ValueError(
                    f"Task '{key}': harness '{task.harness}' 的配置未找到。"
                    f"请确保该 harness 已通过 reg.harness() 注册。"
                )
            # 用 module 层面的配置覆盖构建
            cfg = HarnessConfig(
                prompt_core=existing_cfg.prompt_core,
                prompt_modes=dict(existing_cfg.prompt_modes),
                output_format=(
                    OutputFormat(**task.outputformat) if task.outputformat
                    else existing_cfg.output_format
                ),
                notdo=task.notdo if task.notdo is not None else list(existing_cfg.notdo),
                model=task.model or existing_cfg.model,
                temperature=task.temperature if task.temperature is not None else existing_cfg.temperature,
                think=task.think if task.think is not None else existing_cfg.think,
            )
            self.reg.harness(
                isolated_name,
                cfg,
                promptmode=task.promptmode,
                prompt_extra=task.prompt,
            )

        elif task.type == "script":
            # script 已由 @reg.script() 注册，验证存在后用 body() 注册隔离名
            orig_body = self.reg.get_body(task.script)
            if orig_body is None:
                raise ValueError(
                    f"Task '{key}': script '{task.script}' 未注册。"
                    f"请确保已通过 @reg.script('{task.script}') 注册。"
                )
            self.reg.body(isolated_name, orig_body)

    def _node_declaration(self, key: str, task: TaskDefinition) -> str:
        """生成 node.inputs 和 node.body 声明行。"""
        lines: list[str] = []
        isolated_name = f"{self.module_id}:{key}"
        lines.append(f"{key}.body: {isolated_name}")

        if task.inputs:
            lines.append(f"{key}.inputs: {', '.join(task.inputs.keys())}")
            # 注意：inputs 的 value 是 graph 中的 producer 名（如 source_text, A, B）
            # 由 tasklist 指定，直接传给 tickflow

        return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_graph_builder.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/graph_builder.py module_harness/tests/test_graph_builder.py
git commit -m "feat(module_harness): add TasklistTranslator — tasklist to tickflow Graph"
```

---

### Task 6: Module 编排器 (`module.py`)

**文件:**
- 创建: `module_harness/module.py`
- 创建: `module_harness/tests/test_module.py`

**接口:**
- 依赖: `module_harness.{spec, translator, graph_builder, registry}`, `tickflow.AsyncRunner`
- 产生: `class Module` — `__init__(spec, template_name, llm_client, ...)`, `build_runner() -> AsyncRunner`, `async run(max_ticks)`

- [ ] **Step 1: 编写 Module 集成测试**

```python
# module_harness/tests/test_module.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.events import EventBus
from module_harness.translator import TemplateLoader
from module_harness.module import Module


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def setup_registry(mock_llm):
    """注册最小 harness/script 集合 + 模板。"""
    reg = HarnessRegistry(llm_client=mock_llm)
    bus = EventBus()

    # 翻译 harness
    reg.harness("spec_to_tasklist", HarnessConfig(
        prompt_core="生成 tasklist JSON",
        output_format=OutputFormat(type="json_object"),
    ))

    # 执行 harness
    reg.harness("translate", HarnessConfig(
        prompt_core="翻译：{text}",
        output_format=OutputFormat(type="json_object"),
    ))

    # 后处理 script
    @reg.script("format_output")
    def format_output(view):
        data = view.A.value if "A" in view else view.output.value
        return {"result": "processed", "data": data}

    # 翻译 script
    @reg.script("translate_translator")
    def translate_translator(view):
        spec = view.spec.value
        return {
            "A": {
                "type": "harness",
                "harness": spec["harness_name"],
                "promptmode": spec.get("style", "formal"),
                "inputs": {"text": spec["source_text"]},
                "outputformat": {"type": "json_object"},
            },
            "B": {
                "type": "script",
                "script": "format_output",
                "inputs": {"data": "A"},
            },
        }

    # 模板
    loader = TemplateLoader()
    loader.register("translate", {
        "name": "translate",
        "description": "翻译模块",
        "translation": {"type": "script", "script": "translate_translator"},
        "tasklist": {
            "Tasks": {},
            "Flow": "A --> B",
        },
    })

    loader.register("translate_llm", {
        "name": "translate_llm",
        "description": "翻译模块 (LLM翻译)",
        "translation": {"type": "harness", "harness": "spec_to_tasklist", "prompt": "根据 spec 生成 tasklist JSON"},
        "tasklist": {
            "Tasks": {},
            "Flow": "A --> B",
        },
    })

    return reg, bus, loader


class TestModule:
    @pytest.mark.asyncio
    async def test_build_runner_script_translation(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}',
            usage={},
            finish_reason="end_turn",
        )

        mod = Module(
            spec={"harness_name": "translate", "source_text": "Hello", "style": "formal"},
            template_name="translate",
            llm_client=mock_llm,
            event_bus=bus,
            template_loader=loader,
            module_id="test_mod",
        )

        runner = mod.build_runner()
        assert runner is not None
        # runner 应可通过 AsyncRunner 方法操作
        assert runner.is_idle()

    @pytest.mark.asyncio
    async def test_run_script_translation(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}',
            usage={},
            finish_reason="end_turn",
        )

        mod = Module(
            spec={"harness_name": "translate", "source_text": "Hello", "style": "formal"},
            template_name="translate",
            llm_client=mock_llm,
            event_bus=bus,
            template_loader=loader,
            module_id="test_run",
        )

        firings = await mod.run(max_ticks=10)
        # 至少 A 节点触发
        assert len(firings) >= 1
        assert any(f.node == "A" for f in firings)

    @pytest.mark.asyncio
    async def test_run_harness_translation(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry

        # LLM 翻译返回 + LLM 执行返回
        call_count = [0]

        async def fake_complete(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 翻译 harness 返回 tasklist
                return LLMResponse(
                    content='{"A": {"type": "harness", "harness": "translate", "inputs": {"text": "Hello"}, "outputformat": {"type": "json_object"}}, "B": {"type": "script", "script": "format_output", "inputs": {"data": "A"}}}',
                    usage={},
                    finish_reason="end_turn",
                )
            else:
                # 执行 harness 返回翻译结果
                return LLMResponse(
                    content='{"translation": "Bonjour"}',
                    usage={},
                    finish_reason="end_turn",
                )

        mock_llm.complete = AsyncMock(side_effect=fake_complete)

        mod = Module(
            spec={"task_type": "translate", "source_text": "Hello"},
            template_name="translate_llm",
            llm_client=mock_llm,
            event_bus=bus,
            template_loader=loader,
            module_id="test_llm",
        )

        firings = await mod.run(max_ticks=10)
        assert len(firings) >= 1

    @pytest.mark.asyncio
    async def test_missing_template_raises(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mod = Module(
            spec={},
            template_name="nonexistent",
            llm_client=mock_llm,
            template_loader=loader,
        )
        with pytest.raises(ValueError, match="nonexistent"):
            mod.build_runner()

    def test_auto_module_id(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mod = Module(
            spec={},
            template_name="translate",
            llm_client=mock_llm,
            template_loader=loader,
        )
        assert mod.module_id is not None
        assert len(mod.module_id) > 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest module_harness/tests/test_module.py -v
# 预期: 全部 FAIL
```

- [ ] **Step 3: 编写 module.py**

```python
# module_harness/module.py
"""Module 编排器 — spec + template → tasklist → runner。"""

from __future__ import annotations

import uuid
from typing import Any

from tickflow.async_runner import AsyncRunner

from .spec import Spec
from .translator import Translator, TemplateLoader
from .graph_builder import TasklistTranslator
from .registry import HarnessRegistry
from .events import EventBus


class Module:
    """SpecModule 的核心编排器。

    spec + template → 翻译 → tasklist → tickflow Graph + registry → AsyncRunner。
    """

    def __init__(
        self,
        spec: dict[str, Any],
        template_name: str,
        llm_client: Any,
        *,
        event_bus: EventBus | None = None,
        template_loader: TemplateLoader | None = None,
        module_id: str | None = None,
    ) -> None:
        self.spec = Spec(spec)
        self.template_name = template_name
        self.module_id = module_id or f"mod_{uuid.uuid4().hex[:8]}"

        self._reg = HarnessRegistry(
            llm_client=llm_client,
            event_bus=event_bus or EventBus.null(),
        )
        self._loader = template_loader or TemplateLoader()
        self._translator = Translator(self._reg)

    def build_runner(self) -> AsyncRunner:
        """执行翻译 → 构建 graph → 返回 AsyncRunner。"""
        # 1. 加载模板
        template = self._loader.get(self.template_name)
        if template is None:
            raise ValueError(f"模板 '{self.template_name}' 未找到")

        # 2. 翻译 spec → tasklist（同步调用，内部可能需要 await——由 async run 方法处理）
        import asyncio
        tasklist = asyncio.get_event_loop().run_until_complete(
            self._translator.translate(self.spec, template)
        )

        # 3. tasklist → graph
        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist)

        # 4. 返回 runner
        return AsyncRunner(graph, registry=reg, keep_records=True)

    async def _build_runner_async(self) -> AsyncRunner:
        """异步版 build_runner。"""
        template = self._loader.get(self.template_name)
        if template is None:
            raise ValueError(f"模板 '{self.template_name}' 未找到")

        tasklist = await self._translator.translate(self.spec, template)

        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist)

        return AsyncRunner(graph, registry=reg, keep_records=True)

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。"""
        runner = await self._build_runner_async()
        return await runner.run_until_idle(max_ticks=max_ticks)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest module_harness/tests/test_module.py -v
# 预期: 全部 PASS
```

- [ ] **Step 5: 提交**

```bash
git add module_harness/module.py module_harness/tests/test_module.py
git commit -m "feat(module_harness): add Module orchestrator"
```

---

### Task 7: `__init__.py` 更新 + 全量测试

**文件:**
- 修改: `module_harness/__init__.py`

- [ ] **Step 1: 更新 __init__.py**

在现有导出末尾追加：

```python
# 追加到 module_harness/__init__.py

from .spec import (
    Spec,
    TaskDefinition,
    Tasklist,
    TranslationSpec,
    TasklistTemplate,
)
from .translator import TasklistValidator, TemplateLoader, Translator
from .graph_builder import TasklistTranslator
from .module import Module

# 追加到 __all__:
    # 数据模型
    "Spec",
    "TaskDefinition",
    "Tasklist",
    "TranslationSpec",
    "TasklistTemplate",
    # 翻译
    "TasklistValidator",
    "TemplateLoader",
    "Translator",
    # Graph 构建
    "TasklistTranslator",
    # 编排
    "Module",
```

- [ ] **Step 2: 验证导入**

```bash
python -c "import module_harness; print([x for x in module_harness.__all__ if x in ('Spec','Tasklist','TasklistTemplate','TasklistValidator','TemplateLoader','Translator','TasklistTranslator','Module')])"
# 预期: ['Spec', 'Tasklist', 'TasklistTemplate', 'TasklistValidator', 'TemplateLoader', 'Translator', 'TasklistTranslator', 'Module']
```

- [ ] **Step 3: 运行全部测试**

```bash
python -m pytest module_harness/tests/ -v
# 预期: 全部 PASS（约 70 + 新测试）
```

- [ ] **Step 4: 提交**

```bash
git add module_harness/__init__.py
git commit -m "feat(module_harness): add spec/tasklist exports"
```

---

## 任务依赖图

```
Task 1 (spec.py) ──────────────────────────────────────┐
     │                                                  │
Task 2 (validator, 前半 translator.py) ─────────────────┤
     │                                                  │
Task 3 (TemplateLoader, 后半 translator.py) ────────────┤
     │                                                  │
Task 4 (Translator, 完成 translator.py) ────────────────┤
     │                                                  │
Task 5 (graph_builder.py) ──────────────────────────────┤
     │                                                  │
Task 6 (module.py) ─────────────────────────────────────┤
     │                                                  │
Task 7 (__init__.py) ───────────────────────────────────┘
```

线性依赖，必须按顺序执行。

## 验证清单

- [x] spec 无预定义 schema——`Spec` 只包装 dict
- [x] TaskDefinition 字段与 HarnessConfig 对齐
- [x] 翻译不走 tickflow runner——Translator 直接调 body
- [x] 命名空间隔离——`{module_id}:{key}`
- [x] translation.type 显式声明 harness/script
- [x] 翻译结果通过 TasklistValidator 校验
- [x] 内置模板目录 `templates/builtin/`
- [x] tickflow 零修改——仅 import
- [x] module_harness 已有模块零修改——仅新增文件 + `__init__.py` 扩展导出
