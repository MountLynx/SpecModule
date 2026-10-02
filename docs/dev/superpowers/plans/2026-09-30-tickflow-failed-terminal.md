# tickflow FAILED 终态落地 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 tickflow 的 FAILED 终态真正落地——饿死 run（有 pending 工作但无可激发节点）空 tick 后判 `FAILED`（原停在 `IDLE`，被 SpecModule 误报成 `done`），并完成 0.3.0 发布 + SpecModule 侧适配。

**Architecture:** 引擎改动只有两处 tick 状态判定（`runner.py` sync 与 `async_runner.py` async 对偶）：空 tick 时用现成的 `_has_pending()`（armed_starts 非空或任意槽位 True）区分 `FAILED` / `IDLE`。同步屏障语义下空 tick 即不动点，判定可靠无误报面。SpecModule 侧只改 `_finalize_phase` 的 FAILED 分支文案并附未点火节点清单（模块层从 run_state 推导，不新增引擎 API）。随后按 0.2.0 发布先例（Graph clone 改动 → bump 0.3.0 → PyPI → 依赖下限抬升 → 文档注记）走完发布线。

**Tech Stack:** Python 3.10+，pytest（Graph 侧 sync 测试 + `asyncio.run` 包 async；SpecModule 侧 pytest-asyncio），setuptools build + twine（PyPI 发布）。

**关联 spec:** `docs/dev/superpowers/specs/2026-09-30-tickflow-failed-terminal-design.md`

**两个仓库，注意 cwd：**
- **Graph clone**（tickflow 上游）：`C:\Users\xingy\Desktop\开发\Graph`——Task 1–5
- **SpecModule**：`C:\Users\xingy\Desktop\开发\SpecModule`——Task 6–7

**基线（写计划时实测）：** Graph 全量 `python -m pytest tests/ -q` → **278 passed**；两仓库 git 工作区干净；`pip show tickflow-py` → 0.2.0；SpecModule 版本 0.4.0、依赖 `tickflow-py>=0.2.0,<0.3`。Graph 的 `dist/` 已 gitignore，构建产物不需提交。

**关键事实（实现者必读，决定测试写法）：**
1. `FAILED` 已是 `_TERMINAL` 成员（runner.py:251）：ticking 停止、`is_terminal()` 为真、`restore()` 把终态快照重置回 IDLE（可 resume）、`cancel()` 对终态是 no-op——这些现有语义全部不变、全部要测。
2. **失败上游写 False ≠ pending**：body 返回 `Failure(type="llm")` 时所有出边写 `False`，下游被跳过、槽位非 True——`_has_pending()` 为假，run 照旧 `IDLE`（`tests/test_failure.py::test_llm_failure_writes_false_downstream` 是现成回归锚）。只有 **True 槽位或未触发 start 搁浅**（如 AND-join 等一个永不来的 True）才判 FAILED。
3. 饿死测试图能过 `strict_deadlock` 静态检查：checker 只识别 XOR-splitter 互斥分支模式，两 producer AND-join 动态饿死不可静态判定。
4. `run_until_idle` 的空 tick break（runner.py:660 `if not firings: break`）发生在 tick 内状态判定**之后**——break 读到的 status 已是 FAILED，无需改 run_until_idle。
5. SpecModule script 经 `reg.script()` 包裹后是**单参 `view`** 的 view-mode body（registry.py:108），E1/E2 值模式校验不适用——两 producer 无 inputs 的节点合法。
6. SpecModule 端未点火清单推导用 `runner.run_state.firings_of(n)` 真值判断（空 = 从未点火；持久后端读 DB 全史、NullBackend 读内存窗口，两模式都可靠。**不要用 `last_output`**——已点火但输出为 None 的节点会误判为未点火；也**不要用 `audit_log()`**——`keep_records=False` 时返回空表）。

---

## Task 1: FAILED 判定落地（Graph clone，sync + async 对偶）

**Files:**
- Create: `C:\Users\xingy\Desktop\开发\Graph\tests\test_failed_terminal.py`
- Modify: `C:\Users\xingy\Desktop\开发\Graph\tickflow\runner.py:634-639`（`Runner.tick` 状态判定）
- Modify: `C:\Users\xingy\Desktop\开发\Graph\tickflow\async_runner.py:221-226`（`AsyncRunner.tick` 状态判定）

