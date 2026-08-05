# tickflow 双层存储设计文档（内存窗口 + SQLite 全量）

> 日期：2026-08-05 | 状态：待确认
> 前身：`2026-08-05-edges-window-design.md`（纯内存窗口方案，已实现为记录）——本设计是其演进：保留窗口机制，并将历史/审计/快照落盘

## 1. 背景与动机

tickflow 的 `RunState` 是唯一运行时状态容器，三层结构（`tickflow/state.py`）：

| 层 | 内容 | 维护 |
|----|------|------|
| `_edges` | `dict[node, list[(tick, output)]]` 输出历史索引 | 每次触发追加，无上限 |
| `_state` | `dict[node, dict]` 每节点可变状态（body 写入） | 覆盖，每节点一份 |
| `_records` | `list[NodeState]` 完整审计 | `keep_records=True` 时追加，无上限 |

存在三个问题：

**问题 1：`_edges` 无界累积（loop 场景）**。循环节点每轮触发追加一条 `(tick, output)`，上万次触发线性膨胀。

**问题 2：`latest_before(t)` 线性扫描**。`resolve()`（`state.py:140-158`）对 `latest` 策略顺序扫描该节点全部历史找 `tick < t` 的最后一条——历史越长越慢，而 `resolve()` 是热路径（每节点每 tick 调用，`engine.py:162`）。

**问题 3：大 output 全量驻留内存（编程任务等 token 消耗大的场景）**。`_edges` 全量持有每次触发的 output（LLM 产出代码/长文本可能几十 KB~上百 KB），`_records` 全量持有每次触发的完整 NodeState（含 output 与渲染后的 prompt）——一个多节点编程任务轻松累积数百 MB 内存。此问题与 loop 无关，线性流程同样存在。

**判断准则**（本设计的一切决策依据）：

1. 内存只维持**运行必要**的内容，内存**随时清理**
2. 历史、审计、快照等**都落盘**
3. **保证必要功能不受影响**
4. **性能开销优先于速度**（为控制内存可接受 I/O 开销；LLM 场景下 SQLite µs 级查询相对秒级模型延迟可忽略）

## 2. 目标架构：双层存储

```
┌─ 内存层（热，运行必要）─────────────────────────┐
│  _edges：每节点最近 2 条 (tick, value)  ← 窗口    │  resolve(latest) 零 I/O
│  _state：每节点当前可变状态                       │  body 直接使用
└──────────────────────────────────────────────────┘
┌─ SQLite 层（冷，全部落盘）──────────────────────┐
│  firings 表：全量 (node, tick, output, NodeState) │  resolve(index)、
│  snapshots 表：快照 / checkpoints 表             │  audit_log、firings_of、
│                                                  │  回放/恢复按需查库
└──────────────────────────────────────────────────┘
```

**分工原则**：内存只放"本次运行继续走下去必须立刻可读"的数据（窗口两条 + 当前状态）；一旦窗口滚动，旧数据立即从内存释放、写入 SQLite。

## 3. 设计决策

### D1. `_edges` 窗口化：每节点最近 2 条（含 value），通用（不限于 loop）

```python
def record(self, ns: NodeState) -> None:
    entries = self._edges.setdefault(ns.node, [])
    entries.append((ns.tick, ns.output))
    if len(entries) > 2:
        del entries[0]                  # 只留最近两条
    self._state[ns.node] = dict(ns.mutable_state)
    if self._keep_records:
        self._records.append(ns)
    self._persist_firing(ns)            # D3：同步落盘
```

**为什么 2 条是最小安全集**（marking 语义论证）：
- 节点一个 tick 最多触发一次（fireable 集合每 tick 计算）
- `resolve(latest, t)` 需要"tick < t 的最近一条"：窗口 `[(t-1, 上一轮), (t, 本轮)]` 恒满足——本轮写入发生在 resolve 之后（Phase A 先解析后执行再记录，`engine.py:155-160`）
- **同 tick 并行**（AND-join）：先触发节点的本轮写入对后触发节点不可见（`tick < t` 过滤）——只留"最后一条"会破坏该语义，故需 2 条
- 第一轮窗口仅 1 条：`tick < t` 查无 → `Missing`（与现状一致）
- **窗口内必须含 value**：`resolve` 的产物 `Resolved(value, k)` 的 value 是 body 每次执行必需（script 读 `view.A.value`、harness 渲染 `{log}` 占位符）——热路径取值零 I/O

