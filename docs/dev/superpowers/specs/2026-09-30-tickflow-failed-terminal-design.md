# tickflow FAILED 终态设计 — 饿死不再误报 done

> 日期：2026-09-30
> 状态：设计已确认（brainstorm 三案：上游正改 / Module 层检测 / checker
> 静态——取上游正改），待实施
> 关联：tickflow 上游 clone `C:\Users\xingy\Desktop\开发\Graph`
> （`tickflow/runner.py` / `async_runner.py` / `engine.py`）；
> SpecModule 侧 `module_harness/model/module.py`（_finalize_phase 文案）；
> 发布流程先例：SpecModule commit 7ce896e（tickflow 0.2.0 发布 + 依赖下限）
> 背景：失败语义缺口讨论（join 饿死 → IDLE → Module 映射 "done"）；
> 姊妹 spec：`2026-09-30-validation-retry-design.md`（SpecModule 侧重试，
> 独立发版线）

## 0. 缘起与边界

tickflow 状态机文档承诺了四个终态（runner.py 模块 docstring 状态图）：

```
IDLE --tick--> RUNNING --(no fireable / max_ticks)--> IDLE
            --(infra Failure)--> ABORTED
            --cancel()--> CANCELLED
            --(all failed, no fireable)--> FAILED
```

但 **FAILED 从未被赋值**——枚举声明（runner.py:241）、`_TERMINAL` 成员、
文档图三处俱在，engine.py / runner.py / async_runner.py / cli.py 全库无一
赋值点。实际行为：join 饿死（AND-join 等一个永不来的 True 槽位）→ 第一个
空 tick `if not firings: break` → status 停在 IDLE → SpecModule
`_finalize_phase` 的 else 分支把它映射成 **"done"**。即"图里有工作永远
点不着火"的 run 被报成成功——半册 deck 静默导出的引擎级版本。

事实约束（决定设计形状）：

1. **空 tick 即不动点，判定可靠**。同步屏障语义下（Phase A 全部点火基于
   tick 开始时的槽位，Phase B 写槽只影响下一 tick），fireable 集为空 →
   本 tick 无点火 → marking 不变 → 下一点火 fireable 集相同。空 tick 时
   有 pending 槽位 = 这些工作**永远**点不着，不存在"等等就会来"。
2. **无误报面**。mid-flight 不产生空 tick：producer 写槽与 consumer 点火
   逐 tick 交替，衔接连续；pause 在 tick 前 break（不进状态判定）；cancel
   是独立终态；max_ticks 耗尽时 status 为 RUNNING（映射 truncated，可
   resume）——均不经过空 tick 判定。
3. **FAILED 已是终态成员**。`_TERMINAL` 已含 FAILED（ticking 停止、返回
   []），状态机其余部分零改动。
4. **现网无人踩坑**。ppt_master 用 OR-join 门 + 页节点"永不 Failure"收据
   语义，碰不到饿死路径。本改动是修引擎契约（文档承诺 vs 实现缺失），
   不是修现网故障——不紧急，但嵌入者（含"拆 harness + 收据 script"这类
   未来拓扑）会一头撞上。

非目标：
- 不做"失败即收据"等失败传播语义改造（那是模块层收据模式的领地）；
- 不做 checker 静态饿死检测（运行期失败静态不可判定）；
- 不改 IDLE→done 映射的其余路径（正常熄火仍是 done）。

## 1. 行为定义 — runner.py / async_runner.py

两处 tick 的状态判定（sync 与 async 对偶）由：

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
    self.status = (
        RunStatus.FAILED if self._has_pending() else RunStatus.IDLE
    )
else:
    self.status = RunStatus.RUNNING
```

`_has_pending()`（runner.py:322-326）现成判据：`armed_starts` 非空或任意
槽位 True。判定落在 `_BaseRunner` 子类 tick 内，私有方法不跨 API 边界。

**语义矩阵**：

| 终局 | status | Module 映射 |
|---|---|---|
| 全部工作完成（无 pending） | IDLE | done |
| 有 pending 槽位但无 fireable（饿死） | **FAILED**（新） | aborted |
| infrastructure Failure | ABORTED | aborted |
| cancel() | CANCELLED | cancelled |
| max_ticks 耗尽 | RUNNING | truncated（可 resume） |

## 2. SpecModule 侧适配 — model/module.py

`_finalize_phase` 已有 `FAILED → "aborted"` 分支（module.py:485-487），
error 文案由 `"all nodes failed"`（与实际触发条件不符）改为
`"starved: work pending but nothing fireable"`。未点火节点清单不新增引擎
API：模块层由 run_state 全量 NodeState 推导（任务全集 − 已点火）。

## 3. 发布线（AGENTS.md 规则 1 流程）

1. Graph clone 内改动 + 单测，commit；
2. 版本 bump **0.3.0**（新终态 = 行为新增，minor；`pyproject.toml`）；
3. PyPI 发布 `tickflow-py`；
4. 本仓库 `pip install -U tickflow-py`，依赖下限抬升
   `tickflow-py>=0.3.0,<0.4`（对齐 0.2.0 先例，SpecModule commit 7ce896e）；
5. 本仓库受影响文档注记（历史 plan/design 的 tickflow 版本注记体例，
   参照 b498e79）。

## 4. 测试

**tickflow 侧**（Graph clone 测试套）：

1. 饿死：两 producer AND-join，其一上游返 `Failure(type="llm")` → run 终态
   FAILED（非 IDLE）；sync/async 对偶断言；
2. 正常完成：全链跑通、无 pending → IDLE（确认不误伤 done）；
3. OR-join 波语义：repair 环拓扑（producer 条件到场）不受影响——波间衔接
   无空 tick；
4. pause/cancel/truncated 路径回归：状态判定改动不触碰这些分支；
5. 空 tick break 时序：`run_until_idle` 首个空 tick break 后 status 已是
   FAILED（判定发生在 tick 内、break 读取之前）。

**SpecModule 侧**：

6. 全量 `python -m pytest module_harness/tests/ -q` 绿；
7. ppt_master e2e（mock llm）绿——验证 OR-join 波语义与收据模式零回归；
8. `_finalize_phase` 新文案断言（构造 FAILED runner 喂入）。