- [ ] **Step 1: 写失败测试**（cwd：Graph clone）

创建 `tests/test_failed_terminal.py`：

```python
"""Tests for the FAILED terminal state: a starved run (pending work, nothing
fireable) must end FAILED, not IDLE. Spec: SpecModule
2026-09-30-tickflow-failed-terminal-design.md."""
from __future__ import annotations

import asyncio

import pytest

from tickflow import parse, Runner, Registry, RunStatus, Failure
from tickflow.async_runner import AsyncRunner


def _reg():
    r = Registry()

    @r.body("ok")
    def _ok(v):
        return "ok"

    @r.body("fail_llm")
    def _fllm(v):
        return Failure("bad output", type="llm")

    @r.body("bump")
    def _bump(v):
        v.state["n"] = v.state.get("n", 0) + 1
        return v.state["n"]

    r.guard("lt3", lambda out: out < 3)
    return r


def _starved_graph(r):
    # A ok -> slot (C,A)=True; B llm-fail -> slot (C,B)=False.
    # AND-join C waits forever for (C,B): pending but nothing fireable.
    return parse(
        "[A]-->C\n[B]-->C\nC.join: AND\nA.body: ok\nB.body: fail_llm",
        registry=r,
    )


def test_starved_sync_ends_failed():
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.run_until_idle(max_ticks=10)
    assert rn.status == RunStatus.FAILED
    assert rn.is_terminal()
    assert not rn.is_idle()
    # FAILED 已在 _TERMINAL：后续 tick 空操作。
    before = len(rn.audit_log())
    rn.tick()
    assert len(rn.audit_log()) == before


def test_starved_status_set_inside_tick_before_break():
    # 状态判定发生在 tick 内、run_until_idle 空 tick break 读取之前：
    # 首个空 tick 返回 [] 时 status 已是 FAILED。
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.tick()                   # tick 0: A ok + B fail_llm（双双点火）
    assert rn.status == RunStatus.RUNNING
    firings = rn.tick()         # tick 1: 空 tick
    assert firings == []
    assert rn.status == RunStatus.FAILED


def test_starved_async_ends_failed():
    r = _reg()
    rn = AsyncRunner(_starved_graph(r), r)
    asyncio.run(rn.run_until_idle(max_ticks=10))
    assert rn.status == RunStatus.FAILED
    assert rn.is_terminal()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_failed_terminal.py -v`
Expected: **3 FAILED** —— `assert RunStatus.IDLE == RunStatus.FAILED`（旧实现空 tick 恒判 IDLE）。若报的是别的错（如 parse 错误），先修测试图再继续。

- [ ] **Step 3: 改 `Runner.tick` 状态判定**（`tickflow/runner.py:634-639`）

现文：

```python
        if aborted:
            self.status = RunStatus.ABORTED
        elif not firings:
            self.status = RunStatus.IDLE
        else:
            self.status = RunStatus.RUNNING
```

改为：

```python
        if aborted:
            self.status = RunStatus.ABORTED
        elif not firings:
            # Empty tick is a fixpoint (sync barrier semantics): pending work
            # with nothing fireable can never fire -> starved terminal.
            self.status = (
                RunStatus.FAILED if self._has_pending() else RunStatus.IDLE
            )
        else:
            self.status = RunStatus.RUNNING
```

- [ ] **Step 4: 跑 sync 子集确认通过**

Run: `python -m pytest tests/test_failed_terminal.py -v -k "not async"`
Expected: 2 passed（`test_starved_async_ends_failed` 仍 FAILED——async 未改）。

- [ ] **Step 5: 改 `AsyncRunner.tick` 状态判定**（`tickflow/async_runner.py:221-226`）

现文：

```python
        if aborted:
            self.status = RunStatus.ABORTED
        elif not firings:
            self.status = RunStatus.IDLE
        else:
            self.status = RunStatus.RUNNING
```

改为：

```python
        if aborted:
            self.status = RunStatus.ABORTED
        elif not firings:
            # Mirror of Runner.tick: empty-tick fixpoint — pending work with
            # nothing fireable is the starved FAILED terminal.
            self.status = (
                RunStatus.FAILED if self._has_pending() else RunStatus.IDLE
            )
        else:
            self.status = RunStatus.RUNNING
```

- [ ] **Step 6: 跑新测试文件确认全绿**

