# 运行状态查询 + 对齐检查 harness 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 roadmap #7 运行状态查询（跨进程 status.json + SQLite 快照查询）与 #2 对齐检查（内置 align_check harness + 通用输入 token）。

**Architecture:** 两部分独立。运行状态查询：Module 生命周期阶段机原子写 `.specmodule/runs/<module_id>/status.json`（phase/error/updated_at），新模块 `status.py` 提供 `query_run_status()` 跨进程查询（status.json 阶段 + SqliteBackend 最新快照 tick 级信息）。对齐检查：graph_builder 新增 `{spec}`/`{tasklist}`/`{node}` 三个注册时解析的常量 token（进 spec_inputs），新模块 `align.py` 定义内置 `align_check` harness config 并注册进 builtins。全程 tickflow 零修改。

**Tech Stack:** Python 3.13, pytest + pytest-asyncio + unittest.mock, tickflow (只读), sqlite3 (WAL)

## Global Constraints

- tickflow 零修改 — 查询侧只用 `SqliteBackend` 公开 API，不手写 SQL
- 不做新数据容器 — 查询数据唯一真相源是 status.json + RunState 快照
- 原子写 status.json（tmp + os.replace），写失败仅 log 不阻断运行
- `persist=False` 快速模式也写 status.json（阶段级，不随 tick 增长）
- prompt 占位符缺值保留字面量（渲染器现有行为，不隐藏问题）
- 测试用 mock LLM（`AsyncMock`），跑 runner 用 `NullBackend()`（零 IO）

## 文件清单

| 操作 | 路径 | 职责 |
|------|------|------|
| 修改 | `module_harness/graph_builder.py` | 常量 token 解析（{spec}/{tasklist}/{node}） |
| 创建 | `module_harness/align.py` | ALIGN_CHECK_CONFIG + register_align_check_harness |
| 修改 | `module_harness/builtins.py` | 注册 align_check |
| 创建 | `module_harness/status.py` | ModuleStatus + query_run_status |
| 修改 | `module_harness/module.py` | 阶段机 + status.json 原子写 |
| 修改 | `module_harness/__init__.py` | 导出新 API |
| 修改 | `module_harness/tests/test_graph_builder.py` | token 解析测试 |
| 创建 | `module_harness/tests/test_align.py` | align_check 测试 |
| 创建 | `module_harness/tests/test_run_status.py` | 查询 API + 阶段机测试 |
| 修改 | `docs/progress/module-roadmap.md` | 标记 #7、#2 完成 |

**测试基线**（改动前）：`python -m pytest module_harness/tests/ -q` → 209 passed, 1 failed（smoke 真实 LLM 测试，环境相关，非本次范围）, 1 xfailed。所有任务完成后应保持 209+新增 passed，且不新增 failed。

---

### Task 1: graph_builder 常量 token（{spec}/{tasklist}/{node}）

**Files:**
- Modify: `module_harness/graph_builder.py`
- Test: `module_harness/tests/test_graph_builder.py`

**接口:**
- 新增模块级常量 `_CONSTANT_TOKENS = frozenset({"{spec}", "{tasklist}", "{node}"})`
- 新增辅助函数 `_is_constant_ref(producer: Any) -> bool` — 判断是否为常量引用（token 或 `{spec.xxx}`）
- 新增静态方法 `TasklistTranslator._resolve_constant(token, node_key, spec_dict, tasklist_dict) -> Any`
- `build()` 构造 `tasklist_dict`（Tasks+Flow JSON 用）传给 `_register_body`；`_register_body`/`_register_harness` 签名加 `tasklist_dict` 参数
- 三处跳过逻辑统一改用 `_is_constant_ref`：spec_inputs 构建、input_aliases 构建、build() 第 5 步 wiring

- [ ] **Step 1: 编写失败测试**（追加到 `test_graph_builder.py`）

在文件头部 import 区补充：

```python
from unittest.mock import AsyncMock

from llm.client import LLMResponse
from module_harness.spec import Spec
from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend
```

在 `reg` fixture 之后新增 fixture 与测试类：

