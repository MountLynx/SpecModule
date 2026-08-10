# Submodule 一等节点类型实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `submodule` 成为 tasklist 一等节点类型（黑盒嵌入运行子模块，LLM 配置传播，打包内置），并修复两个前置框架缺口（`_check_flow` 丢 registry、SubModule 无 guards 通道）。

**Architecture:** 定义单元 = `SubModule` 类（双重身份），父模块类属性 `modules` 声明引用；`graph_builder._register_submodule` 把 submodule 节点注册为 async body，运行时以嵌入模式（`audit=False, persist=False`）`await child.run()` 取终点输出；`pack()`/`ModuleLoader` 递归导出/加载 `submodules/` 与 `guards/`。Spec: `docs/superpowers/specs/2026-08-10-submodule-node-design.md`。

**Tech Stack:** Python 3.13, asyncio, pytest + unittest.mock（AsyncMock/MagicMock）, tickflow 引擎（零修改）。

**前置阅读：** 设计文档 `docs/superpowers/specs/2026-08-10-submodule-node-design.md`；已验证的 loop 模式参考 `module_harness/tests/test_checkpoint.py:900-1000`（counter 自循环 + guard 读 `view["node"].value`）。

---

## 文件结构

| 文件 | 职责 | 变更 |
|------|------|------|
| `module_harness/spec.py` | TaskDefinition 模型（type 扩到 submodule + submodule/outputs 字段）；`SpecValidationError` 迁移至此（graph_builder 需要引用，避免循环 import） | T2, T6 |
| `module_harness/translator.py` | 缺口 1：`_check_flow` 传 registry；校验器 submodule 分支 + `modules` 参数 | T1, T2 |
| `module_harness/submodule.py` | 缺口 2：`guards` 类属性；`modules` 类属性；`run()` 增加 `harness_overrides`/`persist`；`pack()` 导出 guards/ + submodules/ | T3, T4, T5, T7 |
| `module_harness/module.py` | `Module.__init__` 增加 `modules` 参数 + 存储 `_llm_client`；校验器/translator 接线 | T5 |
| `module_harness/graph_builder.py` | `TasklistTranslator` 接收 `modules`/`llm_client`；`_register_submodule` 注册 async body | T5, T6 |
| `module_harness/loader.py` | `_load_guards` + `_load_submodules`（递归）+ 动态类 `guards`/`modules` + manifest `modules` 校验 | T7 |
| `module_harness/tests/test_validator.py` | 缺口 1 测试 + 校验器 submodule 分支测试 | T1, T2 |
| `module_harness/tests/test_submodule.py` | guards 收集/loop 运行、harness_overrides、persist、modules 类属性、pack 导出 | T3, T4, T5, T7 |
| `module_harness/tests/test_submodule_node.py` | **新建**：TaskDefinition roundtrip + 节点行为测试 | T2, T6 |
| `docs/progress/module-roadmap.md` 等 | 文档更新 | T8 |

测试命令（全部任务通用）：`python -m pytest module_harness/tests/<file> -q`

---

## Task 1: 缺口 1 — `_check_flow` 传 registry

**Files:**
- Modify: `module_harness/translator.py:54-123`
- Test: `module_harness/tests/test_validator.py`

- [ ] **Step 1: 写失败测试**（追加到 `test_validator.py` 末尾，新增 import）

```python
from unittest.mock import MagicMock
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry
from module_harness.spec import Tasklist, TaskDefinition
from module_harness.translator import TasklistValidator


class TestGuardFlow:
    def _reg(self):
        reg = HarnessRegistry(llm_client=object(), event_bus=EventBus())
        reg.script("s")(lambda view: {"n": 1})
        return reg

    def test_guard_edge_with_registered_guard_passes(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="[A] --|until3|--> A",
        )
        reg = self._reg()
        reg.guard("until3")(lambda view: False)
        assert TasklistValidator.validate(tl, reg) == []

    def test_guard_edge_unregistered_guard_fails(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="[A] --|until3|--> A",
        )
        errors = TasklistValidator.validate(tl, self._reg())
        assert any("until3" in e for e in errors)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_validator.py::TestGuardFlow -q`
Expected: `test_guard_edge_with_registered_guard_passes` FAIL（`Flow 解析失败: guard 'until3' not registered`）；另一个 PASS

- [ ] **Step 3: 最小实现**（`module_harness/translator.py`）

```python
    @staticmethod
    def validate(tasklist: Tasklist, registry: HarnessRegistry) -> list[str]:
        """返回问题列表，空列表 = 合法。"""
        errors: list[str] = []

        for key, task in tasklist.tasks.items():
            errors.extend(TasklistValidator._check_task(key, task, registry))

        errors.extend(TasklistValidator._check_flow(tasklist, registry))
        return errors
```

（`validate` 本体不变，只改 `_check_flow` 签名与调用点）

```python
    @staticmethod
    def _check_flow(tasklist: Tasklist, registry: HarnessRegistry) -> list[str]:
        ...
        # 尝试 tickflow parse 检测语法问题（与 graph_builder 使用相同的
        # prepare_flow 预处理；guard 名需在 registry 中可解析）
        try:
            parse_graph(prepare_flow(flow), registry=registry)
        except Exception as e:
            errors.append(f"Flow 解析失败: {e}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_validator.py -q`
Expected: 全部 PASS（含既有 `TestTasklistValidator`）

- [ ] **Step 5: 提交**

```bash
git add module_harness/translator.py module_harness/tests/test_validator.py
git commit -m "fix: TasklistValidator._check_flow 传 registry（guard 边校验不再误拒）"
```

---

## Task 2: TaskDefinition 扩展 + 校验器 submodule 分支

**Files:**
- Modify: `module_harness/spec.py:45-110`、`module_harness/translator.py:54-123`
- Create: `module_harness/tests/test_submodule_node.py`
- Test: `module_harness/tests/test_validator.py`