Run: `python -m pytest tests/test_failed_terminal.py -v`
Expected: **3 passed**。

- [ ] **Step 7: 全量回归**

Run: `python -m pytest tests/ -q`
Expected: **281 passed**（基线 278 + 新 3）。若有既有测试翻红：它是把旧误报语义（饿死 → IDLE）钉死的测试——对照上面"关键事实 2"判定是改测试还是实现有误报；拿不准就停下来报告，不要硬改。

- [ ] **Step 8: Commit**（cwd：Graph clone）

```bash
git add tickflow/runner.py tickflow/async_runner.py tests/test_failed_terminal.py
git commit -m "feat(runner): FAILED 终态落地——饿死空 tick（有 pending 无可激发）判 FAILED（sync+async）"
```

---

## Task 2: 不误伤回归护栏（Graph clone）

**Files:**
- Modify: `C:\Users\xingy\Desktop\开发\Graph\tests\test_failed_terminal.py`（文件末尾追加）

- [ ] **Step 1: 追加回归测试**（追加到 `tests/test_failed_terminal.py` 末尾）

```python
# --- 不误伤面：语义矩阵其余各行不受状态判定改动影响 -----------------------


def test_full_completion_still_idle():
    # 全部工作完成（无 pending）→ IDLE（Module 层照旧映射 done）。
    r = _reg()
    g = parse("[A]-->B\nA.body: ok\nB.body: ok", registry=r)
    rn = Runner(g, r)
    rn.run_until_idle(max_ticks=10)
    assert rn.status == RunStatus.IDLE
    assert not rn._has_pending()
    assert rn.is_terminal()          # IDLE 且无事可做仍是终局


def test_or_join_loop_exit_stays_idle():
    # OR-join repair 环（producer 条件到场，波间衔接无空 tick）：守卫关闭
    # 循环边后所有槽位 False → 空 tick → IDLE 而非 FAILED。
    r = _reg()
    g = parse(
        "[P]-->W\nP.body: ok\nW.body: bump\nW--|lt3|-->W\nW.join: OR",
        registry=r,
    )
    rn = Runner(g, r)
    rn.run_until_idle(max_ticks=50)
    assert rn.status == RunStatus.IDLE
    w_outputs = [f.output for f in rn.audit_log() if f.node == "W"]
    assert w_outputs == [1, 2, 3]


def test_or_join_loop_exit_stays_idle_async():
    r = _reg()
    g = parse(
        "[P]-->W\nP.body: ok\nW.body: bump\nW--|lt3|-->W\nW.join: OR",
        registry=r,
    )
    rn = AsyncRunner(g, r)
    asyncio.run(rn.run_until_idle(max_ticks=50))
    assert rn.status == RunStatus.IDLE


def test_failed_upstream_false_slot_still_idle():
    # 失败上游写 False ≠ pending：下游被跳过，照旧 IDLE
    #（与 tests/test_failure.py::test_llm_failure_writes_false_downstream 互为锚）。
    r = _reg()
    g = parse("[A]-->B\nA.body: fail_llm\nB.body: ok", registry=r)
    rn = Runner(g, r)
    rn.run_until_idle(max_ticks=10)
    assert rn.status == RunStatus.IDLE


def test_max_ticks_cutoff_stays_running():
    # max_ticks 耗尽停在 RUNNING（Module 层映射 truncated，可 resume），
    # 不经过空 tick 判定——不得变成 FAILED/IDLE。
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.run_until_idle(max_ticks=1)     # tick 0 点火 A、B 后即达上限
    assert rn.status == RunStatus.RUNNING


def test_pause_breaks_before_status_judgement():
    # pause 在 tick 前 break，不进状态判定。
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.run_until_idle(max_ticks=10, pause_at={1})
    assert rn.status == RunStatus.RUNNING


def test_cancel_before_run_and_after_failed():
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.cancel("user")
    assert rn.status == RunStatus.CANCELLED
    assert rn.cancel_reason == "user"
    rn2 = Runner(_starved_graph(r), r)
    rn2.run_until_idle(max_ticks=10)
    assert rn2.status == RunStatus.FAILED
    rn2.cancel("late")
    assert rn2.status == RunStatus.FAILED   # 终态 cancel 是 no-op


def test_reset_from_failed_back_to_idle():
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.run_until_idle(max_ticks=10)
    assert rn.status == RunStatus.FAILED
    rn.reset()
    assert rn.status == RunStatus.IDLE


def test_restore_failed_snapshot_resumes_as_idle():
    # restore() 把终态快照重置回 IDLE（既有 resume 语义），快照本身带 failed。
    r = _reg()
    rn = Runner(_starved_graph(r), r)
    rn.run_until_idle(max_ticks=10)
    snap = rn.snapshot()
    assert snap["status"] == "failed"
    rn.restore(snap)
    assert rn.status == RunStatus.IDLE
```