```python
@pytest.fixture
def mock_llm_async(mock_llm):
    mock_llm.complete = AsyncMock(return_value=LLMResponse(content='{"ok": true}'))
    return mock_llm


class TestConstantTokens:
    """{spec}/{tasklist}/{node} 常量 token：注册时解析，不注册为图输入。"""

    @pytest.mark.asyncio
    async def test_tokens_resolve_to_spec_inputs(self, mock_llm_async, reg):
        """token 不注册为图输入（不进 InputPolicy），非常量 input 正常 wiring。"""
        reg.harness("align_probe", HarnessConfig(
            prompt_core="spec={spec}\ntasklist={tasklist}\npos={position}\ndata={data}"
        ))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_probe",
                    inputs={
                        "spec": "{spec}",
                        "tasklist": "{tasklist}",
                        "position": "{node}",
                        "data": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好", "target": "world"}))

        assert "spec" not in graph.nodes["C"].inputs
        assert "tasklist" not in graph.nodes["C"].inputs
        assert "position" not in graph.nodes["C"].inputs
        assert "data" in graph.nodes["C"].inputs

    @pytest.mark.asyncio
    async def test_tokens_render_into_prompt(self, mock_llm_async, reg):
        """端到端：prompt 渲染包含 spec JSON / tasklist JSON / 节点 key。"""
        reg.harness("align_probe", HarnessConfig(
            prompt_core="spec={spec}\ntasklist={tasklist}\npos={position}\ndata={data}"
        ))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_probe",
                    inputs={
                        "spec": "{spec}",
                        "tasklist": "{tasklist}",
                        "position": "{node}",
                        "data": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好", "target": "world"}))
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=10)

        assert mock_llm_async.complete.await_count == 2
        prompt = mock_llm_async.complete.call_args_list[1].kwargs["prompt"]
        assert '"source_text": "你好"' in prompt    # {spec} → spec JSON
        assert '"Tasks"' in prompt                   # {tasklist} → tasklist JSON
        assert "pos=C" in prompt                     # {position} → 节点 key

    @pytest.mark.asyncio
    async def test_spec_token_without_spec_renders_empty(self, mock_llm_async, reg):
        """build 未传 spec 时 {spec} → 空 dict JSON（显式可见）。"""
        reg.harness("probe", HarnessConfig(prompt_core="spec={spec}"))
        tl = Tasklist(
            tasks={"A": TaskDefinition(
                type="harness", harness="probe", inputs={"spec": "{spec}"},
            )},
            flow="[A]",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl)  # 不传 spec
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=5)
        prompt = mock_llm_async.complete.call_args.kwargs["prompt"]
        assert "spec={}" in prompt
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_graph_builder.py::TestConstantTokens -q`
Expected: 3 failed — `TypeError: _register_body() takes 4 positional arguments but 5 were given`（或 `NameError: _CONSTANT_TOKENS`）

- [ ] **Step 3: 实现**（修改 `module_harness/graph_builder.py`）

文件头部 import 区补充：

```python
import dataclasses
import json
```

模块级常量（`TasklistTranslator` 类定义之前）：

```python
_CONSTANT_TOKENS = frozenset({"{spec}", "{tasklist}", "{node}"})


def _is_constant_ref(producer: Any) -> bool:
    """是否为注册时解析的常量引用（{spec}/{tasklist}/{node} 或 {spec.xxx}）。"""
    return (
        isinstance(producer, str)
        and (producer in _CONSTANT_TOKENS or producer.startswith("{spec."))
    )
```

`build()` 方法改造（Step 1 的 wiring 部分）：

```python
    def build(self, tasklist: Tasklist, spec: Any | None = None) -> tuple[Graph, HarnessRegistry]:
        """Iterate tasks, register bodies, parse flow, attach body names.

        ``spec``：可选，用于解析 task 中 ``{spec.xxx}`` 字段引用。
        Returns (graph, registry) where *registry* is ``self.reg`` (the same
        object that was passed to the constructor, now populated with the
        isolated body entries).
        """
        spec_dict = spec.to_dict() if spec is not None else {}
        tasklist_dict = {
            "Tasks": {
                key: dataclasses.asdict(task) for key, task in tasklist.tasks.items()
            },
            "Flow": tasklist.flow,
        }

        # 1.  Register every task's body under an isolated name.
        for key, task in tasklist.tasks.items():
            self._register_body(key, task, spec_dict, tasklist_dict)
```

（`prepare_flow` / `parse_graph` / body 赋值步骤不变）第 5 步 wiring 循环改为：

```python
        for key, task in tasklist.tasks.items():
            if task.inputs:
                for field_name, producer in task.inputs.items():
                    if _is_constant_ref(producer):
                        continue
                    graph.nodes[key].inputs[field_name] = InputPolicy.latest()
                    if producer != field_name:
                        graph.nodes[key].inputs[producer] = InputPolicy.latest()
```

`_register_body` 签名与 `_register_harness` 调用（删除原注释里的 spec-only 描述，保留中文注释）：

