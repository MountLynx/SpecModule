# tickflow _edges 窗口化设计文档

> 日期：2026-08-05 | 状态：待确认
> 修改仓库：`../Graph`（tickflow 主仓库）；设计记录归 SpecModule

## 背景

`RunState._edges`（`dict[node, list[(tick, output)]]`）是输入解析的唯一运行时索引——**每次触发追加一条，无上限**。短流程（一次运行几十次触发）无害，但 **loop 场景**（循环节点上万次触发）会让 `_edges` 线性膨胀：

1. **内存无界**：同一节点每轮触发累积一条历史
2. **线性检索**：`resolve()` 的 `latest_before(t)`（`state.py:152-158`）顺序扫描该节点全部历史——历史越长每次解析越慢，而 `resolve()` 是热路径（每节点每 tick 调用）

**问题根源**：`_edges` 存了超出输入解析需要的数据。默认 `latest` 策略只需要"tick < t 的最近一条"（marking 一致读）；只有 `A[k]` index 策略（跨迭代钉住 / audit replays，`ir.py:50`）需要完整历史。

**快照/回滚现状澄清**：tickflow 已有 `snapshot()/restore()/checkpoint(label)/rollback_to(label)`（`runner.py:223-349`），为**全量快照 + backend 落盘**模式（JsonBackend / SqliteBackend），不是历史重放。快照导出当时的 `edges`/`state`/`records`，恢复即重建——因此**裁剪 `_edges` 不影响快照/回滚正确性**（窗口化后的 edges 本身就是 resolve 所需的最小集，且快照内同时含 marking，恢复后可继续运行）。

## 目标与范围

**目标**：loop 场景下 `_edges` 内存 O(节点数) 而非 O(触发次数)，`latest_before(t)` 由 O(n) 降为 O(1)（窗口内 ≤2 条扫描）。

**MVP 包含**：
- `record()` 窗口裁剪：非 index 节点每节点保留最近 **2 条**
- 被 `A[k]` index 读取的 producer 节点保留**全量**历史（语义不变）
- index 节点集由 Runner 构造时**静态扫描 Graph** 注入 RunState（不改 parser/IR）
- `restore()` 后 full_history 重建
- 查询语义调整：`firings_of()`/`last_output()` 为窗口内数据；完整历史走 `audit_log()`
- Graph 仓库测试 + SpecModule 同步 + 文档更新

**不包含**（YAGNI / 后续演进）：
- `_records` 窗口化或移交数据库（见「后续演进」）
- `SqliteBackend` 改造（已存在，功能不变）
- 可配置窗口大小（当前无场景需要 >2）
- 动态裁剪（行为不可预测，违反"无隐式行为"原则）

## 核心机制

### 窗口语义：为什么是 2 条

节点一个 tick 最多触发一次（fireable 集合每 tick 计算）。窗口 `[(t-1, 上一轮), (t, 本轮)]` 是 `latest` 策略的最小安全集：

- `resolve(latest, t)` 取 "tick < t 的最近一条"：本轮 `(t, …)` 在 resolve 之后才写入（Phase A 先解析后执行再记录，`engine.py:155-160`），resolve 时窗口内 `tick < t` 的即上一轮 `(t-1, …)`
- **同 tick 并行**（AND-join）：先触发节点的本轮写入对后触发节点不可见（`tick < t` 过滤）——只留"最后一条"会破坏该语义，故需 2 条
- 第一轮窗口仅 1 条：`tick < t` 查无 → `Missing`（与现状一致）

### RunState 变更（`tickflow/state.py`）

```python
def __init__(
    self,
    keep_records: bool = True,
    full_history_nodes: frozenset[str] = frozenset(),
) -> None:
    ...
    self._full_history = full_history_nodes

def record(self, ns: NodeState) -> None:
    if ns.node in self._full_history:
        self._edges.setdefault(ns.node, []).append((ns.tick, ns.output))
    else:
        entries = self._edges.setdefault(ns.node, [])
        entries.append((ns.tick, ns.output))
        if len(entries) > 2:
            del entries[0]              # 只留最近两条
    self._state[ns.node] = dict(ns.mutable_state)
    if self._keep_records:
        self._records.append(ns)
```

`resolve()` **不改**——窗口内顺序扫描（≤2 条）。

### index 节点集：Runner 静态扫描注入（不改 parser）

```python
# Runner.__init__ / AsyncRunner.__init__（各自构造 RunState 处）
full_history = frozenset(
    producer
    for node in graph.nodes.values()
    for producer, policy in node.inputs.items()
    if policy.kind == "index"
)
self._full_history_nodes = full_history
self.run_state = RunState(keep_records=..., full_history_nodes=full_history)
```

- `A[k]` 的 producer 全量保留，`resolve(index, k)` 语义不变（`entries[k-1]`）
- 被 index 读的节点若本身是 loop 节点——历史仍无界（语义需要，接受）
- 两种 `keep_records` 模式一视同仁：裁剪与审计正交（`keep_records` 只控制 `_records`）