- [ ] **Step 2: 跑本文件确认全绿**

Run: `python -m pytest tests/test_failed_terminal.py -v`
Expected: **12 passed**（Task 1 的 3 + 本任务 9）。本任务是钉行为：任何一条翻红说明 Task 1 的实现误伤了不误伤面——回查实现，不要改测试。

- [ ] **Step 3: 全量回归**

Run: `python -m pytest tests/ -q`
Expected: **290 passed**（281 + 9）。

- [ ] **Step 4: Commit**

```bash
git add tests/test_failed_terminal.py
git commit -m "test(runner): FAILED 终态不误伤回归护栏（完成 IDLE / OR-join 波 / truncated / pause / cancel / reset / restore）"
```

---

## Task 3: 文档对齐（Graph clone：docstrings + README×2 + change.md）

**Files:**
- Modify: `C:\Users\xingy\Desktop\开发\Graph\tickflow\runner.py:13-18`（模块 docstring 状态图）及 `:232-241`（`RunStatus` docstring）
- Modify: `C:\Users\xingy\Desktop\开发\Graph\README.md:313-316`（Important 引注）及 `:322-328`（RunStatus 表）
- Modify: `C:\Users\xingy\Desktop\开发\Graph\README.zh.md:287-289`（注意引注）及 `:296-301`（状态表）
- Modify: `C:\Users\xingy\Desktop\开发\Graph\docs\change.md`（文末追加归档节）

- [ ] **Step 1: runner.py 模块 docstring 状态图**（`runner.py:13-16` 附近）

现文：

```
    IDLE --tick--> RUNNING --(no fireable / max_ticks)--> IDLE
                --(infra Failure)--> ABORTED
                --cancel()--> CANCELLED
                --(all failed, no fireable)--> FAILED
```

改为：

```
    IDLE --tick--> RUNNING --(empty tick, nothing pending)--> IDLE
                --(infra Failure)--> ABORTED
                --cancel()--> CANCELLED
                --(starved: work pending, nothing fireable)--> FAILED
```

并在其后的 ``is_idle()`` 句（`runner.py:17-18`）末尾追加一句，改为：

```
``is_idle()`` is ``status == IDLE``. ABORTED/CANCELLED/FAILED ticks return
empty and do not advance. (max_ticks exhaustion leaves RUNNING -- the caller
decides whether to resume; run_until_idle just stops.)
```

- [ ] **Step 2: RunStatus 类 docstring 与枚举注释**（`runner.py:232-241`）

现文：

```python
class RunStatus(str, enum.Enum):
    """Lifecycle of a Runner. IDLE means "quiescent, may have work pending but
    nothing fired last tick" -- historically the only state. The terminal
    states (ABORTED/CANCELLED/FAILED) stop further ticking."""

    IDLE = "idle"           # quiescent (nothing fired, or never started)
    RUNNING = "running"     # a tick is in progress (transient, not persisted)
    ABORTED = "aborted"     # an infrastructure Failure occurred; halted
    CANCELLED = "cancelled"  # cancel() was called
    FAILED = "failed"       # all nodes failed and nothing is fireable
```

改为：

```python
class RunStatus(str, enum.Enum):
    """Lifecycle of a Runner. IDLE means "quiescent with nothing pending" --
    an empty tick that still has pending work (armed starts or a True slot)
    is FAILED instead. The terminal states (ABORTED/CANCELLED/FAILED) stop
    further ticking."""

    IDLE = "idle"           # quiescent, nothing pending (done, or never started)
    RUNNING = "running"     # a tick is in progress (transient, not persisted)
    ABORTED = "aborted"     # an infrastructure Failure occurred; halted
    CANCELLED = "cancelled"  # cancel() was called
    FAILED = "failed"       # starved: work pending but nothing fireable
```