```python
    def _register_body(self, key: str, task: TaskDefinition,
                       spec_dict: dict[str, Any],
                       tasklist_dict: dict[str, Any]) -> None:
        """Register one task's body in *self.reg* under an isolated name.

        Delegates to the appropriate helper based on ``task.type``.
        """
        if task.type == "harness":
            self._register_harness(key, task, spec_dict, tasklist_dict)
        elif task.type == "script":
            self._register_script(key, task)
        elif task.type == "command":
            self._register_command(key, task)
        else:
            raise ValueError(f"Task '{key}': unknown type {task!r}")
```

新增静态方法（放在 `_resolve_spec_ref` 之后）：

```python
    @staticmethod
    def _resolve_constant(token: str, node_key: str,
                          spec_dict: dict[str, Any],
                          tasklist_dict: dict[str, Any]) -> Any:
        """解析常量 token：{spec} → spec JSON，{tasklist} → tasklist JSON，
        {node} → 当前节点 key。未知 token 抛 ValueError。"""
        if token == "{spec}":
            return json.dumps(spec_dict, ensure_ascii=False)
        if token == "{tasklist}":
            return json.dumps(tasklist_dict, ensure_ascii=False)
        if token == "{node}":
            return node_key
        raise ValueError(f"未知常量 token: {token}")
```

`_register_harness` 签名与 spec_inputs / input_aliases 构建改造：

```python
    def _register_harness(self, key: str, task: TaskDefinition,
                          spec_dict: dict[str, Any],
                          tasklist_dict: dict[str, Any]) -> None:
        """Copy an existing harness config, apply task-level overrides, and
        register under the isolated name."""
        assert task.harness is not None  # validated by spec
        existing = self.reg.harness_config(task.harness)
        if existing is None:
            raise ValueError(
                f"Task '{key}': harness '{task.harness}' not found.  "
                f"Make sure it was registered via reg.harness()."
            )
```

（cfg 构建部分不变）spec_inputs 构建改为：

```python
        spec_inputs: dict[str, Any] = {}
        if task.inputs:
            for field_name, producer in task.inputs.items():
                if isinstance(producer, str) and producer in _CONSTANT_TOKENS:
                    spec_inputs[field_name] = self._resolve_constant(
                        producer, key, spec_dict, tasklist_dict
                    )
                elif isinstance(producer, str) and producer.startswith("{spec."):
                    resolved = self._resolve_spec_ref(producer, spec_dict)
                    spec_inputs[field_name] = resolved
```

input_aliases 构建改为：

```python
        # 跨节点输入别名：非常量引用的 inputs 把 field 名映射到 producer 节点，
        # harness body 运行时据此把 producer 输出渲染进 prompt 的 {field} 占位符。
        input_aliases: dict[str, str] = {}
        if task.inputs:
            for field_name, producer in task.inputs.items():
                if _is_constant_ref(producer):
                    continue
                input_aliases[field_name] = producer
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_graph_builder.py -q`
Expected: 3 new tests pass, existing graph_builder tests still pass（无回归）

- [ ] **Step 5: Commit**

```bash
git add module_harness/graph_builder.py module_harness/tests/test_graph_builder.py
git commit -m "feat(graph_builder): constant tokens {spec}/{tasklist}/{node} resolve to spec_inputs"
```

---

### Task 2: 内置 align_check harness（align.py + builtins.py + 导出）

**Files:**
- Create: `module_harness/align.py`
- Modify: `module_harness/builtins.py`
- Modify: `module_harness/__init__.py`
- Test: `module_harness/tests/test_align.py`

**接口:**
- 产生: `ALIGN_CHECK_CONFIG: HarnessConfig`（json_object 输出，prompt_core 含 {spec}/{tasklist}/{node} 占位符）
- 产生: `register_align_check_harness(reg: HarnessRegistry, name: str = "align_check") -> None`
- `BUILTIN_HARNESS_NAMES` 增加 `"align_check"`；`register_builtin_harnesses()` 调用注册

- [ ] **Step 1: 编写失败测试**（新文件 `module_harness/tests/test_align.py`）