- [ ] **Step 1: 写失败测试**

`module_harness/tests/test_submodule_node.py`（新建，本任务先放模型 roundtrip 测试）：

```python
"""submodule 节点类型测试：模型 roundtrip + 节点行为。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestTaskDefinition:
    def test_submodule_tasklist_roundtrip(self):
        tl = Tasklist(
            tasks={
                "B": TaskDefinition(
                    type="submodule", submodule="fact_review_loop",
                    inputs={"a": "{spec.x}"},
                    outputs={"s": "sum"},
                    model="deepseek-chat",
                ),
            },
            flow="B",
        )
        tl2 = Tasklist.from_json(tl.to_dict())
        b = tl2.tasks["B"]
        assert b.type == "submodule"
        assert b.submodule == "fact_review_loop"
        assert b.outputs == {"s": "sum"}
        assert b.model == "deepseek-chat"
```

追加到 `module_harness/tests/test_validator.py` 的 `TestTasklistValidator`：

```python
    def test_submodule_type_missing_submodule_field(self):
        tl = Tasklist(
            tasks={"B": TaskDefinition(type="submodule", submodule=None)},
            flow="B",
        )
        errors = TasklistValidator.validate(tl, _make_registry())
        assert any("submodule" in e for e in errors)

    def test_submodule_not_in_modules(self):
        tl = Tasklist(
            tasks={"B": TaskDefinition(type="submodule", submodule="nope")},
            flow="B",
        )
        errors = TasklistValidator.validate(
            tl, _make_registry(), modules={"child": object()})
        assert any("nope" in e for e in errors)

    def test_submodule_declared_in_modules_passes(self):
        tl = Tasklist(
            tasks={"B": TaskDefinition(type="submodule", submodule="child")},
            flow="B",
        )
        errors = TasklistValidator.validate(
            tl, _make_registry(), modules={"child": object()})
        assert errors == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_submodule_node.py::TestTaskDefinition module_harness/tests/test_validator.py::TestTasklistValidator -q`
Expected: roundtrip FAIL（`TypeError: __init__() got an unexpected keyword argument 'submodule'`）；submodule 分支测试 FAIL（`未知 type 'submodule'`）

- [ ] **Step 3: 实现**（`module_harness/spec.py`）

```python
@dataclass
class TaskDefinition:
    """tasklist 中单个 Task 的定义。与 HarnessConfig 字段对齐。"""

    type: Literal["harness", "script", "command", "submodule"]
    harness: str | None = None
    script: str | None = None
    command: str | None = None          # type="command" 时引用的命令名
    submodule: str | None = None        # type="submodule" 时引用名（父模块 modules 解析）
    outputs: dict[str, str] | None = None  # submodule 输出映射 {节点字段: 子输出字段}；缺省 = 全量
    timeout: float | None = None        # command 超时覆盖（秒）
    ...
```

`from_dict` 增加：

```python
            submodule=d.get("submodule"),
            outputs=d.get("outputs"),
```

`module_harness/translator.py`——`validate` 与 `_check_task` 增加 `modules` 参数 + submodule 分支：

```python
    @staticmethod
    def validate(
        tasklist: Tasklist,
        registry: HarnessRegistry,
        modules: dict[str, Any] | None = None,
    ) -> list[str]:
        """返回问题列表，空列表 = 合法。``modules`` 为 submodule 名解析表
        （None = 跳过名字校验，向后兼容）。"""
        errors: list[str] = []

        for key, task in tasklist.tasks.items():
            errors.extend(TasklistValidator._check_task(key, task, registry, modules))

        errors.extend(TasklistValidator._check_flow(tasklist, registry))
        return errors

    @staticmethod
    def _check_task(
        key: str,
        task: TaskDefinition,
        registry: HarnessRegistry,
        modules: dict[str, Any] | None = None,
    ) -> list[str]:
        errors: list[str] = []

        if task.type == "harness":
            ...
        elif task.type == "command":
            ...
        elif task.type == "submodule":
            if not task.submodule:
                errors.append(f"Task '{key}': type='submodule' 但缺少 'submodule' 字段")
            elif modules is not None and task.submodule not in modules:
                errors.append(f"Task '{key}': submodule '{task.submodule}' 未在 modules 中声明")
        else:
            errors.append(f"Task '{key}': 未知 type '{task.type}'")

        return errors
```

（文件顶部 import 需补 `Any`：`from typing import Any`）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_submodule_node.py::TestTaskDefinition module_harness/tests/test_validator.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add module_harness/spec.py module_harness/translator.py module_harness/tests/test_submodule_node.py module_harness/tests/test_validator.py
git commit -m "feat: TaskDefinition 支持 submodule 节点类型（type/submodule/outputs 字段 + 校验器分支）"
```

---

## Task 3: 缺口 2 — `SubModule.guards` 声明/收集/注册

**Files:**
- Modify: `module_harness/submodule.py:51-116`
- Test: `module_harness/tests/test_submodule.py`

- [ ] **Step 1: 写失败测试**（追加到 `test_submodule.py`，`TestSubModule` 类前定义辅助）

```python
def _until3(view):
    return view["counter"].value["n"] < 3


class LoopMod(SubModule):
    """带 guard 自循环的 submodule：n 递增到 3 后 guard 放行退出。"""

    name = "loop_mod"
    spec_schema = SpecSchema(input={}, output={"n": "int"})
    guards = [("until3", _until3)]
    tasklist = Tasklist(
        tasks={"counter": TaskDefinition(type="script", script="counter")},
        flow="[counter] --|until3|--> counter",
    )

    @script("counter")
    def counter(view):
        n = view.state.get("n", 0) + 1
        view.state["n"] = n
        return {"n": n}


