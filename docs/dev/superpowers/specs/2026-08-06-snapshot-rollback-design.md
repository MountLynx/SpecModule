# 快照/回滚 Module 封装 设计文档

> **退役标注（2026-08-06）**：`auto_checkpoints` 表已退役（冗余清理设计 S2）——
> 每 tick 快照由 tickflow `_persist_tick` 直接写入 snapshots 表（最小快照）。
> 本设计的自动检查点/环形保留部分不再适用。

> 日期：2026-08-06 | 状态：已确认，待实现
> 对应 roadmap：#5 快照/回滚 Module 封装

## 概述

为二级用户（写 spec/tasklist 的使用者）提供"监控运行中发现跑偏 → 微调 spec/tasklist → 回退到出错点之前 → 续跑"的完整闭环；同时为一级开发者提供进程内调试用的快照/回退 API。

**核心形态**：跨进程恢复/续跑为主。运行中每 tick 自动存检查点（SQLite，环形保留 20）；用户发现问题后终止运行、微调输入，用新 `Module` 实例 `resume(rollback_to=...)` 从检查点恢复继续。

## 现状核实（roadmap 表述过时点）

tickflow 底层已具备全部原语，roadmap 表述有三处过时：

1. **`remap_graph()` 已存在**（`tickflow/runner.py:384`）——换图 + 移植 marking + `keep_nodes()` 裁剪 + `reset()`。这是"回滚时调整 tasklist 未执行部分"的关键底层，roadmap 未提及。
2. **`rollback_to()` 依赖 backend**——`checkpoint()`/`rollback_to()` 在 `NullBackend` 下直接 `RuntimeError`。fast mode（persist=False）下检查点不可用，roadmap 未写此边界。
3. **snapshot 结构已变**——`Runner.snapshot()` 的 edges/state 嵌套在 `run_state` 键下，roadmap #6 仍按直接访问 `RunState._edges`/`_state` 内部结构描述。

### 底层能力盘点（已核实）

| 能力 | 位置 | 说明 |
|------|------|------|
| `snapshot()` / `restore()` | runner.py:290/302 | 全量状态序列化/恢复（tick、marking、run_state、status、fireable） |
| `to_json()` / `from_json()` | runner.py:337/353 | 完整序列化（含 audit records 重嵌） |
| `checkpoint(label)` / `list_checkpoints()` / `rollback_to(label)` | runner.py:420/426/431 | 命名检查点，**要求 backend + session_id** |
| `remap_graph(new_graph)` | runner.py:384 | 换图移植 marking（`(dst,src)` 键匹配）、`keep_nodes`、`reset()`；结构校验 `_warn_graph_changes` |
| `truncate_after(tick)` | state.py:394 | 窗口/计数/mutable state 从持久化历史重建——**loop 中间迭代回退精确恢复** |
| `fire_counts` 序列化 | state.py:352 | loop 节点第 k 次 firing 的 index 解析跨回退正确 |
| `keep_nodes(nodes)` | state.py:476 | remap 后裁剪已删除节点状态 |

**loop 结论**：快照含 `fire_counts`；`truncate_after` 持久路径按"tick ≤ 回退点、每节点最后一次"重建 `_state`（mutable state 精确恢复）；guard 边 slot 随 marking 恢复。回退到循环中途重跑，`view.state` 从该迭代继续——**Module 层无需为 loop 做特殊处理**。

### backend 检查点 API（已核实）

`save_checkpoint` / `list_checkpoints` / `load_checkpoint` 齐全（Json + Sqlite 两实现），**无 delete_checkpoint**——环形淘汰不能靠删 backend 记录，由 Module 层自管表 + SQL `DELETE` 实现。

## 设计决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 使用场景 | 二级用户监控发现跑偏 → 微调 spec/tasklist → 回退到出错点前续跑；一级开发者调试 harness 提示词 |
| 2 | 回退点形态 | 自动检查点（每 tick 一个）+ 回退 |
| 3 | 微调应用方式 | 新 spec/tasklist 全量重建新图 + `remap_graph` 状态移植 |
| 4 | 已执行节点被改 | 宽松 + 警告（修改不生效，需回退更早） |
| 5 | fast mode 边界 | 自动检查点仅 persist=True；fast mode 回退报清晰错误 |
| 6 | 回退执行侧 | 跨进程恢复/续跑为主（监控进程无法操作运行中进程） |
| 7 | 检查点粒度/保留 | 每 tick 一个，环形保留 20；手动 checkpoint 永久保留 |
| 8 | loop 处理 | 底层已完整支持，无需特殊处理 |
| 9 | 兼容性校验 | 硬错误 2 类 + 警告 2 类 |
| 10 | 存储方案 | Module 层独立 `auto_checkpoints` 表（方案 A），零修改 tickflow |