### D2. `resolve()` 分派：latest 走内存窗口，index 走 SQLite

```python
def resolve(self, node: str, kind: str, k: int | None, t: int) -> Any:
    if kind == "index":
        return self._resolve_index_from_backend(node, k)   # D8
    # latest：窗口内扫描（≤2 条，O(1)）
    last: tuple[int, Any] | None = None
    for tk, v in self._edges.get(node, []):
        if tk < t:
            last = (tk, v)
        else:
            break
    return last[1] if last is not None else Missing
```

- `A[k]` index 策略（`ir.py:50`，跨迭代钉住 / audit replays）是**冷查询**：从 SQLite 取该节点第 k 次触发（低频、同一值可被反复读，SQLite 页面缓存友好）
- 内存不再为 index 节点保留全量历史（原方案 A 的 `full_history_nodes` 概念**取消**——统一规则"内存只有窗口，其余落盘"）

### D3. 全量 output 落盘：每次 record 同步写 SQLite

`record()` 末尾调用 `_persist_firing(ns)`：把本次触发的 `(node, tick, output, NodeState)` 写入 firings 表（按 tick 批量，复用现有 `Backend.save_firings` 通道）。窗口滚动释放的内存数据在库中完整可查。

### D4. 审计落盘：`_records` 不再是唯一审计载体

- `keep_records=True`：NodeState 写入 SQLite（落盘）
- **有 backend（默认）**：内存 `_records` **不再全量维护**——每次触发只写库，内存不累积 NodeState（准则 1：内存随时清理；编程任务内存大头在此解决）。`audit_log()` 查库返回全量
- **无 backend（NullBackend）**：内存 `_records` 全量维护（现状行为），`audit_log()` 读内存
- `keep_records` 参数语义不变：控制"是否记录审计"；落盘与否由 backend 决定
- 内存占用（有 backend）：`_edges` 窗口（≤2 条/节点）+ `_state`（1 份/节点）——O(节点数 × 2 × output 大小)，与触发次数无关

### D5. 快照/回滚：格式不变，窗口天然随附

- `snapshot()` 导出 tick + marking + run_state（edges 为当前窗口，天然变小）+ status——**格式不变，向后兼容**
- `restore(snap)`：重建 RunState（窗口 + state）+ `truncate_after(tick-1)`——**恢复后继续运行 resolve(latest) 只需窗口，正确**
- `checkpoint(label)` / `rollback_to(label)`：经 backend 落盘（已有 `persistence.py:91-103`）
- **恢复后 `A[k]` 查库可解析**（firings 表全量在库；checkpoint 与 firings 同 session 同库）

### D6. backend 默认化：tickflow 层默认临时文件，持久化是上层约定

- `Runner`/`AsyncRunner` 构造参数 `backend: Any = None`（已存在）的默认值语义**迁移**：`None`（不传）→ 自动创建临时 `SqliteBackend`（`tempfile` 模式，Runner 生命周期结束自动清理）；可显式传路径持久保留（现有显式传参方式不变）
- **"显式不要落盘"用 `NullBackend()` 表达**——`None` 不再是"无落盘"（API 不变，语义迁移）
- **分层职责**：tickflow 是通用引擎，不知道上层目录约定——默认临时文件（安全、零残留）；持久化到工作目录是 SpecModule 层的默认（D9）
- 理由：准则 2（历史审计落盘）+ 准则 3（audit 功能不受影响）合起来要求"落盘是默认行为"——否则默认路径下 audit 只有窗口，功能受影响
- 现有所有"不传 backend"的调用行为自动变为默认落盘——API 不变，行为增强，无需改调用方

### D7. 无 backend（NullBackend）兼容路径

| 能力 | 有 backend（默认） | 无 backend（显式 NullBackend） |
|------|-------------------|-------------------------------|
| `resolve(latest)` | 内存窗口 | 内存窗口（同） |
| `resolve(index)` | SQLite 全量 | 窗口内可解析；窗口外 → `Missing`（明确降级） |
| `audit_log()` | 查库全量 | 内存 `_records` 全量（现状） |
| `firings_of(node)` | 查库全量 | 窗口（≤2 条） |
| `last_output(node)` | 窗口最后一条 | 窗口最后一条（同） |
| 快照/checkpoint | 落盘 | 内存/不可用（现状：checkpoint 需 backend，已如此） |

