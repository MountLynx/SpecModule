# specmodule CLI（Phase 0 子集）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `specmodule` CLI 的 run（实时三级观察）/ status / review 三命令，打通"M1 论文优化 module 在 CLI 跑通"的验收闭环。

**Architecture:** 模块入口经 `modules/<name>.py` 声明 `ModuleEntry`（entry.py 目录发现）；实时显示由 `AsyncRunner` 的 `on_fire`/`on_tick_start` hooks 驱动（Module 新增 hooks 透传）；review 时间线组合沉淀共享层 query.py（CLI 只 import 不实现，MCP/Web 复用）。

**Tech Stack:** Python 3.13，argparse，asyncio，pytest + unittest.mock，tickflow（AsyncRunner hooks + SqliteBackend），无新增外部依赖。

**测试命令（仓库根目录）：**

```bash
python -m pytest module_harness/tests/test_entry.py -q
python -m pytest module_harness/tests/test_module_hooks.py -q
python -m pytest module_harness/tests/test_query.py -q
python -m pytest module_harness/tests/test_cli.py -q
python -m pytest module_harness/tests/ example/ -q   # 全量回归
```

**设计依据：** `docs/superpowers/specs/2026-08-10-specmodule-cli-design.md`

---

## 执行顺序（探索定稿，2026-08-11）

依赖根部在前、叶子并行、下游依赖上游。**从 Task 1 入手**——它是依赖根、完全自包含（零外部依赖、无 LLM/网络/SQLite）、TDD 最干净（10 passed 闭环最快）。

```
       ┌─ Task 2  Module hooks ──────────────┐
Task 1 entry.py ──┬──▶ Task 4  example M1 入口 ─┴──▶ Task 5  cli.py ──▶ Task 6  test_cli
                  └──▶ Task 3  query.py ────────────┘                     │
                                                                          ▼
                                                                   Task 7 验收+roadmap
```

**推荐顺序**（单步最小可验证）：

1. **Task 1** `entry.py`（依赖根 + 自包含 + 零风险）
2. **Task 3** `query.py`（叶子，比 Task 5 更独立；与 Task 2 可换序）
3. **Task 2** `Module hooks`（三者中最小，可随时插入）
4. **Task 4** example M1 入口（需 Task 1）
5. **Task 5** `cli.py`（需 1+2+3+4）
6. **Task 6** `test_cli.py`（需 5）
7. **Task 7** 验收 + roadmap（收口）

Task 1/2/3 三个叶子理论可并行（`subagent-driven-development` 可一次派三个），但追求单步最小验证时按上述顺序逐个跑。

**执行前已知缺陷（Task 5 实现时一并修）**：`_cmd_run` 的 `except KeyboardInterrupt` 引用 `display.firings` 与 `mod.module_id`，二者在 `try` 块内绑定——Ctrl+C 落在 pre-run 阶段（discover/parse/LLM-config）会抛 `NameError` 而非退出码 2。修法：`try` 前预绑定 `display = mod = None`。

---

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `module_harness/entry.py` | 新建 | ModuleEntry 合约 + discover_modules() |
| `module_harness/module.py` | 修改 | `__init__` 加 `hooks` 参数；`_build_runner_async` 注册 |
| `module_harness/query.py` | 新建 | ReviewEntry/ReviewTimeline + build_timeline/filter_*/timeline_to_dict |
| `module_harness/cli.py` | 新建 | argparse 子命令 run/status/review + RunDisplay 三级显示 |
| `module_harness/__init__.py` | 修改 | 导出 ModuleEntry/discover_modules/查询层 |
| `example/academic_writer.py` | 修改 | `_build_registry` 增加 `event_bus` 参数（向后兼容） |
| `example/modules/academic_writer.py` | 新建 | M1 模块入口（ModuleEntry 声明） |
| `example/spec.academic_writer.json` | 新建 | 验收用 spec 示例 |
| `docs/progress/module-roadmap.md` | 修改 | Phase 0 子集交付记录 + 后续迭代清单 |

---

## Task 1: 模块入口合约（entry.py）

**Files:**
- Create: `module_harness/entry.py`
- Test: `module_harness/tests/test_entry.py`
- Modify: `module_harness/__init__.py`（末尾追加导出）

- [ ] **Step 1: 写失败测试**

创建 `module_harness/tests/test_entry.py`：

```python
# module_harness/tests/test_entry.py
"""模块入口合约：ModuleEntry + discover_modules 目录发现。"""

from __future__ import annotations

from module_harness.entry import ModuleEntry, discover_modules

GOOD = '''
from module_harness.entry import ModuleEntry

entry = ModuleEntry(
    name="hello",
    description="测试模块",
    templates={"hello": {}},
    build_registry=None,
    default_template="hello",
)
'''

GOOD_B = '''
from module_harness.entry import ModuleEntry

entry = ModuleEntry(
    name="hello",
    description="第二个 hello（覆盖用）",
    templates={},
)
'''


def _write(tmp_path, name, body, subdir="modules"):
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return d


class TestDiscoverModules:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert discover_modules(tmp_path / "nope") == {}

    def test_empty_dir_returns_empty(self, tmp_path):
        d = tmp_path / "modules"
        d.mkdir()
        assert discover_modules(d) == {}

    def test_single_module(self, tmp_path):
        d = _write(tmp_path, "hello.py", GOOD)
        entries = discover_modules(d)
        assert set(entries) == {"hello"}
        assert entries["hello"].description == "测试模块"
        assert entries["hello"].default_template == "hello"

    def test_skip_file_without_entry(self, tmp_path):
        d = _write(tmp_path, "noentry.py", "x = 1\n")
        assert discover_modules(d) == {}

    def test_skip_file_with_wrong_entry_type(self, tmp_path):
        d = _write(tmp_path, "badtype.py", 'entry = "not a module"\n')
        assert discover_modules(d) == {}

    def test_skip_double_underscore_file(self, tmp_path):
        d = _write(tmp_path, "_private.py", GOOD)
        assert discover_modules(d) == {}

    def test_duplicate_name_last_wins(self, tmp_path):
        d = _write(tmp_path, "a.py", GOOD)
        _write(tmp_path, "b.py", GOOD_B)
        entries = discover_modules(d)
        assert list(entries) == ["hello"]
        assert entries["hello"].description == "第二个 hello（覆盖用）"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_entry.py -q`