---

## 架构与组件

### 新文件 `module_harness/checkpoint.py`

**① `AutoCheckpointStore`** — run.sqlite 内 `auto_checkpoints` 表管理，职责单一：

```python
class AutoCheckpointStore:
    """run.sqlite 的 auto_checkpoints 表：自动检查点存取 + 环形保留。

    - 独立 sqlite3 连接打开同一 run.sqlite（WAL 单写者多读者安全）
    - 超出 max_auto 按 created_at DELETE 最旧
    - 写失败仅 log 不阻断（对齐 status.json 容错哲学）
    """

    def __init__(self, module_id: str, max_auto: int = 20,
                 base_dir: Path | None = None) -> None
    def save(self, label: str, snap: dict) -> None      # INSERT OR REPLACE + 环形淘汰
    def load(self, label: str) -> dict | None
    def list(self) -> list[tuple[str, int]]             # (label, tick)，按 tick 升序
    def close(self) -> None
```

表结构：`CREATE TABLE IF NOT EXISTS auto_checkpoints(label TEXT PRIMARY KEY, tick INT, snap TEXT, created_at REAL)`。`snap` 存 `json.dumps(snapshot)`。

**② 兼容性校验** — 纯函数，无状态：

```python
@dataclass
class ResumeCheck:
    hard_errors: list[str]   # 非空则拒绝 resume
    warnings: list[str]

def check_resume_compat(new_tasklist: Tasklist, graph: Graph,
                        executed_nodes: set[str],
                        old_tasklist: Tasklist | None = None) -> ResumeCheck
```

**③ `module_inputs` 存档表** — Module 首次 `run()` 时把翻译后的 tasklist（+ spec）JSON 深拷贝存档到 run.sqlite 的 `module_inputs` 表。用途：兼容性警告 #3 需要"旧 tasklist 定义"做对比；顺带让二级用户跨进程看到"这次 run 用了什么输入"（补齐 roadmap #7 的小盲区）。

---

## 数据流

### 写入路径（自动检查点）

```
Module.run() persist=True
  └─ build runner 后注册 on_tick_end hook
       └─ 每 tick 结束: store.save(f"auto:tick:{t}", runner.snapshot())
```

- 手动 `checkpoint(label)` 透传 `runner.checkpoint()`（backend 表，永久保留）
- fast mode（persist=False）不注册 hook，自动检查点表零写入

### 恢复路径（跨进程续跑，二级用户核心）

```python
m = Module(spec=新spec, tasklist=微调后tasklist, module_id=原id,
           llm_client=..., persist=True)
firings = await m.resume(rollback_to="auto:tick:27")
```

`resume()` 步骤：

1. persist=False → `RuntimeError("resume 需要 persist=True（自动检查点依赖 SQLite backend）")`
2. load 检查点：先查 `auto_checkpoints` 表，未命中再查 backend checkpoints 表 → 都未命中 `KeyError`（附可用检查点列表）
3. 用**当前** spec/tasklist 全量重建新图（走 `_build_runner_async` 既有路径，含 tasklist 校验 + 一致性审核）
4. **兼容性校验**（在构造 runner 之前，硬错误则 raise，不触碰任何 runner 状态）：新图 + 已执行节点集合（从检查点 snapshot 推导：`run_state.edges` 的键）vs 新 tasklist
5. `AsyncRunner(新图, reg, backend=同一 run.sqlite, session_id=module_id)` → `runner.restore(snap)` 恢复 marking/run_state
6. `runner.remap_graph(新图)` — slot 按 `(dst,src)` 键移植已执行边、`keep_nodes` 裁剪、`reset()`
7. 注册自动检查点 hook → `run_until_idle(max_ticks)` 续跑（tick 从检查点继续编号；新自动检查点 INSERT OR REPLACE 自然覆盖旧 label）

**关键事实**：跨进程恢复不需要旧 graph——`restore(snap)` 只恢复 marking/run_state（与 graph 无关），`remap_graph` 的 slot 移植只看 `self.marking.slots`（restore 来的旧标记）+ `new_graph.edges` 的 key 匹配。节点名稳定时已执行边的标记完整移植。唯一失效的是 `_warn_graph_changes` 的 start 校验（old==new 不触发）——由兼容性校验硬错误 #2 补上。

---

## API 形态（Module 扩展）