```python
# module_harness/tests/test_align.py
"""对齐检查 harness：ALIGN_CHECK_CONFIG / register_align_check_harness / 端到端。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.align import ALIGN_CHECK_CONFIG, register_align_check_harness
from module_harness.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.config import HarnessConfig
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist
from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestRegisterAlignCheck:
    def test_registers_via_builtins(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        assert reg.is_harness("align_check")
        cfg = reg.harness_config("align_check")
        assert cfg.output_format is not None
        assert cfg.output_format.type == "json_object"
        assert "{spec}" in cfg.prompt_core
        assert "{tasklist}" in cfg.prompt_core
        assert "{node}" in cfg.prompt_core

    def test_builtin_names_contains_align_check(self):
        assert "align_check" in BUILTIN_HARNESS_NAMES

    def test_custom_name(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_align_check_harness(reg, name="my_align")
        assert reg.is_harness("my_align")
        assert not reg.is_harness("align_check")

    def test_config_shape(self):
        assert ALIGN_CHECK_CONFIG.name == "align_check"
        assert ALIGN_CHECK_CONFIG.temperature == 0.1


class TestAlignCheckEndToEnd:
    @pytest.mark.asyncio
    async def test_align_check_node_outputs_dict(self, mock_llm):
        """align_check 节点输出为解析后的 dict（json_object 自动提取）。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"aligned": true, "suggestions": "ok"}'
        )
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_check",
                    inputs={
                        "spec": "{spec}", "tasklist": "{tasklist}", "node": "{node}",
                        "output_a": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好"}))
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=10)

        out = runner.run_state.last_output("C")
        assert out == {"aligned": True, "suggestions": "ok"}
        # C 的 prompt 含 spec / tasklist / 当前位置
        assert mock_llm.complete.await_count == 2
        prompt = mock_llm.complete.call_args_list[1].kwargs["prompt"]
        assert '"source_text": "你好"' in prompt
        assert '"Tasks"' in prompt
        assert "当前位置: C" in prompt

    @pytest.mark.asyncio
    async def test_aligned_false_does_not_block(self, mock_llm):
        """aligned=false 是普通节点输出，不阻断 run（框架不强制）。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"aligned": false, "suggestions": "偏离目标"}'
        )
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_check",
                    inputs={
                        "spec": "{spec}", "tasklist": "{tasklist}", "node": "{node}",
                        "output_a": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好"}))
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=10)

        assert runner.status.value == "idle"  # run 正常结束
        assert runner.run_state.last_output("C") == {
            "aligned": False, "suggestions": "偏离目标",
        }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_align.py -q`
Expected: 6 failed — `ModuleNotFoundError: module_harness.align` / `AttributeError: BUILTIN_HARNESS_NAMES 无 align_check`

- [ ] **Step 3: 实现**

创建 `module_harness/align.py`：

```python
# module_harness/align.py
"""对齐检查 — 内置 align_check harness 节点（roadmap #2）。

普通 harness 节点：模板设计者在 flow 中自行插入（通常放在关键产出节点之后），
框架不额外调度。不插入即不执行。
"""

from __future__ import annotations

from .config import HarnessConfig
from .outputfmt import OutputFormat
from .registry import HarnessRegistry


ALIGN_CHECK_CONFIG = HarnessConfig(
    name="align_check",
    prompt_core=(
        "你是对齐检查器。判断当前节点产出是否偏离 spec 目标。\n"
        "spec: {spec}\n"
        "tasklist: {tasklist}\n"
        "当前位置: {node}\n"
        "结合前置节点输出判断，输出 JSON："
        '{"aligned": true/false, "suggestions": "..."}'
    ),
    output_format=OutputFormat(type="json_object"),
    temperature=0.1,
)


def register_align_check_harness(
    reg: HarnessRegistry, name: str = "align_check"
) -> None:
    """注册内置对齐检查 harness（默认名 align_check）。"""
    reg.harness(name, ALIGN_CHECK_CONFIG)
```

修改 `module_harness/builtins.py`：

```python
from .align import register_align_check_harness
from .config import HarnessConfig, OutputFormat
from .consistency import register_review_harness
from .registry import HarnessRegistry

BUILTIN_HARNESS_NAMES: frozenset[str] = frozenset({
    "spec_to_tasklist", "spec_tasklist_review", "align_check",
})
```

（`SPEC_TO_TASKLIST_CONFIG` 不变）`register_builtin_harnesses` 末尾追加：

```python
def register_builtin_harnesses(reg: HarnessRegistry) -> None:
    """注册内置 harness（spec_to_tasklist、spec_tasklist_review、align_check）。
    幂等，可重复调用。"""
    reg.harness("spec_to_tasklist", SPEC_TO_TASKLIST_CONFIG)
    register_review_harness(reg)
    register_align_check_harness(reg)
```

修改 `module_harness/__init__.py` — import 区加：

```python
from .align import ALIGN_CHECK_CONFIG, register_align_check_harness
```

`__all__` 加：

```python
    # 对齐检查
    "ALIGN_CHECK_CONFIG",
    "register_align_check_harness",
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_align.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add module_harness/align.py module_harness/builtins.py module_harness/__init__.py module_harness/tests/test_align.py
git commit -m "feat(align): built-in align_check harness with spec/tasklist/node tokens"
```