### D8. Backend 协议扩展（`tickflow/persistence.py`）

现有 `Backend` 协议（`save_snapshot/load_snapshot/save_firings/list_firings/save_checkpoint/...`）增加冷查询接口：

```python
def firing_at(self, session_id: str, node: str, k: int) -> Any | None:
    """该节点第 k 次触发（1-based）的 output；不存在返回 None。"""

def firings_of(self, session_id: str, node: str) -> list[tuple[int, Any]]:
    """该节点全量 [(tick, output), ...]，按 tick 序。"""
```

- `SqliteBackend`：firings 表按 `(session_id, node, seq)` 建索引实现
- `NullBackend`：`firing_at` 返回 None、`firings_of` 返回 `[]`（配合 D7 降级语义）
- `JsonBackend`：按现有文件结构实现或抛 `NotImplementedError`（实现时定，优先复用 `list_firings`）

### D9. SpecModule 层持久化约定：`.specmodule/runs/<run_id>/` 每任务独立数据库

```
<工作目录>/.specmodule/runs/<run_id>/run.sqlite
```

- **run_id = module_id**（Module 构造时的 `module_id`；SubModule 每次 `run()` 生成的 `{name}_{uuid[:6]}`）——一个任务一次运行一个子目录、一个独立 sqlite 数据库（互不干扰，删一个任务目录即删其全部记录）
- **持久化开关**：`Module(persist=True)`（默认）自动创建上述路径（`mkdir(parents=True)`）；`persist=False` 走 tickflow 默认临时文件（D6），无 `.specmodule` 残留
- **默认持久化的理由**：准则 2（历史审计落盘）+ 项目愿景（docs/SpecModule.md："状态记录以最终实现前端可视化展示与监控为准"）——历史必须跨运行存在，前端/SDK/审计才有数据可查
- 敏感数据考量：默认落盘意味着 LLM 产出（代码、prompt）持久化到工作目录——`persist=False` 即关闭开关；文档明示此默认行为（无隐式行为原则）
- **retention/清理策略（保留最近 N 次、按大小、手动清理）不在本次范围**——随可视化与第二层用户体验优化后续设计（第 9 节）

### D10. `_state` 不变

每节点一份当前状态（不累积），已是"运行必要"的最小形态；其内容（如 harness 渲染的 prompt）随 NodeState 落盘供审计。

### D11. module_harness 小代码改动：`persist` 开关 + 默认持久化

`module_harness` 与 tickflow 运行状态的接触面（已 grep 验证）：仅 `module.py:103`（`AsyncRunner(graph, registry=reg, keep_records=...)` 不传 backend）与 `submodule.py:148`（透传 `keep_records=audit`）——无任何对 `run_state` 内部的直接访问。**功能改动仅一处：新增 `persist` 开关（默认 `True`）**。

**`Module.__init__` 新增参数**：

```python
persist: bool = True
# True（默认）：构造 .specmodule/runs/<run_id>/run.sqlite 持久 backend（D9）
# False：不传 backend——tickflow 默认临时文件，运行结束自动清理
```

`_build_runner_async` 中：

```python
backend = SqliteBackend(_persist_dir(self.module_id)) if self.persist else None
return AsyncRunner(graph, registry=reg, keep_records=self.keep_records, backend=backend)
```

`SubModule` 构造透传 `persist` 到内部 `Module`（`SubModule.run(audit=..., persist=...)` 或构造参数，实现时定）。

**行为变化**（API 新增，默认行为增强，需文档化）：

1. **默认持久化**：每次 `Module.run` 在 `.specmodule/runs/<run_id>/` 生成独立 sqlite（D9），跨运行历史可查
2. **嵌入模式（`SubModule.run(audit=False)` / `keep_records=False`）同样落盘**：`_persist_firing` 与 `keep_records` 正交。**定位更新**：docs/SpecModule.md 的"取消快照状态等开销"演变为"内存不保留 + 落盘"——嵌入模式由此获得完整历史（之前是丢弃），`SubModule.run` docstring 同步更新
3. **临时文件清理可靠性**（persist=False 时）：Windows 上文件句柄/锁须正确释放才能自动清理——实现细节，测试覆盖
4. 快照/回滚：module_harness 当前不使用（roadmap #6 待实现）；持久 backend 使未来 Module 封装 checkpoint/rollback 时落盘开箱可用