```python
# 进程内（一级开发者调试，需已构建 runner）
def snapshot(self) -> dict                        # {spec, tasklist, runner_snapshot} 三件套
def restore(self, snap: dict) -> None             # 回滚 runner + 恢复 spec/tasklist 字段
def checkpoint(self, label: str) -> None          # 透传（backend 表，永久）
def rollback_to(self, label: str) -> None         # 进程内回退（透传）
def list_checkpoints(self) -> list[tuple[str, int, str]]  # (label, tick, kind) kind ∈ {"auto","manual"}，合并两表

# 跨进程续跑（二级用户核心）
async def resume(self, rollback_to: str, max_ticks: int = 100) -> list[NodeState]
```

**两类快照职责分离**：
- 自动检查点：只存 tickflow snapshot（每 tick 一个，体积可控）
- `Module.snapshot()` 三件套：进程内全量（含 spec/tasklist 深拷贝），一级开发者调试用

**Module 持有 runner**：`Module` 目前不保存 runner（`build_runner()` 返回后、`run()` 里是局部变量）。本次在 `_build_runner_async` 成功后赋值 `self._runner`，进程内 API（`snapshot`/`restore`/`checkpoint`/`rollback_to`）依赖它；未构建 runner 时调用 → `RuntimeError`（提示先 `build_runner()` 或 `run()`）。

---

## 兼容性校验规则（`check_resume_compat`）

| # | 类别 | 规则 | 处理 |
|---|------|------|------|
| 1 | **硬错误** | 新 task 的 `inputs` 引用的 producer 不在新图节点集合中 | `ResumeError`（图不完整，静态错误） |
| 2 | **硬错误** | 新图中成为 start 且有历史输出的节点（底层 `_warn_graph_changes` 在 remap 时 old==new 检测失效，Module 层补） | `ResumeError`（armed_starts 一次性，永不重跑） |
| 3 | **警告** | 已执行节点在新 tasklist 中被修改（对比 `dataclasses.asdict(task)` 与 `module_inputs` 存档；TaskDefinition 无 to_dict，Spec 才有） | `log.warning`（修改对已执行部分不生效，需回退更早） |
| 4 | **警告** | 新 task 的 `inputs` 引用的 producer 是未执行节点（运行时 resolve 为 `Missing`，prompt 占位符保留字面量） | `log.warning`（可能拿到坏输入） |

**已执行节点集合推导**：检查点 snapshot 的 `run_state.edges` 键集。

**校验时机**：`resume()` 第 4 步，构造 runner 之前。硬错误 → `ResumeError`（含全部错误明细）；警告仅 log。

---

## 错误处理矩阵

| 场景 | 行为 |
|------|------|
| `resume()` 且 persist=False | `RuntimeError("resume 需要 persist=True（自动检查点依赖 SQLite backend）")` |
| rollback_to label 不存在（自动表 + 手动表都查过） | `KeyError`，并列出可用检查点 |
| 自动检查点表写失败（磁盘满等） | 仅 log 警告，不阻断运行（对齐 status.json 容错） |
| 硬错误命中 | `ResumeError` raise，runner 未构建（可安全修复后重试 resume） |
| 运行中自动检查点淘汰 | 静默（环形保留，仅保最近 20） |
| resume 后 tick 号与旧检查点 label 冲突 | 新快照覆盖旧 label（INSERT OR REPLACE），自然合并 |
| fast mode 下调用 `checkpoint()`/`rollback_to()` | 透传 runner 的 `RuntimeError`（底层行为） |

---

## 测试计划

**单元**（`module_harness/tests/test_checkpoint.py`）：
- `AutoCheckpointStore`：save/load/list roundtrip、环形淘汰 20、跨实例打开同一 DB、损坏 JSON 容错
- `check_resume_compat`：4 类校验各自触发/不触发、硬错误优先级、已执行节点集合推导
- `Module.snapshot()/restore()`：三件套 roundtrip、spec/tasklist 深拷贝独立性

**集成**（mock LLM）：
- `resume()` 端到端：跑 5 tick → 停 → 微调 tasklist（改未执行节点 prompt）→ resume 到 `auto:tick:3` → 已执行 3 tick 输出保留、新节点用新 prompt、最终输出正确
- loop 场景：带循环的 tasklist 跑 8 tick → resume 回退到循环中途 → 重跑，`view.state` 从该迭代继续
- fast mode resume → RuntimeError
- 硬错误 → ResumeError，runner 未构建

**回归**：全量 `python -m pytest module_harness/tests/ -q`（含既有 224 项）

---

## 文档更新

- `docs/dev/progress/module-roadmap.md`：标记 #5 完成（17 → 18/19），更新"待实现"与实现顺序说明
- `AGENTS.md`：如有必要补充 auto_checkpoints / module_inputs 表约定