---

### Task 3: 运行状态查询 API（status.py）

**Files:**
- Create: `module_harness/status.py`
- Modify: `module_harness/__init__.py`
- Test: `module_harness/tests/test_run_status.py`

**接口:**
- 产生: `ModuleStatus` dataclass（module_id/phase/status/tick/fireable/outputs/node_states/error/updated_at）
- 产生: `query_run_status(module_id: str, base_dir: Path | None = None) -> ModuleStatus | None`
- status.json 缺失 → None；JSON 损坏 → None + log warning；DB 读失败 → 降级 phase-only

- [ ] **Step 1: 编写失败测试**（新文件 `module_harness/tests/test_run_status.py`）

```python
# module_harness/tests/test_run_status.py
"""运行状态查询 API：query_run_status / ModuleStatus 字段/降级路径。"""

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from module_harness.status import ModuleStatus, query_run_status
from tickflow.persistence import SqliteBackend


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _write_status(tmp_path, module_id="mod_x", phase="running", error=None, updated_at=100.0):
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(
        json.dumps({
            "module_id": module_id, "phase": phase, "error": error,
            "updated_at": updated_at,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return run_dir


class TestQueryRunStatus:
    def test_missing_status_returns_none(self, tmp_path):
        assert query_run_status("mod_x", base_dir=tmp_path) is None

    def test_phase_only_without_db(self, tmp_path):
        _write_status(tmp_path, phase="running", updated_at=100.0)
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st is not None
        assert isinstance(st, ModuleStatus)
        assert st.module_id == "mod_x"
        assert st.phase == "running"
        assert st.status is None
        assert st.tick is None
        assert st.outputs == {}
        assert st.node_states == {}
        assert st.fireable == []
        assert st.updated_at == 100.0

    def test_full_snapshot_query(self, tmp_path):
        run_dir = _write_status(tmp_path, phase="running", updated_at=100.0)
        backend = SqliteBackend(run_dir / "run.sqlite")
        backend.save_snapshot("mod_x", 2, {
            "tick": 2,
            "status": "running",
            "fireable": ["B"],
            "edges": {"A": [[1, "out1"], [2, "out2"]]},
            "state": {"A": {"_prompt": "x"}},
        })
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.status == "running"
        assert st.tick == 2
        assert st.fireable == ["B"]
        assert st.outputs == {"A": "out2"}          # edges 窗口最新值
        assert st.node_states == {"A": {"_prompt": "x"}}

    def test_corrupt_status_json_returns_none(self, tmp_path, caplog):
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_x"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text("not json{{", encoding="utf-8")
        assert query_run_status("mod_x", base_dir=tmp_path) is None
        assert "status.json" in caplog.text

    def test_db_failure_degrades_to_phase_only(self, tmp_path, monkeypatch):
        run_dir = _write_status(tmp_path, phase="done", updated_at=1.0)
        SqliteBackend(run_dir / "run.sqlite").close()

        def boom(self, session_id):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(SqliteBackend, "latest_tick", boom)
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.phase == "done"          # 降级为 phase-only
        assert st.status is None
        assert st.tick is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_run_status.py -q`
Expected: 5 failed — `ModuleNotFoundError: module_harness.status`

- [ ] **Step 3: 实现**

创建 `module_harness/status.py`：