- [ ] **Step 3: README.md 两处**

`README.md:313-316` Important 引注，现文：

```
> **Important**: a failed node writes `False` to **all** out-edges —
> guarded edges are not evaluated. To implement controllable routing
> (e.g. retry on failure), have the body return a result dict and let the
> guard inspect the output value, rather than returning `Failure`.
```

改为（句尾追加饿死指引）：

```
> **Important**: a failed node writes `False` to **all** out-edges —
> guarded edges are not evaluated. To implement controllable routing
> (e.g. retry on failure), have the body return a result dict and let the
> guard inspect the output value, rather than returning `Failure`. If the
> skip strands pending work (an AND-join whose other input already
> arrived), the run ends `FAILED` (starved) instead of `IDLE`.
```

`README.md:322-328` RunStatus 表，现文：

```
| Status | Meaning |
|--------|---------|
| `IDLE` | quiescent (nothing fired last tick, or never started) |
| `RUNNING` | a tick fired (transient; becomes IDLE/terminal next) |
| `ABORTED` | an infrastructure `Failure` occurred; halted |
| `CANCELLED` | `cancel()` was called; halted |
| `FAILED` | (reserved) all nodes failed and nothing fireable |
```

改为：

```
| Status | Meaning |
|--------|---------|
| `IDLE` | quiescent with nothing pending (all work done, or never started) |
| `RUNNING` | a tick fired (transient; becomes IDLE/terminal next) |
| `ABORTED` | an infrastructure `Failure` occurred; halted |
| `CANCELLED` | `cancel()` was called; halted |
| `FAILED` | starved: work is pending (a True slot / armed start) but nothing is fireable; halted |
```

- [ ] **Step 4: README.zh.md 两处**

`README.zh.md:287-289` 注意引注，现文：

```
> **注意**：失败节点向**所有**出边写 `False`——守卫不再求值。要实现
> 可控路由（例如失败重试），让 body 返回结果 dict、由守卫检查输出值，
> 而不是返回 `Failure`。
```

改为：

```
> **注意**：失败节点向**所有**出边写 `False`——守卫不再求值。要实现
> 可控路由（例如失败重试），让 body 返回结果 dict、由守卫检查输出值，
> 而不是返回 `Failure`。若跳过导致待做工作搁浅（AND-join 的另一输入已
> 到场），run 以 `FAILED`（饿死）结束而非 `IDLE`。
```

`README.zh.md:296-301` 状态表，现文：

```
| 状态 | 含义 |
|--------|---------|
| `IDLE` | 静默（上个 tick 无激发，或尚未开始） |
| `RUNNING` | 某 tick 有激发（瞬态；下一步变为 IDLE 或终止态） |
| `ABORTED` | 发生了 infrastructure `Failure`；已停止 |
| `CANCELLED` | 调用了 `cancel()`；已停止 |
| `FAILED` | （保留）所有节点失败且无可激发节点 |
```

改为：

```
| 状态 | 含义 |
|--------|---------|
| `IDLE` | 静默且无待做工作（全部完成，或尚未开始） |
| `RUNNING` | 某 tick 有激发（瞬态；下一步变为 IDLE 或终止态） |
| `ABORTED` | 发生了 infrastructure `Failure`；已停止 |
| `CANCELLED` | 调用了 `cancel()`；已停止 |
| `FAILED` | 饿死：有待做工作（True 槽位/未触发的 start）但无可激发节点；已停止 |
```

- [ ] **Step 5: docs/change.md 文末追加归档节**

```markdown
---

## FAILED 终态落地：饿死 run 不再误报 done（0.3.0，2026-09-30）

状态机文档自始承诺四个终态，但 `FAILED` 从未被赋值：AND-join 饿死（等一个
永不来的 True 槽位）→ 空 tick → status 停在 IDLE → 嵌入层（SpecModule
`_finalize_phase`）把 IDLE 映射成 done——"图里有工作永远点不着火"的 run 被
报成成功。

修复：`runner.py` / `async_runner.py` 两处 tick 状态判定，空 tick 时按
`_has_pending()`（armed_starts 非空或任意槽位 True）区分——有 pending →
FAILED（新终态，ticking 停止），无 pending → IDLE（不变）。同步屏障语义下
空 tick 即不动点，无误报面：producer 写槽与 consumer 点火逐 tick 交替，
mid-flight 不产生空 tick；pause 在 tick 前 break；cancel 是独立终态；
max_ticks 耗尽停在 RUNNING（truncated，可 resume）。

嵌入侧：SpecModule `_finalize_phase` 的 FAILED 分支文案改为
"starved: work pending but nothing fireable"，并附未点火节点清单
（任务全集 − 已点火，模块层由 run_state 边历史推导，不新增引擎 API）。
```