class TestGuards:
    def test_guards_copied_between_subclasses(self):
        class G1(SubModule):
            name = "g1"
            guards = [("g", lambda view: True)]

        class G2(G1):
            name = "g2"

        assert G2.guards == G1.guards
        assert G1.guards is not G2.guards
        G2.guards.append(("h", lambda view: False))
        assert len(G1.guards) == 1  # 不污染父类

    @pytest.mark.asyncio
    async def test_loop_runs_until_guard_opens(self, mock_llm):
        """带 guard 的 loop tasklist 正常终止（校验 + 构建 + 运行全链路）。"""
        firings = await LoopMod(llm_client=mock_llm).run({}, max_ticks=20)
        assert len(firings) == 3
        assert firings[-1].output == {"n": 3}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestGuards -q`
Expected: `test_loop_runs_until_guard_opens` FAIL（`Flow 解析失败: guard 'until3' not registered`——guards 未注册进 registry）

- [ ] **Step 3: 实现**（`module_harness/submodule.py`）

类属性增加：

```python
    requires: list[str] = []
    guards: list[tuple[str, Callable]] = []   # [(名字, 函数)]，名字 = 注册名 = 打包文件名
    modules: dict[str, type["SubModule"]] = {}  # submodule 节点引用表 {tasklist 名: 类}
```

（`modules` 在 Task 5 使用，本任务先声明 + 复制）

`__init_subclass__` 复制逻辑：

```python
        # 列表类属性按子类复制，防止子类就地修改污染父类注册
        for attr in ("harnesses", "commands", "requires", "guards"):
            if attr not in cls.__dict__:
                setattr(cls, attr, list(getattr(cls, attr)))
        if "modules" not in cls.__dict__:
            setattr(cls, "modules", dict(getattr(cls, "modules")))