## 4. 数据流总览

```
节点触发
  ├─ resolve(latest)  → 内存窗口（≤2 条，O(1)）     ← 热路径，零 I/O
  ├─ resolve(index)   → SQLite firing_at(node, k)    ← 冷查询
  ├─ body 执行        → 用 Resolved(value) / 写 _state
  └─ record(ns)
       ├─ _edges 窗口追加（>2 条即删最旧）
       ├─ _state 覆盖
       ├─ _records 追加（仅无 backend 时；有 backend 不维护内存审计）
       └─ _persist_firing → SQLite（全量 output + NodeState）

查询接口
  ├─ audit_log()      → SQLite（有 backend）/ 内存 _records（无）
  ├─ firings_of()     → SQLite（有）/ 窗口（无）
  ├─ last_output()    → 窗口最后一条
  └─ snapshot/checkpoint → SQLite（已有机制）
```

## 5. 性能分析

| 路径 | 现状 | 本设计 |
|------|------|--------|
| `resolve(latest)` | 无界列表 O(n) | 窗口 ≤2 条 O(1)，零 I/O |
| `resolve(index)` | 内存 O(1) | SQLite 点查 ~µs（低频，可接受） |
| `record()` | 内存追加 | 内存追加 + SQLite 写（按 tick 批量，一次事务） |
| 内存占用 | O(触发数 × output 大小) | **O(节点数 × 2 × output 大小)**——大 output 不累积 |
| `audit_log()` | 内存读 | 显式调用时查库 |

- loop 场景：`resolve` 读"上一轮"——**永远命中窗口**——SQLite 运行时零读取，内存 O(1)
- 编程任务场景：output 仅窗口两条驻留，其余落盘
- 已知限制：`sqlite3` 为同步 API，`AsyncRunner` tick 内写库会阻塞事件循环——按准则 4 接受（LLM 调用秒级延迟主导）；异步批量写列为后续演进

## 6. 测试计划（Graph 仓库 `tests/`）

**新增**：

| 测试 | 断言 |
|------|------|
| loop 窗口 | 循环 100 次后 `len(rn.run_state._edges["A"]) <= 2` |
| 线性流程窗口 | 非 loop 四节点链跑完后各节点 `_edges` ≤ 2 条 |
| 大 output 不驻留 | 每轮触发大字符串 × N 次，内存 `_edges` 仅 2 条（len 断言） |
| latest 正确性（多轮） | 循环节点 `resolve(latest, t)` 各轮取到上一轮值（现有 loop 测试走 `audit_log` 断言，保留） |
| 并行污染回归 | AND-join 并行节点读不到同 tick 写入（窗口下） |
| index 窗口外可解析（有 backend） | A 触发 5 次（有 `A[3]` 边），`resolve(index,3)` 返回第 3 次值 |
| index 降级（NullBackend） | 窗口外 `A[5]` → `Missing` |
| audit 查库全量（有 backend） | 循环 100 次 `audit_log()` 100 条（从 SQLite） |
| firings_of 分派 | 有 backend 全量 / NullBackend 窗口 |
| snapshot/restore 重放 | 快照→跑完→restore→重放结果与完整运行一致（现有 `test_snapshot.py` 跑通） |
| restore 后 index 可解析 | restore 后 `A[k]` 仍返回正确值（firings 在库） |
| 默认 backend 生命周期 | 默认构造 Runner 运行后临时 DB 被清理；显式路径文件保留 |

**既有测试适配**（破坏面已排查）：

| 测试 | 适配 |
|------|------|
| `test_snapshot.py::test_restore_truncates_history` | `any(t >= snap["tick"])` 断言在窗口下仍成立（窗口最近两条产生于快照后）；运行验证 |
| 依赖 `_edges` 全量的断言 | 改为窗口断言或改走 `audit_log()`/`firings_of` |
| 无 backend 的 Runner 测试 | 兼容路径行为不变（D7），`_edges` 断言除外 |
| `test_persistence.py` | Backend 协议新增方法需在 NullBackend/JsonBackend/SqliteBackend 实现 |

**SpecModule 侧测试**（`module_harness/tests/test_module.py` 或新文件追加）：