Expected: FAIL（`ModuleError: No module named 'module_harness.entry'` 或 import 错误）

- [ ] **Step 3: 实现 entry.py**

创建 `module_harness/entry.py`：

```python
# module_harness/entry.py
"""模块入口合约：ModuleEntry + 目录发现（roadmap Phase 0，CLI 使用）。

一个 module 一个 py 文件（``modules/<name>.py``），文件内声明模块级
``entry`` 变量。未来 ``init`` 脚手架可据此生成实例骨架
（scripts/harnesses/submodules/modules 分目录）。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .events import EventBus
from .registry import HarnessRegistry

log = logging.getLogger(__name__)


@dataclass
class ModuleEntry:
    """模块入口声明：模板 + submodule + registry 构建 + 默认 spec/schema。"""

    name: str
    description: str
    templates: dict[str, dict]                                   # {模板名: TasklistTemplate JSON}
    submodules: dict[str, type] = field(default_factory=dict)    # {tasklist 名: SubModule 类}
    build_registry: Callable[[Any, str, EventBus], HarnessRegistry] | None = None
    default_spec: dict[str, Any] | None = None
    default_template: str | None = None
    spec_schema: dict[str, str] | None = None                    # {字段: 类型名}
    review_harness: str | None = "spec_tasklist_review"

    def __post_init__(self) -> None:
        if self.default_template is not None and self.default_template not in self.templates:
            raise ValueError(f"default_template '{self.default_template}' 不在 templates 中")


def discover_modules(modules_dir: Path | str) -> dict[str, ModuleEntry]:
    """扫描 ``modules_dir/*.py``，导入后收集模块级 ``entry`` 变量。

    缺 ``entry`` 或类型不符的文件跳过并 log 警告；同名冲突后者覆盖 + 警告；
    文件导入抛异常跳过 + log exception（不阻断整体发现）。
    """
    out: dict[str, ModuleEntry] = {}
    d = Path(modules_dir)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"specmodule_module_{p.stem}", p
        )
        if spec is None or spec.loader is None:
            log.warning("无法加载模块入口文件（跳过）: %s", p)
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            log.exception("模块入口加载失败（跳过）: %s", p)
            continue
        entry = getattr(mod, "entry", None)
        if not isinstance(entry, ModuleEntry):
            log.warning("文件 %s 缺少 entry 变量（ModuleEntry）——跳过", p)
            continue
        if entry.name in out:
            log.warning("模块名 '%s' 重复（%s 覆盖）", entry.name, p)
        out[entry.name] = entry
    return out
```

- [ ] **Step 4: 更新 `__init__.py` 导出**

在 `module_harness/__init__.py` 的 `from .status import ModuleStatus, query_run_status` 之后追加：

```python
from .entry import ModuleEntry, discover_modules
```

在 `__all__` 末尾 `"check_resume_compat",` 之后追加：

```python
    # 模块入口（roadmap Phase 0：CLI 使用）
    "ModuleEntry",
    "discover_modules",
]
```

注意：把 `__all__` 的结尾 `]` 替换为上面的整块（保留原 `"check_resume_compat",` 行）。

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_entry.py -q`
Expected: PASS（10 passed）

- [ ] **Step 6: 提交**

```bash
git add module_harness/entry.py module_harness/tests/test_entry.py module_harness/__init__.py
git commit -m "feat: 模块入口合约 ModuleEntry + discover_modules 目录发现（CLI Phase 0）"
```

---

## Task 2: Module runner hooks 透传

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_module_hooks.py`

- [ ] **Step 1: 写失败测试**

创建 `module_harness/tests/test_module_hooks.py`：

```python
# module_harness/tests/test_module_hooks.py
"""Module hooks 透传：on_tick_start / on_fire 注册与调用。"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist


def _registry(llm):
    reg = HarnessRegistry(llm_client=llm, event_bus=None)

    @reg.script("greet")
    async def greet(view):
        return "hello"

    return reg


def _tasklist() -> Tasklist:
    return Tasklist(
        tasks={"Greet": TaskDefinition(type="script", script="greet")},
        flow="[Greet]",
    )


def _module(**kw) -> Module:
    llm = MagicMock()
    return Module(
        spec={},
        tasklist=_tasklist(),
        llm_client=llm,
        registry=_registry(llm),
        persist=False,
        status_file=False,
        review_harness=None,
        **kw,
    )


def test_on_fire_receives_node_state():
    seen = []

    async def on_fire(ns):
        seen.append(ns)

    asyncio.run(_module(hooks={"on_fire": on_fire}).run(max_ticks=10))
    assert len(seen) == 1
    assert seen[0].node == "Greet"
    assert seen[0].status == "ok"
    assert seen[0].output == "hello"


def test_on_tick_start_receives_tick():
    ticks = []

    async def on_tick_start(tick, fireable):
        ticks.append((tick, list(fireable)))

    asyncio.run(_module(hooks={"on_tick_start": on_tick_start}).run(max_ticks=10))
    assert ticks, "on_tick_start 未被调用"
    assert ticks[0][0] == 0
    assert "Greet" in ticks[0][1]


def test_unknown_hook_name_ignored():
    async def on_nope(ns):
        raise AssertionError("不应被调用")

    # 未知 hook 名只 log 警告，不抛错、不运行
    asyncio.run(_module(hooks={"on_nope": on_nope}).run(max_ticks=10))
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_module_hooks.py -q`
Expected: 至少一个 FAIL（`TypeError: Module.__init__() got an unexpected keyword argument 'hooks'`）

- [ ] **Step 3: 实现 Module hooks**

修改 `module_harness/module.py`：

1. `__init__` 签名末尾（`modules: dict[str, Any] | None = None,` 之后）追加参数：

```python
        modules: dict[str, Any] | None = None,
        hooks: dict | None = None,
```

2. `__init__` 体内（`self._modules = dict(modules or {})` 之后）追加：