```

`_build_registry` 注册 guards：

```python
        for sname, fn in self._scripts.items():
            reg.script(sname)(fn)
        for gname, gfn in self.guards:
            reg.guard(gname, gfn)
        register_builtin_harnesses(reg)
        return reg
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py -q`
Expected: 全部 PASS（含既有 TestSubModule / TestPack / TestModuleLoader）

- [ ] **Step 5: 提交**

```bash
git add module_harness/submodule.py module_harness/tests/test_submodule.py
git commit -m "feat: SubModule.guards 声明/收集/注册（类式模块支持 guard loop）"
```

---

## Task 4: `SubModule.run` 扩展 — `harness_overrides` + `persist`

**Files:**
- Modify: `module_harness/submodule.py:102-157`
- Test: `module_harness/tests/test_submodule.py`

- [ ] **Step 1: 写失败测试**（追加到 `TestSubModule`）

```python
    @pytest.mark.asyncio
    async def test_harness_overrides_propagate_to_all_harnesses(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run(
            {"source_text": "Hello", "style": "formal"},
            harness_overrides={"model": "model-x", "temperature": 0.7},
            max_ticks=10,
        )
        calls = mock_llm.complete.await_args_list
        assert calls
        assert all(c.kwargs.get("model") == "model-x" for c in calls)
        assert all(c.kwargs.get("temperature") == 0.7 for c in calls)

    @pytest.mark.asyncio
    async def test_harness_overrides_api_params_merge(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run(
            {"source_text": "Hello", "style": "formal"},
            harness_overrides={"api_params": {"max_tokens": 200}},
            max_ticks=10,
        )
        calls = mock_llm.complete.await_args_list
        assert calls
        assert all(c.kwargs.get("api_params", {}).get("max_tokens") == 200 for c in calls)

    @pytest.mark.asyncio
    async def test_run_persist_false_zero_residue(self, tmp_path, monkeypatch, mock_llm):
        monkeypatch.chdir(tmp_path)
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run(
            {"source_text": "Hello", "style": "formal"}, persist=False, max_ticks=10)
        assert not (tmp_path / ".specmodule").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestSubModule -q`
Expected: 三个新测试 FAIL（`TypeError: run() got an unexpected keyword argument 'harness_overrides'` / `'persist'`）

- [ ] **Step 3: 实现**（`module_harness/submodule.py`）

`run()` 签名与 body：

```python
    async def run(
        self,
        spec: dict[str, Any],
        *,
        tasklist: Tasklist | dict[str, Any] | None = None,
        audit: bool = False,
        max_ticks: int = 100,
        harness_overrides: dict[str, Any] | None = None,
        persist: bool | None = None,
    ) -> list[Any]:
        """执行 submodule。

        - tasklist=None：用自身固定 tasklist，不触发一致性审核（发布前已验证）
        - 传入自定义 tasklist：与 Module 一致，校验 + 一致性审核
        - harness_overrides：{model/temperature/think/api_params} 覆盖，
          构建 registry 时应用到全部 harness（submodule 节点 LLM 配置传播）
        - audit=False（默认）：嵌入模式，EventBus.null() + keep_records=False；
          除非 mode="fast"，嵌入模式同样落盘（D11）
        - persist：False = 快速模式（NullBackend 全内存 + 无 status.json，
          零落盘零 I/O）；None = 按 mode 决定（"fast" → False，否则 True）
        - audit=True：keep_records 全开；订阅事件需在构造时传入 event_bus
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
        use_persist = persist if persist is not None else (self.mode != "fast")
        reg = self._build_registry(audit, harness_overrides)
        module = Module(
            spec=spec,
            tasklist=use_tasklist,
            llm_client=self._ensure_client(),
            event_bus=self._event_bus,
            module_id=self._module_id(),
            registry=reg,
            review_harness=review,
            keep_records=audit,
            persist=use_persist,
            status_file=use_persist,
        )
        return await module.run(max_ticks=max_ticks)
```

`_build_registry` 与覆盖辅助：

```python
    def _build_registry(
        self,
        audit: bool,
        harness_overrides: dict[str, Any] | None = None,
    ) -> HarnessRegistry:
        bus = self._event_bus if audit else EventBus.null()
        reg = HarnessRegistry(llm_client=self._ensure_client(), event_bus=bus)
        for hc in self.harnesses:
            if not hc.name:
                raise ValueError(f"harnesses 配置缺少 name: {hc}")
            cfg = (
                self._apply_harness_overrides(hc, harness_overrides)
                if harness_overrides else hc
            )
            reg.harness(cfg.name, cfg)
        for cc in self.commands:
            if not cc.name:
                raise ValueError(f"commands 配置缺少 name: {cc}")
            reg.command(cc.name, cc)
        for sname, fn in self._scripts.items():
            reg.script(sname)(fn)
        for gname, gfn in self.guards:
            reg.guard(gname, gfn)
        register_builtin_harnesses(reg)
        return reg

    @staticmethod
    def _apply_harness_overrides(
        hc: HarnessConfig, overrides: dict[str, Any]
    ) -> HarnessConfig:
        """批量应用 LLM 覆盖（model/temperature/think/api_params）到单个 harness。"""
        api_params = dict(hc.api_params)
        if overrides.get("api_params"):
            api_params.update(overrides["api_params"])
        return HarnessConfig(
            name=hc.name,
            prompt_core=hc.prompt_core,
            prompt_modes=dict(hc.prompt_modes),
            output_format=hc.output_format,
            notdo=list(hc.notdo),
            model=overrides.get("model", hc.model),
            temperature=overrides.get("temperature", hc.temperature),
            think=overrides.get("think", hc.think),
            api_params=api_params,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add module_harness/submodule.py module_harness/tests/test_submodule.py
git commit -m "feat: SubModule.run 支持 harness_overrides（LLM 配置传播）与 persist=False（零落盘）"
```

---

## Task 5: `Module.modules` 参数 + 接线 + `SubModule.modules` 类属性

**Files:**
- Modify: `module_harness/module.py:58-98, 150-197`、`module_harness/submodule.py`、`module_harness/graph_builder.py:45-47`
- Test: `module_harness/tests/test_submodule.py`

- [ ] **Step 1: 写失败测试**（追加到 `test_submodule.py` 的 `TestSubModule` 附近）

```python
class TestModulesAttr:
    def test_modules_copied_between_subclasses(self):
        class M1(SubModule):
            name = "m1"
            modules = {"child": object}

        class M2(M1):
            name = "m2"

        assert M2.modules == {"child": object}
        assert M1.modules is not M2.modules
        M2.modules["other"] = object
        assert "other" not in M1.modules  # 不污染父类

    def test_modules_explicit_override(self):
        class M1(SubModule):
            name = "m1"
            modules = {"child": object}

        class M2(M1):
            name = "m2"
            modules = {"another": object}  # 显式定义覆盖

        assert set(M2.modules) == {"another"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestModulesAttr -q`
Expected: FAIL（`AttributeError: 'SubModule' object has no attribute 'modules'` 或 `M1.modules is M2.modules`）

- [ ] **Step 3: 实现**

`module_harness/submodule.py`（Task 3 已加类属性与复制——确认在；`run()` 传 modules）：

```python
        module = Module(
            spec=spec,
            tasklist=use_tasklist,
            llm_client=self._ensure_client(),
            event_bus=self._event_bus,
            module_id=self._module_id(),
            registry=reg,
            review_harness=review,
            keep_records=audit,
            persist=use_persist,
            status_file=use_persist,
            modules=self.modules,
        )
```

`module_harness/module.py`——`__init__` 签名尾部增加 + 存储：

```python
        keep_records: bool = True,
        persist: bool = True,
        status_file: bool = True,
        modules: dict[str, Any] | None = None,
    ) -> None:
```

（`module.py` 顶部需确认 `from typing import Any` 已存在；没有则加）

```python
        self.module_id = module_id or f"mod_{uuid.uuid4().hex[:8]}"
        self._llm_client = llm_client
        self._modules = dict(modules or {})
```

`_build_runner_async` 两处接线：

```python
            errors = TasklistValidator.validate(tasklist, self._reg, self._modules)
```

```python
        builder = TasklistTranslator(
            self._reg, self.module_id,
            modules=self._modules, llm_client=self._llm_client,
        )
```

`module_harness/graph_builder.py`——`TasklistTranslator.__init__`：

```python
    def __init__(
        self,
        registry: HarnessRegistry,
        module_id: str,
        *,
        modules: dict[str, Any] | None = None,
        llm_client: Any = None,
    ) -> None:
        self.reg = registry
        self.module_id = module_id
        self.modules = dict(modules or {})
        self._llm_client = llm_client
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py module_harness/tests/test_validator.py module_harness/tests/test_checkpoint.py -q`
Expected: 全部 PASS（checkpoint 测试验证 `TasklistTranslator` 既有调用兼容新参数——默认 None）

- [ ] **Step 5: 提交**

```bash
git add module_harness/module.py module_harness/submodule.py module_harness/graph_builder.py module_harness/tests/test_submodule.py
git commit -m "feat: Module/SubModule 支持 modules 声明（submodule 引用解析表，接线校验器与 translator）"
```

---

## Task 6: graph_builder — `_register_submodule` 节点运行

**Files:**
- Modify: `module_harness/spec.py`（`SpecValidationError` 迁移）、`module_harness/submodule.py`（re-export）、`module_harness/graph_builder.py`
- Test: `module_harness/tests/test_submodule_node.py`

- [ ] **Step 1: 迁移 `SpecValidationError` 到 spec.py**（graph_builder 需引用，避免循环 import：graph_builder → submodule → module → graph_builder）

`module_harness/spec.py` 末尾追加：

```python
class SpecValidationError(Exception):
    """spec 不满足 spec_schema 契约。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("spec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
```

`module_harness/submodule.py`：删除本类定义，import 改为：

```python
from .spec import SpecSchema, SpecValidationError, Tasklist
```

（保留 `SpecValidationError` 名字可导入——re-export，`test_submodule.py` 既有 import 不破）

Run: `python -m pytest module_harness/tests/test_submodule.py -q`
Expected: 全部 PASS（确认迁移无破坏）

- [ ] **Step 2: 写失败测试**（追加到 `test_submodule_node.py`）

```python
class EchoChild(SubModule):
    """纯 script 子模块：无 spec 输入，终点输出固定 dict。"""

    name = "echo_child"
    spec_schema = SpecSchema(input={}, output={"msg": "str"})
    tasklist = Tasklist(
        tasks={"S": TaskDefinition(type="script", script="echo")},
        flow="[S]",
    )

    @script("echo")
    def echo(view):
        return {"msg": "from_child"}


class Parent(SubModule):
    """引用 EchoChild 的父模块：B = submodule 节点，C 读取其输出。"""

    name = "parent"
    modules = {"echo_child": EchoChild}
    tasklist = Tasklist(
        tasks={
            "B": TaskDefinition(type="submodule", submodule="echo_child"),
            "C": TaskDefinition(type="script", script="read", inputs={"data": "B"}),
        },
        flow="[B] --> C",
    )

    @script("read")
    def read(view):
        return {"got": view["B"].value}


class TestSubmoduleNode:
    @pytest.mark.asyncio
    async def test_basic_run(self, mock_llm):
        firings = await Parent(llm_client=mock_llm).run({"x": 1}, max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"msg": "from_child"}}

    @pytest.mark.asyncio
    async def test_undeclared_submodule_rejected(self, mock_llm):
        class Bad(SubModule):
            name = "bad"
            modules = {}
            tasklist = Tasklist(
                tasks={"B": TaskDefinition(type="submodule", submodule="nope")},
                flow="B",
            )

        with pytest.raises(ValueError, match="nope"):
            await Bad(llm_client=mock_llm).run({}, max_ticks=20)

    @pytest.mark.asyncio
    async def test_outputs_field_not_in_schema_rejected(self, mock_llm):
        class Bad(SubModule):
            name = "bad"
            modules = {"echo_child": EchoChild}
            tasklist = Tasklist(
                tasks={
                    "B": TaskDefinition(
                        type="submodule", submodule="echo_child",
                        outputs={"x": "no_such"},
                    ),
                },
                flow="B",
            )

        with pytest.raises(ValueError, match="no_such"):
            await Bad(llm_client=mock_llm).run({}, max_ticks=20)

    @pytest.mark.asyncio
    async def test_inputs_spec_and_node_refs(self, mock_llm):
        class SumChild(SubModule):
            name = "sum_child"
            spec_schema = SpecSchema(input={"a": "int", "b": "int"}, output={"sum": "int"})
            harnesses = [
                HarnessConfig(
                    name="sum", prompt_core="求和：{a} + {b}",
                    output_format=OutputFormat(type="json_object"),
                ),
            ]
            tasklist = Tasklist(
                tasks={
                    "S": TaskDefinition(
                        type="harness", harness="sum",
                        inputs={"a": "{spec.a}", "b": "{spec.b}"},
                        outputformat={"type": "json_object"},
                    ),
                },
                flow="[S]",
            )

        class P2(SubModule):
            name = "p2"
            modules = {"sum_child": SumChild}
            tasklist = Tasklist(
                tasks={
                    "A": TaskDefinition(type="script", script="gen"),
                    "B": TaskDefinition(
                        type="submodule", submodule="sum_child",
                        inputs={"a": "{spec.x}", "b": "A"},
                    ),
                },
                flow="[A] --> B",
            )

            @script("gen")
            def gen(view):
                return 7

        mock_llm.complete.return_value = LLMResponse(
            content='{"sum": 10}', usage={}, finish_reason="end_turn")
        firings = await P2(llm_client=mock_llm).run({"x": 3}, max_ticks=20)
        assert mock_llm.complete.await_args is not None
        prompt = mock_llm.complete.await_args.kwargs["prompt"]
        assert "3" in prompt and "7" in prompt
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"sum": 10}

    @pytest.mark.asyncio
    async def test_outputs_mapping(self, mock_llm):
        class P3(SubModule):
            name = "p3"
            modules = {"echo_child": EchoChild}
            tasklist = Tasklist(
                tasks={
                    "B": TaskDefinition(
                        type="submodule", submodule="echo_child",
                        outputs={"renamed": "msg"},
                    ),
                    "C": TaskDefinition(type="script", script="read", inputs={"data": "B"}),
                },
                flow="[B] --> C",
            )

            @script("read")
            def read(view):
                return {"got": view["B"].value}

        firings = await P3(llm_client=mock_llm).run({}, max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"renamed": "from_child"}}

    @pytest.mark.asyncio
    async def test_child_spec_validation_failure_is_infrastructure(self, mock_llm):
        class SumChild(SubModule):
            name = "sum_child2"
            spec_schema = SpecSchema(input={"a": "int"}, output={"sum": "int"})
            tasklist = Tasklist(
                tasks={"S": TaskDefinition(type="script", script="echo")},
                flow="[S]",
            )

            @script("echo")
            def echo(view):
                return {"sum": 0}

        class P4(SubModule):
            name = "p4"
            modules = {"sum_child2": SumChild}
            tasklist = Tasklist(
                tasks={"B": TaskDefinition(type="submodule", submodule="sum_child2")},
                flow="B",
            )

        from tickflow import Failure
        firings = await P4(llm_client=mock_llm).run({}, max_ticks=20)
        b_out = next(f.output for f in firings if f.node == "B")
        assert isinstance(b_out, Failure)
        assert b_out.type == "infrastructure"

    @pytest.mark.asyncio
    async def test_submodule_node_in_loop(self, mock_llm):
        def until3(view):
            return view["A"].value["n"] < 3

        class LoopChild(SubModule):
            name = "loop_child"
            spec_schema = SpecSchema(input={"seed": "any"}, output={"msg": "str"})
            tasklist = Tasklist(
                tasks={"S": TaskDefinition(type="script", script="echo")},
                flow="[S]",
            )

            @script("echo")
            def echo(view):
                return {"msg": "from_child"}

        class LoopParent(SubModule):
            name = "loop_parent"
            modules = {"loop_child": LoopChild}
            guards = [("until3", until3)]
            tasklist = Tasklist(
                tasks={
                    "A": TaskDefinition(type="script", script="counter"),
                    "B": TaskDefinition(
                        type="submodule", submodule="loop_child",
                        inputs={"seed": "A"},
                    ),
                },
                flow="[A] --|until3|--> A\nA --> B",
            )

            @script("counter")
            def counter(view):
                n = view.state.get("n", 0) + 1
                view.state["n"] = n
                return {"n": n}

        firings = await LoopParent(llm_client=mock_llm).run({}, max_ticks=30)
        b_firings = [f for f in firings if f.node == "B"]
        assert len(b_firings) == 3  # A 循环 3 轮，B（submodule 节点）每轮触发一次

    @pytest.mark.asyncio
    async def test_procedural_module_api_equivalent(self, mock_llm):
        """过程式 Module(spec, tasklist, modules=...) 与类式同效。"""
        from module_harness.module import Module

        mod = Module(
            spec={"x": 1},
            tasklist=Parent.tasklist,
            llm_client=mock_llm,
            modules={"echo_child": EchoChild},
            review_harness=None,
        )
        firings = await mod.run(max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"msg": "from_child"}}
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_submodule_node.py::TestSubmoduleNode -q`
Expected: 全部 FAIL（`Task 'B': unknown type 'submodule'` 或 `_register_body` ValueError）

- [ ] **Step 4: 实现**（`module_harness/graph_builder.py`）

import 更新：

```python
from tickflow import Failure, Graph, parse as parse_graph
from tickflow.ir import InputPolicy