- [ ] **Step 6: 快速回归（文档不许碰坏行为）**

Run: `python -m pytest tests/ -q`
Expected: **290 passed**。

- [ ] **Step 7: Commit**

```bash
git add tickflow/runner.py README.md README.zh.md docs/change.md
git commit -m "docs(runner): 状态图/RunStatus/README 对齐 FAILED 终态语义；change.md 归档"
```

---

## Task 4: 版本 bump 0.3.0（Graph clone）

**Files:**
- Modify: `C:\Users\xingy\Desktop\开发\Graph\pyproject.toml:7`

- [ ] **Step 1: bump 版本**

`pyproject.toml` 第 7 行 `version = "0.2.0"` 改为：

```toml
version = "0.3.0"
```

- [ ] **Step 2: 终验**

Run: `python -m pytest tests/ -q`
Expected: **290 passed**。

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "release: 0.3.0 — FAILED 终态（饿死 run 不再误报 done）"
```

---

## Task 5: PyPI 发布 tickflow-py 0.3.0（Graph clone）

**Files:** 无代码改动（`dist/` 已 gitignore，不入库）。

- [ ] **Step 1: 构建**

Run（缺则先 `pip install build twine`）：

```bash
python -m build
```

Expected: `dist/tickflow_py-0.3.0-py3-none-any.whl` 与 `dist/tickflow_py-0.3.0.tar.gz` 生成（`ls dist` 确认）。

- [ ] **Step 2: 上传 PyPI**（⚠️ 外发动作，需已配置 PyPI 凭据）

```bash
twine upload dist/tickflow_py-0.3.0*
```

Expected: twine 输出上传成功（wheel + sdist 两项）。

- [ ] **Step 3: 验证可安装**

Run: `pip index versions tickflow-py`
Expected: 列出 `0.3.0`（PyPI 索引传播可能有分钟级延迟，未出现则稍候重试）。

---

## Task 6: SpecModule 适配（module.py 文案 + 未点火清单 + 测试）

**Files:**
- Modify: `C:\Users\xingy\Desktop\开发\SpecModule\module_harness\model\module.py:485-487`（`_finalize_phase` FAILED 分支）
- Test: `C:\Users\xingy\Desktop\开发\SpecModule\module_harness\tests\test_run_status.py`（`TestModulePhase` 类末尾追加）

- [ ] **Step 0: 升级依赖**（cwd：SpecModule）

```bash
pip install -U tickflow-py
pip show tickflow-py
```

Expected: `Version: 0.3.0`。若仍是 0.2.0，说明 Task 5 未完成——先回 Graph 侧。

- [ ] **Step 1: 写失败测试**（追加到 `TestModulePhase` 类末尾，`test_run_status.py` 文件内最后一个方法之后）

```python
    # --- tickflow 0.3 FAILED 终态：饿死 run 不再误报 done -----------------

    def _starved_tasklist(self):
        # A ok → (C,A)=True；B llm 失败 → (C,B)=False；AND-join C 永不点火。
        # C 是 view-mode script（单参 view），两 producer 无 inputs 合法。
        return Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="a_ok"),
                "B": TaskDefinition(type="script", script="b_fail"),
                "C": TaskDefinition(type="script", script="noop"),
            },
            flow="[A]-->C\n[B]-->C\nC.join: AND",
        )

    def _starved_reg(self, mock_llm):
        return self._script_reg(
            mock_llm,
            a_ok=lambda view: {"ok": True},
            b_fail=lambda view: Failure("bad output", type="llm"),
            noop=lambda view: {"ok": True},
        )

    @pytest.mark.asyncio
    async def test_finalize_phase_failed_starved_copy(self, tmp_path, monkeypatch, mock_llm):
        """构造 FAILED runner 喂入 _finalize_phase → aborted + starved 文案 + 未点火清单。"""
        from tickflow.runner import RunStatus

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._starved_reg(mock_llm),
            tasklist=self._starved_tasklist(),
        )
        runner = await mod._build_runner_async()
        await runner.run_until_idle(max_ticks=10)
        assert runner.status == RunStatus.FAILED
        assert mod._finalize_phase(runner, max_ticks=10) == "aborted"
        st = self._read_status(tmp_path)
        assert st["phase"] == "aborted"
        assert "starved: work pending but nothing fireable" in st["error"]
        assert st["error"].endswith("unfired: C")   # 未点火清单：仅 C

    @pytest.mark.asyncio
    async def test_starved_run_maps_to_aborted(self, tmp_path, monkeypatch, mock_llm):
        """饿死 run 全链路（run() → FAILED → 映射）→ phase=aborted（原误报 done）。"""
        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._starved_reg(mock_llm),
            tasklist=self._starved_tasklist(),
        )
        await mod.run()
        st = self._read_status(tmp_path)
        assert st["phase"] == "aborted"
        assert "starved: work pending but nothing fireable" in st["error"]
        assert st["error"].endswith("unfired: C")   # 未点火清单：仅 C