```python
# module_harness/status.py
"""运行状态查询 — 跨进程读取 Module 当前运行状态（roadmap #7）。

数据真相源：``.specmodule/runs/<module_id>/status.json``（阶段级，Module 原子写）
+ ``run.sqlite`` 最新快照（tick 级，persist 模式每 tick 写）。
独立于 Module 内部实现——任何进程可直接查询。

并发安全：SQLite WAL 模式下单写者 + 多读者读写互不阻塞（实测 500 次写对撞
读取 0 次 database is locked）。同一 module_id 不可并发（双写者会锁冲突）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ModuleStatus:
    """Module 运行状态静态快照。"""

    module_id: str
    phase: str                 # idle/translating/reviewing/building/ready/running/done/aborted/cancelled
    status: str | None = None  # tickflow RunStatus（"RUNNING"/"IDLE"/...；无 DB 时为 None）
    tick: int | None = None    # 最新快照 tick（无 DB 时为 None）
    fireable: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)     # node → 最新输出
    node_states: dict[str, dict] = field(default_factory=dict)  # node → mutable state
    error: str | None = None
    updated_at: float = 0.0


def _run_dir(module_id: str, base_dir: Path) -> Path:
    return base_dir / ".specmodule" / "runs" / module_id


def query_run_status(
    module_id: str, base_dir: Path | None = None
) -> ModuleStatus | None:
    """查询 Module 当前运行状态。未开始（status.json 缺失）→ None。

    有 ``run.sqlite`` 时叠加最新快照的 tick 级信息；DB 读失败降级为
    phase-only（监控方绝不被 DB 锁搞崩）。
    """
    base = base_dir or Path.cwd()
    run_dir = _run_dir(module_id, base)
    status_path = run_dir / "status.json"
    if not status_path.exists():
        return None
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("status.json 损坏或不可读: %s", status_path)
        return None

    st = ModuleStatus(
        module_id=str(data.get("module_id", module_id)),
        phase=str(data.get("phase", "unknown")),
        error=data.get("error"),
        updated_at=float(data.get("updated_at", 0.0)),
    )

    db_path = run_dir / "run.sqlite"
    if db_path.exists():
        try:
            from tickflow.persistence import SqliteBackend

            backend = SqliteBackend(db_path)
            try:
                tick = backend.latest_tick(module_id)
                if tick is not None:
                    snap = backend.load_snapshot(module_id, tick)
                    st.status = snap.get("status")
                    st.tick = snap.get("tick", tick)
                    st.fireable = list(snap.get("fireable", []))
                    st.outputs = {
                        n: lst[-1][1] for n, lst in snap.get("edges", {}).items()
                    }
                    st.node_states = dict(snap.get("state", {}))
            finally:
                backend.close()
        except Exception:
            log.exception("读取 run.sqlite 失败，降级为 phase-only")
    return st
```

修改 `module_harness/__init__.py` — import 区加：

```python
from .status import ModuleStatus, query_run_status
```

`__all__` 加：

```python
    # 运行状态查询
    "ModuleStatus",
    "query_run_status",
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_run_status.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add module_harness/status.py module_harness/__init__.py module_harness/tests/test_run_status.py
git commit -m "feat(status): cross-process query_run_status over status.json + latest snapshot"
```

---

### Task 4: Module 生命周期阶段机 + status.json 原子写（module.py）

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_run_status.py`（追加）

**接口:**
- 新增私有方法 `Module._write_phase(phase: str, error: str | None = None) -> None` — 原子写 status.json（tmp + os.replace），OSError 仅 log
- `__init__` 签名加 `status_file: bool = True`（默认 True，#7 开箱可用）——独立开关，与 persist 正交：`status_file=False` 时不写盘（零残留）；快速模式 = `persist=False + status_file=False`
- `__init__` 末尾写 `idle`；`_build_runner_async` 写 `reviewing`/`translating` → `building` → `ready`；`run()` 写 `running` → `done`/`aborted`/`cancelled`
- RunStatus 映射：IDLE → done；ABORTED → aborted；CANCELLED → cancelled；FAILED → aborted（非正常结束统一归中止）

- [ ] **Step 1: 编写失败测试**（追加到 `test_run_status.py`）

import 区补充：

```python
import asyncio