from .config import HarnessConfig
from .outputfmt import OutputFormat
from .registry import HarnessRegistry
from .spec import SpecValidationError, TaskDefinition, Tasklist
from .translator import prepare_flow
```

`_register_body` 增加分支：

```python
        elif task.type == "submodule":
            self._register_submodule(key, task, spec_dict, tasklist_dict)
```

新增方法（放在 `_register_command` 之后）：

```python
    def _register_submodule(self, key: str, task: TaskDefinition,
                            spec_dict: dict[str, Any],
                            tasklist_dict: dict[str, Any]) -> None:
        """注册 submodule 节点 body：黑盒嵌入运行子模块（audit=False +
        persist=False，不进审计/落盘），返回子流程终点输出。"""
        assert task.submodule is not None  # validated by spec
        child_ref = self.modules.get(task.submodule)
        if child_ref is None:
            raise ValueError(
                f"Task '{key}': submodule '{task.submodule}' not found.  "
                f"Make sure it was declared in modules."
            )
        if task.outputs:
            for out_field, child_field in task.outputs.items():
                if child_field not in child_ref.spec_schema.output:
                    raise ValueError(
                        f"Task '{key}': outputs 字段 '{child_field}' 不在 "
                        f"submodule '{task.submodule}' 的 spec_schema.output 中"
                    )

        # 子模块实例：类 → 懒实例化（注入父 client）；实例（loader 加载）→ 复用
        if isinstance(child_ref, type):
            child = child_ref(llm_client=self._llm_client)
        else:
            child = child_ref

        # 节点级 LLM 覆盖 → 传播到子模块内部全部 harness
        overrides: dict[str, Any] = {}
        for k in ("model", "temperature", "think", "api_params"):
            v = getattr(task, k)
            if v is not None:
                overrides[k] = v

        # 常量引用（{spec.xxx} / {spec}/{tasklist}/{node}）构建期解析
        const_inputs: dict[str, Any] = {}
        if task.inputs:
            for field_name, producer in task.inputs.items():
                if isinstance(producer, str) and producer in _CONSTANT_TOKENS:
                    const_inputs[field_name] = self._resolve_constant(
                        producer, key, spec_dict, tasklist_dict
                    )
                elif isinstance(producer, str) and producer.startswith("{spec."):
                    const_inputs[field_name] = self._resolve_spec_ref(
                        producer, spec_dict
                    )

        async def body(view: Any) -> Any:
            spec_input = dict(const_inputs)
            if task.inputs:
                for field_name, producer in task.inputs.items():
                    if _is_constant_ref(producer):
                        continue
                    spec_input[field_name] = view[producer].value
            try:
                firings = await child.run(
                    spec_input, audit=False, persist=False,
                    harness_overrides=overrides,
                )
            except SpecValidationError as e:
                return Failure(
                    f"submodule '{task.submodule}' spec 校验失败: {e}",
                    type="infrastructure",
                )
            out = firings[-1].output if firings else {}
            if task.outputs:
                out = {k: out.get(v) for k, v in task.outputs.items()}
            return out

        self.reg.body(self._isolated(key), body)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_submodule_node.py -q`
Expected: 全部 PASS

- [ ] **Step 6: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add module_harness/spec.py module_harness/submodule.py module_harness/graph_builder.py module_harness/tests/test_submodule_node.py
git commit -m "feat: submodule 一等节点类型（_register_submodule 黑盒嵌入运行 + 输出映射 + LLM 覆盖传播）"
```