## 语义变化

| 接口 | 裁剪后 | 完整历史 |
|------|--------|----------|
| `resolve()` | 不变（窗口 ≤2 条） | — |
| `firings_of(node)` | 最近两条 | `audit_log()` |
| `last_output(node)` | 最后一条（窗口含它，不变） | `audit_log()` |
| `audit_log()` | 不变（读 `_records`） | 完整 |

职责分工：**`_edges` = 运行时索引（窗口），`_records` = 审计（完整）**。

## 快照/回滚适配

- `to_snapshot_data()` 格式**不变**（edges 导出当前窗口；快照文件随之变小）
- `from_snapshot_data(cls, d, full_history_nodes=frozenset())` 增加参数
- **`restore()` 修复点**：`RunState.from_snapshot_data()` 重建会丢失 full_history——Runner 用 `self._full_history_nodes` 重新注入（`runner.py:241` 处）
- `truncate_after()` 不变（本身即裁剪函数；窗口可能缩到 <2 条，后续 record 自然补齐）
- `remap_graph`（`state.py:272`）不变（remap 后 Runner 重建时重新扫描注入，实现时验证）

## 测试计划（Graph 仓库 `tests/`）

| 测试 | 断言 |
|------|------|
| loop 窗口 | 循环 100 次后 `len(rn.run_state._edges["A"]) <= 2`（私有访问先例见 `test_snapshot.py`） |
| index 窗口外可解析 | A 触发 5 次（有 `A[3]` 边），`resolve(index,3)` 仍返回第 3 次值 |
| 并行污染回归 | AND-join 并行节点读不到同 tick 写入（现有语义 + 窗口断言） |
| snapshot/restore 重放 | 快照→跑完→restore→重放结果与完整运行一致（现有 `test_snapshot.py` 跑通） |
| restore 后 index 可解析 | restore 后 `A[k]` 仍返回正确值（full_history 重建验证） |
| firings_of 窗口 | 触发 5 次后 `firings_of` 返回 2 条 |
| audit_log 完整 | 循环 100 次 `audit_log()` 仍 100 条（keep_records=True） |

既有测试影响面（已排查）：loop 断言走 `audit_log()`（不裁）；`test_snapshot.py::test_restore_truncates_history` 的 `any(t >= snap["tick"])` 断言在窗口下仍成立（窗口内最近两条产生于快照之后，tick 必然更大）；`test_remap.py` 的 `last_output` 为少触发场景。

## 文件变更清单

**Graph 仓库（`../Graph`）**：

| 文件 | 变更 |
|------|------|
| `tickflow/state.py` | `__init__` 加 `full_history_nodes`；`record()` 窗口裁剪；`from_snapshot_data` 加参数；模块 docstring 更新（`_edges` 描述、`firings_of`/`last_output` 窗口语义） |
| `tickflow/runner.py` | `__init__` 扫描注入 + 保存 `self._full_history_nodes`；`restore()` 重新注入 |
| `tickflow/async_runner.py` | 与 runner.py 相同 |
| `tests/test_loop.py` | 追加窗口测试 |
| `tests/test_snapshot.py` | 追加/适配（restore 后 index 可解析等） |
| `README.md` | 检查后如有历史语义描述则更新 |

**SpecModule（同步）**：

| 文件 | 变更 |
|------|------|
| `tickflow/` | 从 Graph 仓库复制同步（排除 `__pycache__`），独立 commit |
| `AGENTS.md` | 架构规则 3 的 `_edges` 描述补充 "windowed" |

## 执行流程

```
Graph 仓库修改（state/runner/async_runner + tests）→ Graph tests 全绿
→ 复制 tickflow/ 目录到 SpecModule/tickflow/
→ SpecModule 回归：python -m pytest module_harness/tests -q --ignore=.../smoke（192 项，覆盖上层语义）
→ SpecModule 提交 tickflow 同步 + AGENTS.md
```

## 后续演进（不进本次）

**`_records` 移交 SqliteBackend**：`keep_records=True` 时 `_records` 仍全留内存——长 loop + 完整审计场景可把审计查库（`audit_log()` 无 backend 时读内存窗口 / 有 backend 时查库，语义分裂需专门设计）。`SqliteBackend` 已存在（`persistence.py:277`，含增量 `save_firings`），热路径 `resolve()` 保持内存窗口（数据库查询比内存扫描慢 2-3 个数量级，且 sqlite3 同步阻塞不适合 async tick 循环）。

## 与已有模块的关系

- **tickflow 内部改动**，`module_harness` 零改动（只透传 `keep_records`）
- `snapshot()` 格式兼容：旧快照（未裁剪历史）可正常 restore（数据更多无妨）；新快照 restore 到旧版本 tickflow 同样兼容（窗口数据是完整数据的子集）
- `InputPolicy`/`A[k]` 语法/`views.py` 行为不变