```

注：`Failure` 已在 `test_run_status.py:19` 导入，`Tasklist` / `TaskDefinition` 已在 `:17` 导入，无需新增 import。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_run_status.py -v -k "starved or failed_starved"`
Expected: **2 failed**，且都是文案断言翻红（实际 error 是旧文案 `"all nodes failed"`）——这是"实现未改"的正确失败态。若 `runner.status == RunStatus.FAILED` 这条先翻红，说明 tickflow 0.3.0 没装上，回 Step 0。

- [ ] **Step 3: 改 `_finalize_phase` FAILED 分支**（`module.py:485-487`）

现文：

```python
        elif runner.status == RunStatus.FAILED:
            self._write_phase("aborted", error="all nodes failed")
            return "aborted"
```

改为：

```python
        elif runner.status == RunStatus.FAILED:
            # tickflow 0.3：FAILED = 饿死（有 pending 槽位/未触发 start 但无
            # 可激发节点）。未点火清单由点火历史推导（firings_of 非空 = 点过
            # 火；勿用 last_output——输出为 None 的已点火节点会误判，也勿用
            # audit_log——keep_records=False 时为空表）。
            fired = {
                n for n in runner.graph.nodes if runner.run_state.firings_of(n)
            }
            unfired = sorted(set(runner.graph.nodes) - fired)
            self._write_phase(
                "aborted",
                error="starved: work pending but nothing fireable; unfired: "
                      + (", ".join(unfired) if unfired else "(none)"),
            )
            return "aborted"
```

- [ ] **Step 4: 跑本文件确认全绿**

Run: `python -m pytest module_harness/tests/test_run_status.py -q`
Expected: 全部 passed（原有用例 + 新 2 条）。

- [ ] **Step 5: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全部 passed，0 failed。若有既有用例翻红：对照"关键事实 2"判定——钉死旧误报（饿死 → done）的用例应更新断言；真实的 done 路径翻红则实现有误报，停下报告。

- [ ] **Step 6: ppt_master e2e（mock llm）**

Run: `python -m pytest example/test_ppt_master_e2e.py -q`
Expected: 全部 passed——OR-join 波语义与收据模式零回归（页节点"永不 Failure"收据语义碰不到饿死路径）。

- [ ] **Step 7: Commit**（cwd：SpecModule）

```bash
git add module_harness/model/module.py module_harness/tests/test_run_status.py
git commit -m "feat(module): FAILED 映射文案 starved + 未点火清单（tickflow 0.3 对齐）"
```

---

## Task 7: SpecModule 发布线收尾（依赖下限 + 版本 + 文档注记）

**Files:**
- Modify: `C:\Users\xingy\Desktop\开发\SpecModule\pyproject.toml:7,22-26`
- Modify: `C:\Users\xingy\Desktop\开发\SpecModule\docs\dev\superpowers\plans\2026-08-06-run-status-alignment-check.md`（标题后插注记）
- Modify: `C:\Users\xingy\Desktop\开发\SpecModule\docs\dev\superpowers\plans\2026-08-06-snapshot-rollback.md`（标题后插注记）
- Modify: `C:\Users\xingy\Desktop\开发\SpecModule\docs\dev\superpowers\specs\2026-08-06-run-status-alignment-check-design.md`（标题后插注记）