```python
        # runner hooks 透传（观察通道）：{hook名: async/sync 回调}，构造
        # runner 后注册。CLI 实时显示使用 on_tick_start/on_fire。
        self._hooks = dict(hooks or {})
```

3. `_build_runner_async` 中 `self._runner = runner` 与 `return runner` 之间插入：

```python
        self._runner = runner
        for _hook_name, _cb in self._hooks.items():
            _register = getattr(runner, _hook_name, None)
            if callable(_register):
                _register(_cb)
            else:
                log.warning("Module hooks: 未知 runner hook '%s'（忽略）", _hook_name)
        return runner
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_module_hooks.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 回归现有测试**

Run: `python -m pytest module_harness/tests/ -q`
Expected: PASS（全量，hooks 为可选参数不破坏现有调用）

- [ ] **Step 6: 提交**

```bash
git add module_harness/module.py module_harness/tests/test_module_hooks.py
git commit -m "feat: Module 增加 runner hooks 透传（on_tick_start/on_fire 等，CLI 实时显示通道）"
```

---

## Task 3: 共享查询层（query.py）

**Files:**
- Create: `module_harness/query.py`
- Test: `module_harness/tests/test_query.py`
- Modify: `module_harness/__init__.py`（追加导出）

- [ ] **Step 1: 写失败测试**

创建 `module_harness/tests/test_query.py`：

```python
# module_harness/tests/test_query.py
"""共享查询层：review 时间线组合（分组/去重/过滤/JSON）。"""

from __future__ import annotations

from tickflow.persistence import SqliteBackend
from tickflow.state import NodeState

from module_harness.query import (
    build_timeline,
    filter_failed,
    filter_node,
    filter_tick,
    timeline_to_dict,
)


def _seed(tmp_path, module_id="mod_x"):
    """写 4 条 firings：A@1 ok、B@1 failed、A@2 ok（含一条 (2,A) 重复）。"""
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(run_dir / "run.sqlite")
    backend.save_firing(module_id, NodeState(tick=1, node="A", output="a1"))
    backend.save_firing(
        module_id,
        NodeState(tick=1, node="B", output="b1", status="failed", error="boom"),
    )
    backend.save_firing(module_id, NodeState(tick=2, node="A", output="a2"))
    backend.save_firing(module_id, NodeState(tick=2, node="A", output="a2dup"))
    backend.close()
    return tmp_path