---

## Task 7: pack/loader — guards + submodules 递归

**Files:**
- Modify: `module_harness/submodule.py:159-201`、`module_harness/loader.py`
- Test: `module_harness/tests/test_submodule.py`（pack）、`module_harness/tests/test_submodule_node.py`（roundtrip）

- [ ] **Step 1: 写失败测试**

`test_submodule.py` 追加（复用 Task 3 的 `LoopMod`——guards 导出需要模块级函数 `_until3`，已定义）：

```python
class TestPackGuards:
    def test_pack_exports_guards(self, tmp_path):
        out = LoopMod().pack(tmp_path / "dist")
        guard_file = out / "guards" / "until3.py"
        assert guard_file.is_file()
        ns: dict = {}
        exec(compile(guard_file.read_text(encoding="utf-8"), "until3.py", "exec"), ns)
        assert callable(ns["until3"])
```

`test_submodule_node.py` 追加（`Parent` / `EchoChild` 为 Task 6 定义，同一文件复用）：

```python
class TestPackLoadSubmodules:
    def test_pack_exports_submodules(self, tmp_path):
        out = Parent().pack(tmp_path / "dist")
        manifest = json.loads((out / "module.json").read_text(encoding="utf-8"))
        assert manifest["modules"] == ["echo_child"]
        assert (out / "submodules" / "echo_child" / "module.json").is_file()

    @pytest.mark.asyncio
    async def test_load_roundtrip_with_submodules(self, tmp_path, mock_llm):
        from module_harness.loader import ModuleLoader

        out = Parent().pack(tmp_path / "dist")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        assert set(module.modules) == {"echo_child"}
        firings = await module.run({"x": 1}, max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"msg": "from_child"}}

    def test_manifest_modules_missing_dir_rejected(self, tmp_path, mock_llm):
        import json as _json
        from module_harness.loader import ModuleLoader, ModuleManifestError

        out = Parent().pack(tmp_path / "dist")
        manifest_path = out / "module.json"
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["modules"] = ["ghost"]
        manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ModuleManifestError, match="ghost"):
            ModuleLoader(llm_client=mock_llm).load(out)
```