- [ ] **Step 1: 依赖下限 + 版本 bump**（对齐 0.2.0 先例 SpecModule commit 7ce896e：release commit 同时收版本与下限）

`pyproject.toml:7` `version = "0.4.0"` 改为：

```toml
version = "0.4.1"
```

（0.4.1 = 修复版：饿死 run 的 phase 由误报 done 改为 aborted，属行为修复。）

`pyproject.toml` dependencies 段，现文：

```toml
dependencies = [
    # 0.2.0 起依赖 tickflow bind 时代 API（Bind/NodeView/GuardView/保留名约定）；
    # 0.1 的 DictView 视图时代不满足
    "tickflow-py>=0.2.0,<0.3",
]
```

改为（整段替换，先例即整段替换）：

```toml
dependencies = [
    # 0.3.0 起依赖 tickflow FAILED 终态（饿死 run 判 FAILED，phase=aborted）；
    # 0.2 的空 tick 恒 IDLE 会把饿死 run 误报成 done
    "tickflow-py>=0.3.0,<0.4",
]
```

- [ ] **Step 2: 受影响历史文档注记**（b498e79 体例：标题行之后空行处插入引用块）

三篇文档的第一行均为 `# ` 标题，在其后插入同一段注记（各文档同文本）：

```markdown
> ⚠️ **tickflow 0.3.0 FAILED 终态注记（2026-09-30）**：tickflow 0.3.0 起空 tick 且有 pending 槽位（未触发 start 或 True 槽位）→ run 终态 FAILED（原为 IDLE）；Module 层 `_finalize_phase` 映射 phase=aborted，error 文案 "starved: work pending but nothing fireable" 并附未点火节点清单。本文编写时饿死 run 仍误报 done/"all nodes failed"，相关段落以当前契约为准（`module_harness/model/module.py`）。
```

三处锚点（均在标题行后）：
1. `docs/dev/superpowers/plans/2026-08-06-run-status-alignment-check.md`（:1161 嵌旧文案代码块）
2. `docs/dev/superpowers/plans/2026-08-06-snapshot-rollback.md`（:1122 嵌旧文案代码块）
3. `docs/dev/superpowers/specs/2026-08-06-run-status-alignment-check-design.md`（phase 表缺 FAILED→aborted 行）

- [ ] **Step 3: Commit（docs 先行，对齐 b498e79 → 7ce896e 顺序）**

```bash
git add docs/dev/superpowers/plans/2026-08-06-run-status-alignment-check.md docs/dev/superpowers/plans/2026-08-06-snapshot-rollback.md docs/dev/superpowers/specs/2026-08-06-run-status-alignment-check-design.md
git commit -m "docs: tickflow 0.3 FAILED 终态注记（run-status / snapshot-rollback 历史 plan/design）"
```

- [ ] **Step 4: Commit（release）**

```bash
git add pyproject.toml
git commit -m "release: 0.4.1 — 依赖下限抬升 tickflow-py>=0.3.0,<0.4（FAILED 终态）"
```

- [ ] **Step 5: 终验**（cwd：SpecModule）

```bash
pip show tickflow-py        # Version: 0.3.0
python -m pytest module_harness/tests/ -q
python -m pytest example/test_ppt_master_e2e.py -q
```

Expected: 全部 passed。完成后两仓库工作区干净，姊妹 spec（validation-retry）的发版线不受影响。

---

## 语义矩阵（验收对照，来自 spec §1）

| 终局 | status | Module 映射 | 钉行为的测试 |
|---|---|---|---|
| 全部工作完成（无 pending） | IDLE | done | `test_full_completion_still_idle`；SpecModule `test_run_writes_done` |
| 有 pending 但无可激发（饿死） | **FAILED**（新） | aborted + starved 文案 | `test_starved_*`；SpecModule `test_starved_run_maps_to_aborted` |
| infrastructure Failure | ABORTED | aborted | 既有 `test_infra_failure_sets_aborted` / `test_run_aborted_phase` |
| cancel() | CANCELLED | cancelled | `test_cancel_before_run_and_after_failed` |
| max_ticks 耗尽 | RUNNING | truncated（可 resume） | `test_max_ticks_cutoff_stays_running`；既有 `test_max_ticks_cutoff_truncated` |