| 测试 | 断言 |
|------|------|
| persist 默认持久化 | `Module.run` 后 `<cwd>/.specmodule/runs/<module_id>/run.sqlite` 存在且非空 |
| persist=False 无残留 | 运行后无 `.specmodule` 目录生成（临时文件路径，Graph 层测试覆盖清理） |
| SubModule 每任务独立目录 | 两次 `SubModule.run()` 生成两个不同 run_id 目录，互不干扰 |

## 7. 文件变更清单

### Graph 仓库（`C:\Users\xingy\Desktop\开发\Graph`，tickflow 主仓库）

| 文件 | 变更 |
|------|------|
| `tickflow/state.py` | `record()` 窗口化 + `_persist_firing` 调用；`resolve()` index 分派；`audit_log`/`firings_of` backend 分派；模块 docstring 更新（`_edges` 窗口语义、查询接口两套语义） |
| `tickflow/persistence.py` | `Backend` 协议增加 `firing_at`/`firings_of`；`SqliteBackend` 实现（firings 表索引）；`NullBackend` 降级实现；`JsonBackend` 实现或 NotImplemented |
| `tickflow/runner.py` | backend 默认化（临时 SqliteBackend + 生命周期清理）；`_persist_tick` 走新通道 |
| `tickflow/async_runner.py` | 同 runner.py |
| `tickflow/views.py` | 文档注释（如提及历史语义则更新） |
| `README.md` | 运行状态/持久化章节更新（若涉及） |
| `tests/*` | 上节适配 + 新增 |

### SpecModule（`C:\Users\xingy\Desktop\开发\SpecModule`）

| 文件 | 变更 |
|------|------|
| `tickflow/` | 从 Graph 仓库复制同步（排除 `__pycache__`），独立 commit |
| `AGENTS.md` | 架构规则 3 更新：`_edges` 描述补 "windowed (last 2)"、`_records` 补 "audit persisted via backend" |
| `docs/SpecModule.md` | 嵌入模式定位更新："取消快照状态等开销" → "内存不保留 + 落盘（完整历史可查）"；新增 `.specmodule/runs/` 持久化约定说明 |
| `module_harness/module.py` | 新增 `persist: bool = True` 参数 + `_build_runner_async` 构造持久 backend（D9/D11） |
| `module_harness/submodule.py` | 透传 `persist`（构造参数或 `run()` 参数，实现时定）；`audit=False` docstring 补充"仍默认落盘" |
| `module_harness/` | 其余功能代码零改动；`module_harness/tests` 全量回归（192 项非 smoke）+ 上节 persist 测试 |

## 8. 向后兼容

- **快照格式不变**：窗口 edges 是完整 edges 的子集，新旧互读兼容
- **`backend=None` 语义迁移**：不传 backend 的现有调用（含 `Module.run`）行为从"无落盘"变为"默认临时 SqliteBackend 落盘"——API 签名不变、返回结果不变，仅磁盘行为增强；需要"无落盘"的调用显式传 `NullBackend()`
- **显式 NullBackend 路径 = 现状行为 + 窗口化**：唯一公开语义变化是 `firings_of`（窗口化，已确认"裁剪即索引语义"）与 `resolve(index)` 窗口外降级（D7）
- `keep_records` 参数语义不变
- `InputPolicy`/`A[k]` 语法/`views.py` 行为不变（有 backend 时 index 语义完整）

## 9. 已知限制与后续演进

- **`.specmodule/runs/` retention/清理策略**（保留最近 N 次、按大小、手动清理）——随可视化与第二层用户体验优化后续设计（D9）
- **sqlite3 同步阻塞**（async tick 内）：接受；后续可做异步批量写入 / 独立写线程
- **`_records` 内存全量仅存在于无 backend 路径**：若未来强制 backend，可移除该分支
- JsonBackend 冷查询接口实现优先级最低（SQLite 为默认）
- Module 的 backend 精细控制（自定义路径/多 backend 策略）后续随 SDK（roadmap #6）暴露

## 10. 与既有设计文档的关系

`2026-08-05-edges-window-design.md`（方案 A：纯内存窗口）为本设计的前身：其窗口机制（D1）与语义论证全部保留并吸收；`full_history_nodes` 概念被 D2/D8（index 落盘）取代；快照/回滚适配结论（格式不变、restore 重建）继承。
