# tasklist 执行语义：与 tickflow 的集成

写 tasklist 时的心智模型：**tasklist 是声明，执行是 tickflow 图步进**。本文档解释"你写的 `{Tasks, Flow}` 执行时会发生什么"——节点如何映射、边与 join 的语义、常见死锁/无限循环陷阱、快照与回退的粒度。tickflow 引擎自身的完整语义（Petri 网模型、标记、持久化、快照）见 [tickflow 上游 README](https://github.com/MountLynx/tickflow-)（已安装副本：`site-packages/tickflow/README.md`）。

## 心智模型

```
tasklist 声明                          tickflow 执行
━━━━━━━━━━━━━━━━━━━                   ━━━━━━━━━━━━━━━━━━━━━━━
Tasks: {A: ..., B: ...}      ──►      Graph：每个 Task 一个节点
Flow: "[A] --> B"            ──►      边 A→B（普通边，恒 True）
                                         │
TasklistTranslator.build()             Runner 按 tick 步进：
  - 注册 body（命名空间隔离）           每 tick 所有 fireable 节点并行 firing
  - parse Flow → 校验                  节点输出写入历史，驱动下游 join
```

- **每个 Task 是一个 tickflow 节点**；`type` 决定 body 种类：`harness`（LLM 调用）/ `script`（纯 Python）/ `command`（shell 子进程）/ `submodule`（嵌套运行子模块）。
- **body 命名空间隔离**：注册名为 `{module_id}:{key}`（`TasklistTranslator._isolated`），多个 Module 同进程共存互不冲突——tasklist 里永远写裸名 `A`，隔离是框架内部事。
- **每个 tick**：引擎收集 fireable 节点（所有输入满足 join 条件），同步执行一轮。一个节点一次 firing 消耗输入、产生输出，输出作为后续 tick 的输入。
- `Runner` 构造时做静态检查：死锁模式无法解析 → 抛 `DeadlockError`；无 guard 循环 → `UnguardedCycleWarning`。**写错会在运行前暴露，不会静默卡死或跑飞。**

## 节点与边的映射

| tasklist 元素 | tickflow 对应 | 说明 |
|---|---|---|
| `[A]` | `Node.is_start = True` | 起始节点，运行起点。无 `[` 标记时首 token 自动包为 start（`prepare_flow`） |
| `A --> B` | 普通边（`Edge.guard = None`） | 恒 True 数据流边：A 每次成功 firing 都向 B 送输入槽 |
| `B --\|g1\|--> C` | guard 边（`Edge.guard = "g1"`） | 条件边：guard 函数结果决定本轮是否放行（False 则不写槽值） |
| `C.inputs: A, B[2]` | `Node.inputs`（`InputPolicy`） | C 读 A 的最近输出（latest）+ B 的第 2 次输出（1-based） |
| `C.join: OR` | `Node.join` | join 策略覆盖（默认 AND） |
| `C.body: compute_c` | `Node.body` | 绑定已注册 body（graph_builder 自动绑定，手写不需要） |

### inputs 的两种读取策略（InputPolicy）

| 写法 | 语义 | 典型用途 |
|---|---|---|
| `A`（latest） | producer 在 `tick < 当前` 的最近一次 firing 输出 | 默认；链式传递、循环取上一轮结果 |
| `A[2]`（index） | producer 的第 2 次 firing 输出（**1-based，跨 tick 固定定位**） | 跨迭代固定取值、审计重放 |

latest 语义天然避免循环中读到**自己同一 tick 的输出**（只读 `tick < t`）。

## join 语义：AND 与 OR

节点可以有多条入边。**入边产出的输入槽如何汇聚，由 `join` 决定：**

- **AND join（默认）**：所有入边槽都有值（即所有上游都成功到达）才 fireable。
- **OR join（`C.join: OR`）**：任一入边槽有值即可 fireable。

`Failure`（节点体返回 `Failure(msg, type=...)`）的影响：`type="llm"` 时该节点输出视为失败——出边写 `False`，下游 AND-join 自然不 fire（"上游失败，跳过下游"），**运行继续**；`type="infrastructure"`（网络断、鉴权失败）时引擎进入 `ABORTED` 状态，**整个运行停止**。

## 陷阱 1：XOR 分支 + AND join = 死锁

**XOR-splitter** = 有 ≥2 条 **guard** 出边的节点（guard 条件互斥，每次至多一条放行）。若两条互斥分支汇入同一个下游节点，且该节点是 AND join（默认）——两条槽永远不可能同时有值 → **死锁**。

```text
# 错误：g1/g2 互斥，C 永远等不到两个输入
[A] --|g1|--> C
[A] --|g2|--> C

# 修法 1：C 声明 OR join（任一分支到达即可继续）
C.join: OR

# 修法 2：先合并再汇聚（D 用 OR join 收分支，C 只接 D 一条边）
[A] --|g1|--> D
[A] --|g2|--> D
D.join: OR
D --> C
```

引擎的检查器会检测该模式：能自动解决则翻转 join，无法解决时 `Runner` 构造抛 `DeadlockError`。**写分支汇合时主动声明 join，不要依赖检查器猜测。**

## 陷阱 2：无 guard 的循环会无限跑

```text
# 错误：A 反复 firing，无任何出口条件
[A] --> B
B --> A

# 正确：至少一条 guard 边控制退出
[A] --> B
B --> A          # 返回边
B --|done|--> C  # guard：满足 done 条件才走向出口
```

解析器对"无至少一条 guard 边的循环"发出 `UnguardedCycleWarning`。循环内做条件判断的 guard 函数读写 `view` 中节点输出（如 `view["node"].value`）决定放行。

## 陷阱 3：循环里别依赖"同 tick 自读"

循环边 `B --> A` 中，A 的输入读取策略默认 latest（`tick < t`），不会读到本轮自己刚产生的输出——若需要固定轮次的数据，用 `A[k]` 显式定位。

## 快照、回退与审计的粒度

- **tick 粒度**：`Module(persist=True)`（默认）时引擎每 tick 落盘轻量快照，`resume(rollback_to=tick)` 可精确回退到任意 tick 重跑；进程内 `snapshot()` / `restore()` 支持任意分支探索。
- **审计与输入解析分离**：`RunState` 三层——`_edges`（快速输入解析，每节点仅保留最近 2 次 firing 的窗口）、`_state`（每节点可变状态）、`_records`（完整审计，`keep_records` 开关门控，落盘经 backend）。写 tasklist 无需关心分层，只需知道：**`keep_records=False` 时事件照常可达，但历史查询接口（`review` 等）无数据可查**。
- **嵌入模式**（`SubModule` / `persist=False` / `mode="fast"`）：`audit=False` 内存不保留审计但记录仍落盘（除非 `mode="fast"` 全内存零落盘）。

## 校验与错误

`TasklistValidator` 在运行前校验：flow 引用未定义节点、任务未被 flow 引用（孤立节点）、DSL 语法错误。`spec-harness-syntax.md` 的错误处理矩阵列有完整错误场景；运行期错误（`Failure`）与退出码见 `references/cli-usage.md`。