（`test_submodule_node.py` 顶部需补 `import json`；pack 断言只用本文件已定义的 `Parent`/`EchoChild`，不跨文件引用）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_submodule.py::TestPackGuardsSubmodules module_harness/tests/test_submodule_node.py::TestPackLoadSubmodules -q`
Expected: FAIL（`guards/until3.py` 不存在 / manifest 无 `modules` 字段 / loader 无 `modules` 属性）

- [ ] **Step 3: 实现**

`module_harness/submodule.py` —— `pack()` 增加 guards + submodules 导出，manifest 记录：

```python
        manifest = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "submodule": True,
            "spec_schema": asdict(self.spec_schema),
            "requires": list(self.requires),
            "modules": list(self.modules),
            "tasklist": self.tasklist.to_dict(),
        }
```

（`pack()` 方法尾部，scripts 循环之后追加）

```python
        for gname, gfn in self.guards:
            src = textwrap.dedent(inspect.getsource(gfn))
            header = "from __future__ import annotations\n\n"
            (p / "guards" / f"{gname}.py").write_text(header + src, encoding="utf-8")
        for mname, mcls in self.modules.items():
            mcls().pack(p / "submodules" / mname)
```

（`guards/` 与 `submodules/` 目录在 `pack()` 开头的 mkdir 块追加：`(p / "guards").mkdir(exist_ok=True)`、`(p / "submodules").mkdir(exist_ok=True)`）

`module_harness/loader.py` —— `load()` 中 provides 加载后追加：

```python
        guards = self._load_guards(p)
        submodules = self._load_submodules(p)

        modules_raw = manifest.get("modules", []) or []
        if not isinstance(modules_raw, list) or not all(
                isinstance(m, str) for m in modules_raw):
            raise ModuleManifestError("modules 必须是字符串列表")
        missing_mods = [m for m in modules_raw if m not in submodules]
        if missing_mods:
            raise ModuleManifestError(
                "modules 缺少子模块目录: " + ", ".join(missing_mods))
```

动态类追加两个属性：

```python
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
            "guards": list(guards.items()),
            "modules": submodules,
        })
```

新增两个私有方法：

```python
    def _load_guards(self, p: Path) -> dict[str, Any]:
        """加载 guards/*.py 为可调用函数（exec 执行——与 scripts 同机制）。"""
        result: dict[str, Any] = {}
        for f in sorted((p / "guards").glob("*.py")):
            ns: dict[str, Any] = {}
            try:
                exec(compile(f.read_text(encoding="utf-8"), str(f), "exec"), ns)
            except Exception as e:  # 函数自身报错视为清单错误
                raise ModuleManifestError(f"{f} 加载失败: {e}") from e
            fn = ns.get(f.stem)
            if not callable(fn):
                raise ModuleManifestError(f"{f} 未定义函数 {f.stem}")
            result[f.stem] = fn
        return result

    def _load_submodules(self, p: Path) -> dict[str, SubModule]:
        """递归加载 submodules/*/（每个是完整子包）→ {目录名: 实例}。

        目录名为引用键（pack 时以父模块 modules 的键命名），与子模块
        自身 name 无关。guard 名不进入 provides/requires（边引用，不参与
        重复名检测）；子模块实例是父的 modules 值，加载时同样解析。"""
        result: dict[str, SubModule] = {}
        base = p / "submodules"
        if not base.is_dir():
            return result
        for d in sorted(base.iterdir()):
            if not (d / "module.json").is_file():
                raise ModuleManifestError(f"{d} 缺少 module.json（submodule 目录无效）")
            result[d.name] = self.load(d)
        return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_submodule.py module_harness/tests/test_submodule_node.py -q`
Expected: 全部 PASS

- [ ] **Step 5: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add module_harness/submodule.py module_harness/loader.py module_harness/tests/test_submodule.py module_harness/tests/test_submodule_node.py
git commit -m "feat: pack/loader 支持 guards 导出加载 + submodules 递归打包（模块内置分发，无运行时依赖）"
```