class TestBuildTimeline:
    def test_no_db_returns_none(self, tmp_path):
        assert build_timeline("mod_x", base_dir=tmp_path) is None

    def test_dedup_and_order(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        assert tl is not None
        assert tl.module_id == "mod_x"
        assert [(e.tick, e.node) for e in tl.entries] == [(1, "A"), (1, "B"), (2, "A")]
        assert tl.entries[1].status == "failed"
        assert tl.entries[1].error == "boom"
        assert tl.entries[2].output == "a2"      # 同 (tick,node) 保留首条
        assert tl.latest_tick == 2


class TestFilters:
    def setup_method(self):
        self.tl = build_timeline.__wrapped__ if hasattr(build_timeline, "__wrapped__") else None

    def test_filter_failed(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        failed = filter_failed(tl)
        assert [e.node for e in failed.entries] == ["B"]
        assert failed.latest_tick == 2

    def test_filter_tick(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        tick1 = filter_tick(tl, 1)
        assert [e.node for e in tick1.entries] == ["A", "B"]

    def test_filter_node(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        node_a = filter_node(tl, "A")
        assert [e.tick for e in node_a.entries] == [1, 2]


class TestTimelineToDict:
    def test_structure(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        d = timeline_to_dict(tl)
        assert d["module_id"] == "mod_x"
        assert d["latest_tick"] == 2
        assert d["entries"][0] == {
            "tick": 1, "node": "A", "status": "ok", "output": "a1", "error": None,
        }
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_query.py -q`
Expected: FAIL（`No module named 'module_harness.query'`）

- [ ] **Step 3: 实现 query.py**

创建 `module_harness/query.py`：

```python
# module_harness/query.py
"""共享查询层：review 历史时间线组合（roadmap Phase 0）。

CLI（host + 查询形态）、MCP、Web 三形态共同消费本模块——形态只 import，
绝不重实现。数据源：run.sqlite 的 firings 表；容错哲学同 query_run_status
（DB 读失败返回 None，监控方绝不被 DB 锁搞崩）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ReviewEntry:
    """单个节点 firing 的审阅记录。"""

    tick: int
    node: str
    status: str          # ok | failed | aborted
    output: Any = None
    error: str | None = None


@dataclass
class ReviewTimeline:
    """完整审阅时间线：firings 顺序，同 (tick, node) 去重 keep-first。"""

    module_id: str
    entries: list[ReviewEntry] = field(default_factory=list)
    latest_tick: int | None = None


def _run_db_path(module_id: str, base_dir: Path | None) -> Path:
    """``<base>/<cwd>/.specmodule/runs/<module_id>/run.sqlite``（与 Module 对齐）。"""
    base = base_dir if base_dir is not None else Path.cwd()
    return base / ".specmodule" / "runs" / module_id / "run.sqlite"


def build_timeline(module_id: str, base_dir: Path | None = None) -> ReviewTimeline | None:
    """从 run.sqlite 构建审阅时间线。无 DB / 读失败 → None。"""
    db_path = _run_db_path(module_id, base_dir)
    if not db_path.exists():
        return None
    try:
        from tickflow.persistence import SqliteBackend

        backend = SqliteBackend(db_path)
        try:
            rows = backend.list_firings(module_id)
        finally:
            backend.close()
    except Exception:
        log.exception("读取 run.sqlite 失败（返回 None）: %s", db_path)
        return None

    entries: list[ReviewEntry] = []
    seen: set[tuple[int, str]] = set()
    latest: int | None = None
    for d in rows:
        tick = int(d.get("tick", 0))
        node = d.get("node")
        if not node:
            continue
        if (tick, node) in seen:
            continue          # 与 tickflow audit() 去重语义一致（restore 重放兼容）
        seen.add((tick, node))
        entries.append(
            ReviewEntry(
                tick=tick,
                node=node,
                status=str(d.get("status", "ok")),
                output=d.get("output"),
                error=d.get("error"),
            )
        )
        latest = tick if latest is None else max(latest, tick)
    return ReviewTimeline(module_id=module_id, entries=entries, latest_tick=latest)


def filter_failed(timeline: ReviewTimeline) -> ReviewTimeline:
    """只看失败/中止节点（定位问题 tick 的核心路径）。"""
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=[e for e in timeline.entries if e.status != "ok"],
        latest_tick=timeline.latest_tick,
    )


def filter_tick(timeline: ReviewTimeline, tick: int) -> ReviewTimeline:
    """只看指定 tick。"""
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=[e for e in timeline.entries if e.tick == tick],
        latest_tick=timeline.latest_tick,
    )


def filter_node(timeline: ReviewTimeline, node: str) -> ReviewTimeline:
    """只看指定节点的全部 firing（含 loop 多轮）。"""
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=[e for e in timeline.entries if e.node == node],
        latest_tick=timeline.latest_tick,
    )


def timeline_to_dict(timeline: ReviewTimeline) -> dict[str, Any]:
    """JSON 出口（MCP/Web 直接消费同一函数）。"""
    return {
        "module_id": timeline.module_id,
        "latest_tick": timeline.latest_tick,
        "entries": [
            {
                "tick": e.tick,
                "node": e.node,
                "status": e.status,
                "output": e.output,
                "error": e.error,
            }
            for e in timeline.entries
        ],
    }
```

- [ ] **Step 4: 更新 `__init__.py` 导出**

在 `module_harness/__init__.py` 的 `from .entry import ModuleEntry, discover_modules` 之后追加：

```python
from .query import (
    ReviewEntry,
    ReviewTimeline,
    build_timeline,
    filter_failed,
    filter_node,
    filter_tick,
    timeline_to_dict,
)
```

在 `__all__` 的 `"discover_modules",` 之后追加：

```python
    # 共享查询层（roadmap Phase 0：CLI/MCP/Web 复用）
    "ReviewEntry",
    "ReviewTimeline",
    "build_timeline",
    "filter_failed",
    "filter_node",
    "filter_tick",
    "timeline_to_dict",
```

（若 Task 1 的 `__all__` 尚未应用到本文件，先完成 Task 1 的同类修改再追加。）

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_query.py -q`
Expected: PASS（7 passed）

- [ ] **Step 6: 提交**

```bash
git add module_harness/query.py module_harness/tests/test_query.py module_harness/__init__.py
git commit -m "feat: 共享查询层 query.py——review 时间线组合（分组/去重/过滤/JSON），CLI/MCP/Web 复用"
```

---

## Task 4: example 扩展——`_build_registry` 接入 event_bus + M1 模块入口

**Files:**
- Modify: `example/academic_writer.py:401-425`（`_build_registry` 加 `event_bus` 参数）
- Create: `example/modules/__init__.py`（空文件，使目录成为包）
- Create: `example/modules/academic_writer.py`（M1 模块入口：`_registry_for` 适配 + ModuleEntry）

> 设计依据（spec §ModuleEntry 合约）：`build_registry` 契约签名是
> `(llm_client, template_name, event_bus)`；而 `example.academic_writer._build_registry`
> 现有签名为 `(llm_client, mode)`——第二参数语义不同（mode 而非 template_name）。
> 因此入口文件定义 `_registry_for` 适配器按模板名换算 mode（spec 的 M1 示例
> 直接写 `build_registry=_build_registry` 是笔误，此处按 `_registry_for` 修正）。

- [ ] **Step 1: 修改 `_build_registry`（向后兼容）**

`example/academic_writer.py` 第 401-403 行替换为：

```python
def _build_registry(
    llm_client: Any,
    mode: str = "submodule",
    event_bus: EventBus | None = None,
) -> HarnessRegistry:
    """注册流水线 harness / script / guard（按模式；含模板通道翻译器）。

    ``event_bus`` 缺省 None → EventBus.null()（CLI 传入外部 bus 时接入，
    否则 CLI 收不到 harness 事件）。
    """
    reg = HarnessRegistry(
        llm_client=llm_client, event_bus=event_bus or EventBus.null()
    )
```

`run_writer` 内既有调用 `_build_registry(llm_client, mode)` 不变（第三个参数
缺省 None → 行为与原来完全一致）。

- [ ] **Step 2: 创建模块入口文件**

创建 `example/modules/__init__.py`（空文件，仅存在即可）。

创建 `example/modules/academic_writer.py`：

```python
"""academic_writer 模块入口（CLI 发现用）。

modules/ 目录扫描约定：一个 module 一个 py 文件，声明模块级 ``entry``
（ModuleEntry）。CLI ``specmodule run --module academic_writer`` 经
discover_modules() 导入本文件，读取 entry 获取模板/子模块/registry 构建方式。
"""

from __future__ import annotations

from typing import Any

from example.academic_writer import (
    ACADEMIC_TEMPLATE,
    DETAILED_TEMPLATE,
    FactReviewLoop,
    _build_registry,
)
from module_harness.entry import ModuleEntry
from module_harness.events import EventBus


def _registry_for(
    llm_client: Any, template_name: str, event_bus: EventBus
) -> Any:
    """ModuleEntry.build_registry 适配：按模板名映射 academic_writer 的模式。

    example.academic_writer._build_registry 以 mode 区分（submodule/detailed），
    ModuleEntry 契约收 template_name——此处按模板名换算 mode。
    """
    mode = "detailed" if template_name == "academic_writer_detailed" else "submodule"
    return _build_registry(llm_client, mode, event_bus)


entry = ModuleEntry(
    name="academic_writer",
    description="灵感式写作 → 学术英语（默认=全文优化，详细=逐段可审计）",
    templates={
        "academic_writer": ACADEMIC_TEMPLATE,
        "academic_writer_detailed": DETAILED_TEMPLATE,
    },
    submodules={"fact_review_loop": FactReviewLoop},
    build_registry=_registry_for,
    default_template="academic_writer",
    review_harness=None,  # 固定流程模板，发布前已验证
)
```

- [ ] **Step 3: 运行确认**

Run: `python -m pytest example/ -q`
Expected: PASS（既有 test_loop.py / test_writer.py，`_build_registry` 改动向后兼容）

- [ ] **Step 4: 提交**

```bash
git add example/academic_writer.py example/modules/
git commit -m "feat: academic_writer 注册 event_bus 透传 + example/modules 模块入口（CLI 发现用）"
```

---

## Task 5: specmodule CLI（cli.py）

**Files:**
- Create: `module_harness/cli.py`
- Test: `module_harness/tests/test_cli.py`（Task 6 写）

- [ ] **Step 1: 实现 cli.py**

创建 `module_harness/cli.py`：

```python
# module_harness/cli.py
"""specmodule CLI — 使用者层面入口（run / status / review）。

用法（无打包，与 ``python -m tickflow`` 一致）::

    python -m module_harness.cli run --module academic_writer --spec-file spec.json
    python -m module_harness.cli run --module academic_writer --spec '{"raw_text": "..."}'
    python -m module_harness.cli status [--run-id xxx] [--json]
    python -m module_harness.cli review [--run-id xxx] [--tick N] [--node xxx] [--failed] [--json]

场景归属：使用者层面（usage scenario）——第二级用户只写 spec/tasklist，
不写 Python。模块按名选择，入口注册由开发者在 ``modules/<name>.py`` 声明。
查询组合逻辑只 import 共享层（module_harness.query），绝不重实现。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm import LLMConfig, create_llm_client
from llm.client import LLMResponse

from .entry import discover_modules
from .events import EventBus
from .module import Module
from .query import (
    ReviewTimeline,
    build_timeline,
    filter_failed,
    filter_node,
    filter_tick,
    timeline_to_dict,
)
from .registry import HarnessRegistry
from .spec import Tasklist
from .status import query_run_status
from .translator import TemplateLoader


class MockLLMClient:
    """--mock 冒烟用：通用假客户端（免 key / 免网络）。

    output_format=json_object 时返回宽松合法 JSON（通过 validator）；text
    时返回占位文本。翻译通道（script 翻译器）不经 LLM，天然可用。
    """

    async def complete(self, **kwargs: Any) -> LLMResponse:
        fmt = kwargs.get("output_format") or {}
        if fmt.get("type") == "json_object":
            content = json.dumps(
                {"result": "mock output", "summary": "mock", "issues": []}
            )
        else:
            content = "mock output"
        return LLMResponse(content=content)


def _preview(value: Any, width: int = 80) -> str:
    """产出预览：JSON 序列化（失败回退 str）→ 单行 → 截断。"""
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width] + "…"


class RunDisplay:
    """三级实时显示（--verbose 1..3），由 runner hooks 驱动。

    L1（默认）：``tick 3  Organize ✓`` 一行；失败节点附加 error + 产出预览
    L2：L1 + 全部节点产出预览（约 80 字符截断）
    L3：完整详情块（tick 分隔线 + 输入摘要 + 完整产出 + error）

    回调均为 sync——tickflow ``_maybe_await`` 自动兼容；异常由 tickflow 吞。
    """

    _STATUS_ICON = {"ok": "✓", "failed": "✗", "aborted": "✗"}

    def __init__(self, verbose: int = 1, stream: Any = None) -> None:
        self.verbose = verbose
        self._out = stream or sys.stdout
        self.firings: list = []  # 全部 NodeState（结束汇总用）

    def hooks(self) -> dict:
        return {"on_tick_start": self._on_tick_start, "on_fire": self._on_fire}

    def _write(self, text: str) -> None:
        print(text, file=self._out)

    def _on_tick_start(self, tick: int, fireable: list[str]) -> None:
        if self.verbose >= 3:
            self._write(f"═══ tick {tick} ═══ fireable: {', '.join(fireable) or '—'}")

    def _on_fire(self, ns: Any) -> None:
        self.firings.append(ns)
        icon = self._STATUS_ICON.get(ns.status, ns.status)
        if self.verbose >= 3:
            self._write(f"── tick {ns.tick}  {ns.node}  [{ns.status}]")
            if ns.inputs:
                self._write(f"    inputs : {_preview(ns.inputs, width=200)}")
            if ns.output is not None:
                self._write(f"    output : {_preview(ns.output, width=2000)}")
            if ns.error:
                self._write(f"    error  : {ns.error}")
            return
        line = f"tick {ns.tick}  {ns.node:<24} {icon}"
        if ns.status != "ok":
            line += f"  error={ns.error}"
            if ns.output is not None:
                line += f"  output={_preview(ns.output)}"
        elif self.verbose >= 2:
            line += f"  output={_preview(ns.output)}"
        self._write(line)


def _resolve_spec(entry: Any, args: argparse.Namespace) -> dict[str, Any]:
    """spec 解析优先级：--spec > --spec-file > entry.default_spec。"""
    if args.spec:
        try:
            data = json.loads(args.spec)
        except json.JSONDecodeError as e:
            raise ValueError(f"--spec 不是合法 JSON: {e}")
        if not isinstance(data, dict):
            raise ValueError("--spec 必须是 JSON 对象")
        return data
    if args.spec_file:
        path = Path(args.spec_file)
        if not path.exists():
            raise ValueError(f"--spec-file 不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"--spec-file 不是合法 JSON: {e}")
        except OSError as e:
            raise ValueError(f"--spec-file 读取失败: {e}")
        if not isinstance(data, dict):
            raise ValueError("--spec-file 内容必须是 JSON 对象")
        return data
    if entry.default_spec is not None:
        return dict(entry.default_spec)
    raise ValueError("缺少 spec——请用 --spec（内联 JSON）或 --spec-file（文件）")


_TYPE_CHECKS = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


def _check_spec_schema(entry: Any, spec: dict[str, Any]) -> None:
    """可选的 spec_schema 校验：{字段: 类型名}，失败列出全部错误。"""
    if not entry.spec_schema:
        return
    errors: list[str] = []
    for field, type_name in entry.spec_schema.items():
        if field not in spec:
            errors.append(f"缺少字段 '{field}'（期望 {type_name}）")
            continue
        check = _TYPE_CHECKS.get(str(type_name))
        if check is not None and not isinstance(spec[field], check):
            errors.append(
                f"字段 '{field}' 应为 {type_name}，实际 {type(spec[field]).__name__}"
            )
    if errors:
        raise ValueError("spec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))


def _build_llm_client(mock: bool) -> Any:
    """--mock 用内置假客户端；否则从环境加载（失败提示 --mock）。"""
    if mock:
        return MockLLMClient()
    try:
        config = LLMConfig.from_env()
    except ValueError as e:
        raise ValueError(
            f"LLM 环境配置失败: {e}\n提示：可加 --mock 免 key 冒烟运行"
        )
    if not config.is_configured:
        raise ValueError(
            "LLM 未配置 API key——请配置 config.json + .env，或加 --mock 冒烟"
        )
    return create_llm_client(config)


def _load_tasklist(path_str: str) -> Tasklist:
    """加载 tasklist JSON 文件（{Tasks, Flow} 结构）。"""
    path = Path(path_str)
    if not path.exists():
        raise ValueError(f"--tasklist 文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"--tasklist 读取失败: {e}")
    return Tasklist.from_json(data)


def _print_available(modules: dict) -> None:
    """打印可用模块列表（模块未找到时）。"""
    if not modules:
        print(
            "modules_dir 中未发现任何模块（modules/<name>.py + entry 声明）",
            file=sys.stderr,
        )
        return
    print("可用模块:", file=sys.stderr)
    for name, entry in modules.items():
        print(f"  {name}: {entry.description}", file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    modules = discover_modules(Path(args.modules_dir))
    entry = modules.get(args.module)
    if entry is None:
        print(
            f"模块 '{args.module}' 未找到（modules_dir={args.modules_dir}）",
            file=sys.stderr,
        )
        _print_available(modules)
        return 1
    if args.tasklist and args.template:
        print("--tasklist 与 --template 互斥——只能二选一", file=sys.stderr)
        return 1
    try:
        spec = _resolve_spec(entry, args)
        _check_spec_schema(entry, spec)
        llm_client = _build_llm_client(args.mock)
        template_name = args.template or entry.default_template
        if template_name is not None and template_name not in entry.templates:
            raise ValueError(
                f"模板 '{template_name}' 未注册——可用: {', '.join(entry.templates)}"
            )
        loader = TemplateLoader()
        for name, data in entry.templates.items():
            loader.register(name, data)
        event_bus = EventBus()
        if entry.build_registry is not None:
            registry = entry.build_registry(llm_client, template_name, event_bus)
        else:
            registry = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)
        display = RunDisplay(args.verbose)
        mod = Module(
            spec=spec,
            template_name=template_name,
            tasklist=_load_tasklist(args.tasklist) if args.tasklist else None,
            llm_client=llm_client,
            event_bus=event_bus,
            template_loader=loader,
            module_id=args.run_id or args.module,
            registry=registry,
            review_harness=entry.review_harness,
            modules=entry.submodules,
            hooks=display.hooks(),
        )
        asyncio.run(mod.run(max_ticks=args.max_ticks))
    except KeyboardInterrupt:
        print(
            f"\n已中断：已执行 {len(display.firings)} 次节点 firing。"
            f"运行数据已落盘 .specmodule/runs/{mod.module_id}/（status/review 可查）",
            file=sys.stderr,
        )
        return 2
    except (ValueError, ModuleNotFoundError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    # 结束汇总
    print(f"\n运行完成: module={entry.name} run_id={mod.module_id}")
    print(f"共 {len(display.firings)} 次节点 firing")
    by_node: dict[str, Any] = {
        ns.node: ns.output for ns in display.firings if ns.status == "ok"
    }
    if by_node:
        print("节点最新输出摘要:")
        for node, out in by_node.items():
            print(f"  {node}: {_preview(out)}")
    return 0


def _latest_run_id() -> str | None:
    """扫描 .specmodule/runs/ 取最新修改的子目录名（status/review 缺省）。"""
    runs = Path.cwd() / ".specmodule" / "runs"
    if not runs.is_dir():
        return None
    try:
        dirs = [d for d in runs.iterdir() if d.is_dir()]
    except OSError:
        return None
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime).name


def _cmd_status(args: argparse.Namespace) -> int:
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    st = query_run_status(run_id)
    if st is None:
        print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(asdict(st), ensure_ascii=False, indent=2))
        return 0
    line = f"模块 {st.module_id}: phase={st.phase}"
    if st.tick is not None:
        line += f" tick={st.tick}"
    print(line)
    if st.status:
        print(f"runner: {st.status}")
    if st.fired:
        print(f"本 tick fired: {', '.join(st.fired)}")
    if st.error:
        print(f"error: {st.error}")
    return 0


def _render_timeline(timeline: ReviewTimeline, show_outputs: bool = False) -> None:
    """按 tick 分组文本时间线（失败节点高亮 + error 详情）。

    ``show_outputs``（--tick/--node 过滤时）对每条 entry 附加产出预览。
    """
    if not timeline.entries:
        print("（空时间线——无节点 firing 记录）")
        return
    ticks: dict[int, list] = {}
    for e in timeline.entries:
        ticks.setdefault(e.tick, []).append(e)
    for tick in sorted(ticks):
        cells = [
            f"{e.node} {RunDisplay._STATUS_ICON.get(e.status, e.status)}"
            for e in ticks[tick]
        ]
        print(f"tick {tick}: " + ", ".join(cells))
        for e in ticks[tick]:
            if e.status != "ok":
                print(f"  ✗ {e.node}: {e.error or '无错误信息'}")
            if show_outputs and e.output is not None:
                print(f"    {e.node} output: {_preview(e.output, width=200)}")
    if timeline.latest_tick is not None:
        print(f"\n最新 tick: {timeline.latest_tick}")


def _cmd_review(args: argparse.Namespace) -> int:
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    timeline = build_timeline(run_id)
    if timeline is None:
        print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    if args.failed:
        timeline = filter_failed(timeline)
    if args.tick is not None:
        timeline = filter_tick(timeline, args.tick)
    if args.node:
        timeline = filter_node(timeline, args.node)
    if args.json:
        print(json.dumps(timeline_to_dict(timeline), ensure_ascii=False, indent=2))
        return 0
    _render_timeline(timeline, show_outputs=args.tick is not None or bool(args.node))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="specmodule",
        description="SpecModule CLI——选择模块、传入 spec/tasklist、观察与审阅运行",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行模块")
    p_run.add_argument("--module", required=True, help="模块名（modules/ 目录中发现）")
    p_run.add_argument(
        "--modules-dir", default="modules",
        help="模块目录（默认 modules/，cwd 相对；未来 init 实例布局即此目录）",
    )
    p_run.add_argument("--spec", help="内联 JSON spec")
    p_run.add_argument("--spec-file", help="spec JSON 文件路径")
    p_run.add_argument("--template", help="模板名（默认 entry.default_template）")
    p_run.add_argument(
        "--tasklist", help="tasklist JSON 文件路径（跳过翻译，与 --template 互斥）"
    )
    p_run.add_argument("--run-id", help="运行目录名（默认模块名）")
    p_run.add_argument(
        "--verbose", type=int, choices=(1, 2, 3), default=1,
        help="实时显示级别：1=tick+节点+状态（默认），2=+产出预览，3=完整详情块",
    )
    p_run.add_argument("--max-ticks", type=int, default=100, help="tick 上限（默认 100）")
    p_run.add_argument("--mock", action="store_true", help="免 key 假 LLM 冒烟（测试/演示）")
    p_run.set_defaults(func=_cmd_run)

    p_status = sub.add_parser("status", help="查询运行状态")
    p_status.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_status.add_argument("--json", action="store_true", help="JSON 输出")
    p_status.set_defaults(func=_cmd_status)

    p_review = sub.add_parser("review", help="审阅历史时间线")
    p_review.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_review.add_argument("--tick", type=int, help="只看指定 tick")
    p_review.add_argument("--node", help="只看指定节点")
    p_review.add_argument("--failed", action="store_true", help="只看失败节点")
    p_review.add_argument("--json", action="store_true", help="JSON 输出")
    p_review.set_defaults(func=_cmd_review)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(
            "\n已中断——运行数据已落盘 .specmodule/runs/（specmodule status/review 可查）",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 冒烟运行（--help）**

Run: `python -m module_harness.cli --help`
Expected: 打印三子命令帮助（argparse 输出后 `sys.exit(main())` 以 0 退出）

- [ ] **Step 3: 提交**

```bash
git add module_harness/cli.py
git commit -m "feat: specmodule CLI（run/status/review）——三级实时显示 + 结束汇总 + 错误处理"
```

---

## Task 6: CLI 测试（test_cli.py）

**Files:**
- Test: `module_harness/tests/test_cli.py`

> 测试模块：`hello.py`（单 script 节点 Greet，自带 default_spec）与 `fail.py`
> （Boom 返回 `Failure(type="llm")`——节点 failed、运行继续，供 --failed 路径）。
> 测试模块写入 tmp_path/modules/，`monkeypatch.chdir(tmp_path)` 隔离 cwd，
> 每次运行落盘 .specmodule/runs/ 于各自 tmp_path。

- [ ] **Step 1: 写测试**

创建 `module_harness/tests/test_cli.py`：

```python
# module_harness/tests/test_cli.py
"""specmodule CLI 测试：run / status / review 命令路径（hello/fail 测试模块）。"""

from __future__ import annotations

import json

import pytest

from module_harness.cli import main

HELLO_PY = '''\
"""hello 测试模块：单 script 节点 Greet（无 LLM 依赖）。"""
from __future__ import annotations

from module_harness.entry import ModuleEntry
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Greet")
    def greet(view):
        return {"greeting": "hello world"}

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
            "Flow": "[Greet]",
        }

    return reg


entry = ModuleEntry(
    name="hello",
    description="hello 测试模块",
    templates={
        "hello": {
            "name": "hello",
            "description": "hello 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            },
        },
    },
    build_registry=_registry_for,
    default_spec={"name": "world"},
    default_template="hello",
    review_harness=None,
)
'''

FAIL_PY = '''\
"""fail 测试模块：单 script 节点 Boom 返回 Failure（type=llm，运行继续）。"""
from __future__ import annotations

from tickflow import Failure
from module_harness.entry import ModuleEntry
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Boom")
    def boom(view):
        return Failure("boom failed", type="llm")

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Boom": {"type": "script", "script": "Boom"}},
            "Flow": "[Boom]",
        }

    return reg


entry = ModuleEntry(
    name="fail",
    description="fail 测试模块",
    templates={
        "fail": {
            "name": "fail",
            "description": "fail 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Boom": {"type": "script", "script": "Boom"}},
                "Flow": "[Boom]",
            },
        },
    },
    build_registry=_registry_for,
    default_template="fail",
    review_harness=None,
)
'''


@pytest.fixture
def modules_dir(tmp_path):
    d = tmp_path / "modules"
    d.mkdir()
    (d / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    (d / "fail.py").write_text(FAIL_PY, encoding="utf-8")
    return d


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(cwd, *argv):
    """run 子命令 + 指向测试模块目录。"""
    return main(["run", *argv, "--modules-dir", str(cwd / "modules")])


class TestRun:
    def test_hello_success(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        out = capsys.readouterr().out
        assert "运行完成" in out
        assert "hello world" in out
        assert (cwd / ".specmodule" / "runs" / "hello" / "run.sqlite").exists()

    def test_default_spec(self, cwd, modules_dir, capsys):
        # 无 --spec/--spec-file，走 entry.default_spec
        assert _run(cwd, "--module", "hello", "--mock") == 0

    def test_module_not_found(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "nope", "--mock") == 1
        assert "未找到" in capsys.readouterr().err

    def test_missing_spec(self, cwd, modules_dir, capsys):
        # fail 无 default_spec 也不传 spec → 报错
        assert _run(cwd, "--module", "fail", "--mock") == 1
        assert "缺少 spec" in capsys.readouterr().err

    def test_tasklist_template_mutually_exclusive(self, cwd, modules_dir, capsys):
        assert _run(
            cwd, "--module", "hello", "--mock",
            "--template", "hello", "--tasklist", "x.json",
        ) == 1
        assert "互斥" in capsys.readouterr().err

    def test_verbose3(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock", "--verbose", "3") == 0
        out = capsys.readouterr().out
        assert "═══" in out
        assert "[ok]" in out

    def test_tasklist_file(self, cwd, modules_dir, capsys):
        tl = cwd / "tl.json"
        tl.write_text(
            json.dumps({
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            }),
            encoding="utf-8",
        )
        assert _run(cwd, "--module", "hello", "--mock", "--tasklist", str(tl)) == 0
        assert "运行完成" in capsys.readouterr().out


class TestStatus:
    def test_status_text(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["status", "--run-id", "hello"]) == 0
        assert "phase=done" in capsys.readouterr().out

    def test_status_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["status", "--run-id", "hello", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["phase"] == "done"

    def test_status_no_run(self, cwd, capsys):
        assert main(["status", "--run-id", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err


class TestReview:
    def test_review_timeline(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "hello"]) == 0
        out = capsys.readouterr().out
        assert "Greet ✓" in out
        assert "最新 tick" in out

    def test_review_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "hello", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["entries"][0]["node"] == "Greet"

    def test_review_failed(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "fail", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "fail", "--failed"]) == 0
        out = capsys.readouterr().out
        assert "Boom ✗" in out
        assert "boom failed" in out

    def test_review_no_run(self, cwd, capsys):
        assert main(["review", "--run-id", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_cli.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'module_harness.cli'`）

- [ ] **Step 3: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_cli.py -q`
Expected: PASS（14 passed）

- [ ] **Step 4: 提交**

```bash
git add module_harness/tests/test_cli.py
git commit -m "test: CLI 测试——run/status/review 命令路径（hello/fail 测试模块）"
```

---

## Task 7: 验收用例 + roadmap 记录

**Files:**
- Create: `example/spec.academic_writer.json`（M1 验收示例 spec）
- Modify: `docs/progress/module-roadmap.md`（Phase 0 子集交付记录 + 后续迭代）

- [ ] **Step 1: 创建 M1 示例 spec**

创建 `example/spec.academic_writer.json`（简短版草稿，验收跑通即可；
正式使用时可将 `raw_text` 替换为 `example/sample_raw_text.txt` 全文）：

```json
{
  "raw_text": "灵感草稿：关于用大模型做代码评审的一些想法。\n我们 propose 一个基于 LLM 的代码评审系统，自动 review pull request。\n我们的方法：用 LLM 分析 diff，生成按 severity 分类的 comments。\n实验：在 200 个 PR 上 accuracy 达到 85%，比规则 baseline 高 15 个百分点。\n总结：LLM-based code review 可行，能减少人工负担。未来工作：repo-level context。"
}
```

- [ ] **Step 2: 更新 roadmap（Phase 0 子集交付）**

`docs/progress/module-roadmap.md` 中 `### Phase 0：CLI 使用者界面 + 历史审阅`
标题下、`**说明**` 之前插入：

```markdown
**✅ 子集已交付（2026-08-10，spec：docs/superpowers/specs/2026-08-10-specmodule-cli-design.md）**：
- `specmodule run` — 按名选模块 + spec/tasklist（终端内联或文件）+ 三级实时显示（tick/节点/状态 → +产出预览 → 完整块）+ `--mock` 冒烟 + `--verbose {1,2,3}`
- `specmodule status` — 复用 `query_run_status`（文本/JSON）
- `specmodule review` — tick 时间线 + `--tick/--node/--failed` 过滤 + `--json`
- 模块入口：`modules/<name>.py` 声明 `ModuleEntry`（`--modules-dir` 默认 `modules/`，兼容未来 init 布局）
- 查询组合逻辑沉淀 `module_harness/query.py`（CLI/MCP/Web 三形态复用）

**后续迭代（本次未做，roadmap 记录）**：截断/暂停续跑（Ctrl+C 保存状态 → `resume`）、`snapshot / rollback` CLI 命令、`visualize`（mermaid 导出）、`init` 脚手架（scripts/harnesses/submodules/modules 分目录实例搭建）。快照/回滚能力本身已就位（Module.snapshot/restore/checkpoint/rollback_to），仅缺 CLI 命令形态。
```

并把 `**实现方向**` 列表中的前 3 项（run/status/review）改为 `- [x]` 前缀，后 2 项
（snapshot/resume/rollback、visualize）保持 `- [ ]`；`**验收用例**` 末尾追加：

```markdown
**2026-08-10 里程碑**：M1 在 CLI 的 `--mock` 冒烟已跑通（run → status → review
全链路）；真实 LLM 验收待补（`--spec-file example/spec.academic_writer.json`）。
Phase 0 完成标志 = 论文优化 module 在 CLI 里跑通（真实 LLM）。
```

- [ ] **Step 3: 全量回归**

Run: `python -m pytest module_harness/tests/ example/ -q`
Expected: PASS（既有 + 新增全部通过）

- [ ] **Step 4: --mock 冒烟验收（仓库根目录 cwd）**

Run: `python -m module_harness.cli run --module academic_writer --mock --spec-file example/spec.academic_writer.json --verbose 2 --modules-dir example/modules`
Expected: 逐节点实时行（tick/节点/状态 + 产出预览）+ `运行完成: module=academic_writer`；
`.specmodule/runs/academic_writer/` 落盘

- [ ] **Step 5: review 验收**

Run: `python -m module_harness.cli review --run-id academic_writer --json`
Expected: 合法 JSON（module_id/latest_tick/entries 结构）

- [ ] **Step 6: 提交**

```bash
git add example/spec.academic_writer.json docs/progress/module-roadmap.md
git commit -m "docs: Phase 0 CLI 子集交付记录 + M1 验收示例 spec（roadmap 后续迭代更新）"
```