from tickflow import Failure
from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist
```

追加测试类：

```python
class TestModulePhase:
    """Module 阶段机：status.json 原子写。"""

    def _read_status(self, tmp_path, module_id="mod_test"):
        return json.loads(
            (tmp_path / ".specmodule" / "runs" / module_id / "status.json")
            .read_text(encoding="utf-8")
        )

    def _script_reg(self, mock_llm, **scripts):
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus())

        def echo(view):
            return {"ok": True}

        reg.script("echo")(echo)
        for name, fn in scripts.items():
            reg.script(name)(fn)
        return reg

    def _script_tasklist(self):
        return Tasklist(
            tasks={"A": TaskDefinition(type="script", script="echo")},
            flow="[A]",
        )

    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, persist=False, **kw):
        monkeypatch.chdir(tmp_path)
        return Module(
            spec={"x": 1},
            tasklist=tasklist or self._script_tasklist(),
            llm_client=mock_llm,
            registry=self._script_reg(mock_llm),
            review_harness=None,
            persist=persist,
            module_id="mod_test",
            **kw,
        )

    def test_init_writes_idle(self, tmp_path, monkeypatch, mock_llm):
        self._make_module(mock_llm, tmp_path, monkeypatch)
        assert self._read_status(tmp_path)["phase"] == "idle"

    def test_build_runner_writes_ready(self, tmp_path, monkeypatch, mock_llm):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        mod.build_runner()
        assert self._read_status(tmp_path)["phase"] == "ready"

    @pytest.mark.asyncio
    async def test_run_writes_done(self, tmp_path, monkeypatch, mock_llm):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        assert self._read_status(tmp_path)["phase"] == "done"

    @pytest.mark.asyncio
    async def test_phase_running_mid_run(self, tmp_path, monkeypatch, mock_llm):
        """运行中（手动 tick 循环）phase 应为 running。"""

        async def slow(view):
            await asyncio.sleep(0.2)   # 真实阻塞点：让 run() 停在 running
            return {"ok": True}

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch, persist=True,
            registry=self._script_reg(mock_llm, slow=slow),
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="slow")},
                flow="[A]",
            ),
        )
        task = asyncio.create_task(mod.run())
        await asyncio.sleep(0.05)
        assert self._read_status(tmp_path)["phase"] == "running"
        await task
        assert self._read_status(tmp_path)["phase"] == "done"

    @pytest.mark.asyncio
    async def test_run_aborted_phase(self, tmp_path, monkeypatch, mock_llm):
        """基础设施 Failure → aborted + error 记录。"""

        def boom(view):
            return Failure("infra down", type="infrastructure")

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._script_reg(mock_llm, boom=boom),
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="boom")},
                flow="[A]",
            ),
        )
        await mod.run()
        st = self._read_status(tmp_path)
        assert st["phase"] == "aborted"
        assert st["error"] == "aborted"

    @pytest.mark.asyncio
    async def test_persist_mode_end_to_end_query(self, tmp_path, monkeypatch, mock_llm):
        """persist=True：run 后 status.json + run.sqlite 都在，query 读全字段。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()

        st = query_run_status("mod_test", base_dir=tmp_path)
        assert st.phase == "done"
        assert st.status == "idle"        # 运行结束后 runner status
        assert st.tick is not None
        assert st.outputs == {"A": {"ok": True}}
        assert st.node_states == {"A": {}}

    def test_status_file_false_no_residue(self, tmp_path, monkeypatch, mock_llm):
        """status_file=False：不写 status.json（零残留）。"""
        self._make_module(mock_llm, tmp_path, monkeypatch, status_file=False)
        assert not (tmp_path / ".specmodule").exists()

    @pytest.mark.asyncio
    async def test_persist_false_status_file_true_phase_only(self, tmp_path, monkeypatch, mock_llm):
        """persist=False + status_file=True：只写 status.json，phase 可查、tick 降级。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=False)
        await mod.run()
        assert self._read_status(tmp_path)["phase"] == "done"
        st = query_run_status("mod_test", base_dir=tmp_path)
        assert st.phase == "done"
        assert st.tick is None          # 无 DB
        assert st.outputs == {}
        assert not (tmp_path / ".specmodule" / "runs" / "mod_test" / "run.sqlite").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_run_status.py::TestModulePhase -q`
Expected: 8 failed — `FileNotFoundError: status.json` / `.specmodule` 不存在（Module 尚未写）

- [ ] **Step 3: 实现**（修改 `module_harness/module.py`）

import 区补充：

```python
import json
import logging
import os
```

（`time`/`uuid`/`Path` 已有）加模块级 log：

```python
log = logging.getLogger(__name__)
```

新增 `_status_path` helper（放在 `_persist_dir` 之后）：

```python
def _status_path(module_id: str) -> Path:
    """``<工作目录>/.specmodule/runs/<module_id>/status.json``（roadmap #7）。

    阶段级运行状态文件：与 run.sqlite 同目录，跨进程查询的轻量通道。
    """
    return Path.cwd() / ".specmodule" / "runs" / module_id / "status.json"