---

## Task 8: 文档更新

**Files:**
- Modify: `docs/progress/module-roadmap.md`、`docs/superpowers/specs/2026-08-05-submodule-design.md`、`docs/superpowers/specs/2026-08-10-academic-writer-design.md`

- [ ] **Step 1: roadmap「模块组合讨论与决策」修正**（`module-roadmap.md:168-186` 整段替换为：）

```markdown
## 模块组合讨论与决策（2026-08-10，已修正）

**背景**：example 模块开发（灵感式写作 → 学术英语，含两阶段"原始↔当前稿"事实审阅 loop）
引出"模块组合"讨论。**原判定（本段早先版本）基于错误前提**：把声明式 submodule 节点
曲解为"图级组合"（"仅省 flow 骨架几行边"），转而采纳"嵌套执行"。用户期望的是组合/封装
的**声明式能力**，不是省骨架。2026-08-10 修正，见 `docs/superpowers/specs/2026-08-10-submodule-node-design.md`。

**三种复用模型（修正后）**：

| 模型 | 机制 | 复用对象 | 审计 | 结论 |
|------|------|---------|------|------|
| **submodule 一等节点** | tasklist 直接写 `{type: "submodule", submodule: "名", inputs, outputs, [LLM 覆盖]}`；父模块 `modules` 类属性声明；打包内置 `submodules/` | 完整模块（黑盒处理单元） | 内部过程无审查意义 → 嵌入模式（不进审计/快照/回滚），只暴露终点输出 | ✅ **采纳**（本次） |
| **嵌套执行** | async script node 内 `await module.run()`（收 spec → 返回结果） | 整个黑盒模块 | 子 run 独立落盘可审计；父子 run 分离 | ✅ 保留为另一场景：任务中间过程有可复用、**需要审计**的完整 module 时，module 之间平级组合（不存在"sub"） |
| **图级组合**（子流程嵌入） | 子流程展开进主图（前缀隔离 / spec 透传 / 跨子图死锁分析） | 图结构（边+guard 语义） | 好（节点进主图） | ⏸️ 不做：用户定位 submodule 为黑盒处理单元，非图结构复用；骨架复用模板机制兜底 |

**submodule 节点语义要点**：
- 定义单元 = `SubModule` 类（双重身份：可独立运行/打包，也可被引用为节点）；与 harness/script
  同级（tasklist 节点实现类型），不冲突
- 节点级 LLM 设置（model/temperature/think/api_params）传播到子模块内部所有 harness
- 输出 = 子流程终点输出全量，可选 `outputs` 字段挑选/重命名；子 run 嵌入模式零落盘
- 依赖声明在父模块类属性 `modules`（无全局注册表）；pack 递归内置子模块 → 加载无运行时依赖
```

- [ ] **Step 2: submodule 设计文档定位更新**（`2026-08-05-submodule-design.md`，在「背景」后追加一段）

```markdown
> **2026-08-10 修正定位**：本设计的 SubModule 同时承担两种身份——① 独立可运行/可打包的
> module；② 可被其他模块引用为 tasklist 节点的**处理单元**（submodule 一等节点类型，
> 与 harness/script 同级）。父模块类属性 `modules: dict[str, type[SubModule]]` 声明引用；
> pack 递归内置 `submodules/<name>/`，加载无运行时依赖。节点级 LLM 设置传播到子模块内部
> 所有 harness；子模块以嵌入模式运行（不进审计/快照/回滚），只暴露终点输出。原「不包含」
> 清单中"submodule 嵌入子 module"一项已实现（见 2026-08-10-submodule-node-design.md）。
```

- [ ] **Step 3: academic-writer 设计更新**（`2026-08-10-academic-writer-design.md`）

- 「概述」中模块二描述：Loop1/Loop2 由"async script 嵌套调用 loop 模块"改为 **submodule 节点**（`modules = {"fact_review_loop": FactReviewLoop}` + `{type: "submodule", submodule: "fact_review_loop", inputs: {...}}`），LLM 配置可在节点级传播
- 「框架现实约束」第三条：删除"无图级模块组合……通过类继承复用"表述，改为"submodule 节点类型（2026-08-10-submodule-node-design.md）支持黑盒引用；两阶段 loop 复用同一 fact_review_loop 处理单元"
- 两个框架缺口（_check_flow registry、SubModule guards）标注**已修复**（本计划 Task 1/3）
- 「范围」中"不包含：图级模块组合能力"保留

- [ ] **Step 4: 最终全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add docs/progress/module-roadmap.md docs/superpowers/specs/2026-08-05-submodule-design.md docs/superpowers/specs/2026-08-10-academic-writer-design.md
git commit -m "docs: 修正 roadmap 模块组合决策（submodule 一等节点）；更新 submodule/academic-writer 设计定位"
```

---

## 验收清单（全部完成后）

- [ ] `python -m pytest module_harness/tests/ -q` 全绿
- [ ] tasklist 可写 `{type: "submodule", submodule: "...", inputs, outputs, model, temperature}` 并黑盒运行
- [ ] 子模块嵌入模式零落盘（无 `.specmodule/runs/<child>/`）
- [ ] 节点级 LLM 设置传播到子模块全部 harness（mock 断言 complete kwargs）
- [ ] 带 guard 的 tasklist（loop）校验/构建/运行全链路通过
- [ ] pack → load round-trip：`submodules/`、`guards/` 目录加载后引用可解析、运行一致
- [ ] 过程式 `Module(spec, tasklist, modules=...)` 与类式同效