```

`Module` 类内新增私有方法（放在 `__init__` 之后）：

```python
    # ------------------------------------------------------------------
    # 运行状态（roadmap #7）
    # ------------------------------------------------------------------

    def _write_phase(self, phase: str, error: str | None = None) -> None:
        """原子写 status.json（tmp + os.replace）。失败仅 log，不阻断运行。

        phase 取值：idle/translating/reviewing/building/ready/running/
        done/aborted/cancelled。status_file=False 时不写盘（零残留）。
        """
        if not self.status_file:
            return
        path = _status_path(self.module_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({
                    "module_id": self.module_id,
                    "phase": phase,
                    "error": error,
                    "updated_at": time.time(),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.exception("写 status.json 失败（不阻断运行）: %s", path)
```

`__init__` 签名加 `status_file: bool = True`（`persist` 之后），赋值并注释（True（默认）：写 .specmodule/runs/<module_id>/status.json（阶段级，跨进程查询通道）；False：零残留（快速模式可用））：

`__init__` 末尾（`self.module_id = ...` 赋值之后，构造函数最后一行）加：

```python
        self._write_phase("idle")
```

`submodule.py` 的 Module 构造调用同步传 `status_file=(self.mode != "fast")`（保持 fast = 完全零残留）；`test_storage_persist.py` 快速模式用例的 Module 构造加 `status_file=False`（意图"关闭所有落盘"）。

`_build_runner_async` 改造 — tasklist 分支开头加：

```python
        if self.tasklist is not None:
            self._write_phase("reviewing")
            tasklist = self.tasklist
```

else 分支开头加：

```python
        else:
            self._write_phase("translating")
            template = self._loader.get(self.template_name)
```

（`if template is None: raise ...` 与 translate 调用不变）在 `builder = TasklistTranslator(...)` 之前加：

```python
        self._write_phase("building")
        builder = TasklistTranslator(self._reg, self.module_id)
```

在 `return AsyncRunner(...)` 之前加：

```python
        self._write_phase("ready")
        return AsyncRunner(
```

`run()` 改造：

```python
    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。"""
        from tickflow.runner import RunStatus

        runner = await self._build_runner_async()
        self._write_phase("running")
        try:
            firings = await runner.run_until_idle(max_ticks=max_ticks)
        finally:
            if runner.status == RunStatus.ABORTED:
                self._write_phase("aborted", error=runner.cancel_reason or "aborted")
            elif runner.status == RunStatus.CANCELLED:
                self._write_phase("cancelled", error=runner.cancel_reason)
            elif runner.status == RunStatus.FAILED:
                self._write_phase("aborted", error="all nodes failed")
            else:
                self._write_phase("done")
        return firings
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_run_status.py -q`
Expected: 14 passed（TestQueryRunStatus 6 + TestModulePhase 8）

- [ ] **Step 5: Commit**

```bash
git add module_harness/module.py module_harness/tests/test_run_status.py
git commit -m "feat(module): lifecycle phase machine writes status.json atomically"
```

---

### Task 5: roadmap 文档更新 + 全量回归

**Files:**
- Modify: `docs/progress/module-roadmap.md`

- [ ] **Step 1: 更新 roadmap**

1. 第 6 行"已实现：**15** / 待实现：**4**" → `已实现：**17** / 待实现：**2**`
2. 第 4 行日期 `> 最后更新：2026-08-05` → `> 最后更新：2026-08-06`
3. "编排与基础设施"表格追加两行：

```markdown
| **运行状态查询** — 跨进程查询 Module 当前运行状态：status.json 阶段机（9 阶段原子写）+ run.sqlite 最新快照 | `Module._write_phase` + `query_run_status` | `module.py`, `status.py` |
| **对齐检查** — 内置 `align_check` harness 节点，spec/tasklist/位置/前置输出注入，输出对齐/偏离 + 建议 | `ALIGN_CHECK_CONFIG` + 常量 token `{spec}/{tasklist}/{node}` | `align.py`, `graph_builder.py` |
```

4. "待实现"节删除"### 2. 对齐检查"与"### 7. 运行状态查询"两个小节（保留 #5 快照/回滚、#6 数据暴露 SDK）
5. "实现顺序建议"图更新：

```markdown
┌─────────────────────────┐
│ 1. spec+tasklist 输入   │  ✅ 已完成（含一致性审核 #4）
├─────────────────────────┤
│ 2. 运行状态查询         │  ✅ 已完成
├─────────────────────────┤
│ 3. 对齐检查 harness     │  ✅ 已完成
├─────────────────────────┤
│ 4. 一致性审核           │  ✅ 随 #1 完成
├─────────────────────────┤
│ 5. submodule + 打包/发布│  ✅ 已完成
├─────────────────────────┤
│ 6. 快照/回滚封装        │  ← 依赖 #1
├─────────────────────────┤
│ 7. SDK 数据暴露层       │  ← 可与其他并行，渐进生长
└─────────────────────────┘
```

6. 末尾说明更新为：`#1 → #4 → #5 → #6 & #2` 有依赖链 → `#1 → #4 → #5 → #6`；`#3` 和 `#7` 已独立完成

- [ ] **Step 2: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 229 passed（基线 209 + 新增 20：Task1 3 + Task2 6 + Task3 5 + Task4 6）, 1 failed（smoke 真实 LLM 测试，与改动前基线一致）, 1 xfailed

- [ ] **Step 3: Commit**

```bash
git add docs/progress/module-roadmap.md
git commit -m "docs: mark run-status query (#7) and align check (#2) done (17/19)"
```
