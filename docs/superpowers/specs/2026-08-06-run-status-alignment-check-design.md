# 运行状态查询 + 对齐检查 harness 设计文档

> 日期：2026-08-06 | 状态：已确认，待实现
> 对应 roadmap：#7 运行状态查询、#2 对齐检查

## 概述

两个独立功能，一次实现：

1. **运行状态查询（#7）**——其他程序/进程在 Module 运行期间查询当前运行状态。通过磁盘通道（`status.json` + SQLite 快照）实现跨进程监控，查询方无需持有 Module 对象。
2. **对齐检查（#2）**——内置 `align_check` harness 节点，模板设计者自行插入 flow，判断当前产出是否偏离 spec 目标。复用通用输入 token 注入机制，框架不额外调度。

## 设计原则

- **不做新数据容器**：查询数据唯一真相源是 tickflow `RunState` + 既有持久化通道
- **显式无魔法**：token 注入走 tasklist 声明的 inputs，与现有 `{spec.xxx}` 机制同族；缺失占位符保留字面量，不隐藏问题
- **零 tickflow 修改**：全部实现位于 module_harness 层；查询侧复用 `SqliteBackend` 公开 API，不手写 SQL
- **最小开销**：对齐检查不插入即不执行；status.json 每次运行约 5-6 次原子写（9 个阶段中典型路径经历 idle → translating/reviewing → building → ready → running → done/aborted/cancelled），不随 tick 增长

---

## 第一部分：运行状态查询（#7）

### 使用场景

```
Module 进程（写者）                    监控进程（读者）
┌──────────────────────┐            ┌──────────────────────┐
│ Module.run()         │            │ query_run_status(id) │
│  ├─ 阶段切换 → 写     │──status.json──▶  ├─ 读阶段          │
│ │   status.json      │            │  └─ 读最新快照（可选）  │
│  └─ 每 tick → 写      │──run.sqlite──▶  （SqliteBackend）  │
│     run.sqlite       │            └──────────────────────┘
└──────────────────────┘
```

- 写者：Module 进程，唯一写者（每个 `module_id` 一个 DB）
- 读者：任意多个进程，轮询或一次性查询，只读
- **并发安全**：WAL 模式（`SqliteBackend` 构造时设置）下单写者 + 多读者读写互不阻塞；实测 500 次写对撞读取 0 次 `database is locked`

### Module 生命周期阶段

| phase | 含义 | 写入时机 |
|-------|------|----------|
| `idle` | 构造完成，未开始 | `__init__` 参数校验通过后 |
| `translating` | 模板通道：LLM 翻译 spec → tasklist | `_build_runner_async` 开始（无 tasklist 分支） |
| `reviewing` | tasklist 通道：一致性审核中 | `_build_runner_async` 开始（有 tasklist 分支） |
| `building` | 构建 graph + runner | 翻译/审核完成后 |
| `ready` | runner 构建完成，待运行 | `_build_runner_async` 返回前 |
| `running` | 执行中 | `run()` 开始 |
| `done` | 正常结束（IDLE） | `run()` 结束后，读 runner.status |
| `aborted` | 基础设施失败中止 | 同上（ABORTED） |
| `cancelled` | 取消 | 同上（CANCELLED） |

> **手动 tick 场景**：用户 `build_runner()` 后自行驱动 tick 时，Module 层不感知 tick 推进，phase 停在 `ready`；tick 级信息（RUNNING/IDLE）由查询侧从 SQLite 快照读取，两者并存不冲突。

### `status.json` 约定

路径：`<cwd>/.specmodule/runs/<module_id>/status.json`（与 `_persist_dir` 同约定）。

```json
{
  "module_id": "mod_x",
  "phase": "running",
  "error": null,
  "updated_at": 1722900000.123
}
```

- **原子写**：先写 `status.json.tmp`，再 `os.replace()`——读者永远读不到半写状态
- `updated_at` 用 `time.time()` 墙钟（跨进程可比；事件流里的 `time.monotonic()` 是进程内的，不可跨进程比较）
- **status_file 独立开关（默认 True）**：与 persist 正交——`status_file=False` 时不写盘（零残留）；`persist=False + status_file=True` 时只写 status.json 不写 DB，查询侧 phase 仍可查、tick 级字段降级。快速模式（`persist=False + status_file=False`）完全零残留。阶段级写入每次运行仅 ~5 次小写，不随 tick 增长

### 查询 API（新文件 `module_harness/status.py`）

```python
@dataclass
class ModuleStatus:
    module_id: str
    phase: str                       # idle/translating/reviewing/building/ready/running/done/aborted/cancelled
    status: str | None               # tickflow RunStatus（"RUNNING"/"IDLE"/...；无 DB 时为 None）
    tick: int | None                 # 最新快照 tick（无 DB 时为 None）
    fireable: list[str]
    outputs: dict[str, Any]          # node → 最新输出（来自快照 edges 窗口）
    node_states: dict[str, dict]
    error: str | None
    updated_at: float

def query_run_status(module_id: str, base_dir: Path | None = None) -> ModuleStatus | None
```

行为：

1. `base_dir` 默认 `Path.cwd()`；`status.json` 不存在 → 返回 `None`（调用方可 `while (st := query_run_status(id)) is None: sleep(...)` 轮询）
2. 读 `status.json` 填 phase/error/updated_at
3. `run.sqlite` 存在 → `SqliteBackend(db).latest_tick()` + `load_snapshot()` 读**最新快照**，填 status/tick/fireable/outputs/node_states；`SqliteBackend` 每次查询新建/关闭，不持有长连接
4. 无 DB（快速模式）→ tick 级字段为 None/空
5. **容错**：`status.json` JSON 损坏 → 返回 None 并 log warning；`sqlite3.OperationalError` → 降级返回 phase-only；监控方绝不被 DB 锁搞崩

**范围边界**：本项只查"当前状态"（status.json + 最新快照）；完整历史（outputs_history / audit_timeline / node_events）归 #6 数据暴露 SDK，不在本设计内。

### 并发边界（实测结论）

- WAL 模式下读不阻塞写、写不阻塞读；读者只看到已提交数据（快照语义正确）
- 每 tick 的 `save_firings` 与 `save_snapshot` 是两次独立 commit，读者可能看到快照滞后一拍（监控可接受）
- **唯一冲突风险是双写者**：同一 `module_id` 的 Module 并发运行会写同一 DB。默认 `module_id` 为 uuid 天然避免；文档注明"同一 module_id 不可并发"

---

## 第二部分：对齐检查 harness（#2）

### 通用输入 token（graph_builder 扩展）

`task.inputs` 新增三个常量 token，**任何 harness 可用**（与 `{spec.xxx}` 同机制：field 名任意，token 决定值）：

| token | 解析为（注册时） | 实现位置 |
|-------|----------------|----------|
| `{spec}` | `json.dumps(spec.to_dict(), ensure_ascii=False)` | `_register_harness` 的 spec_inputs 构建 |
| `{tasklist}` | `json.dumps({"Tasks": ..., "Flow": ...}, ensure_ascii=False)` | 同上 |
| `{node}` | 当前节点 key（"当前位置"） | 同上 |

变更点：

1. `TasklistTranslator.build()` 将 tasklist 的 Tasks+Flow 字典传入 `_register_body(key, task, spec_dict, tasklist_dict)`（签名扩展）
2. `_register_harness` 中识别三个 token → 存入 `spec_inputs`（常量，不走运行时输入解析）
3. `build()` 第 5 步 wiring 循环与 `input_aliases` 构建同步跳过这些 token（不注册为图输入/别名）
4. `{spec}` 但 build 未传 spec → 空 dict JSON（显式可见）

### 内置 `align_check` harness（新文件 `align.py`，镜像 `consistency.py` 模式）

```python
ALIGN_CHECK_CONFIG = HarnessConfig(
    name="align_check",
    prompt_core=(
        "你是对齐检查器。判断当前节点产出是否偏离 spec 目标。\n"
        "spec: {spec}\ntasklist: {tasklist}\n当前位置: {node}\n"
        "结合前置节点输出判断，输出 JSON："
        '{"aligned": true/false, "suggestions": "..."}'
    ),
    output_format=OutputFormat(type="json_object"),
    temperature=0.1,
)

def register_align_check_harness(reg: HarnessRegistry, name: str = "align_check") -> None
```

- 纳入 `register_builtin_harnesses()`，`BUILTIN_HARNESS_NAMES` 增加 `"align_check"`
- **无专用事件/审核器**：普通 harness 节点，走标准 harness 事件流，结果即节点输出（`json_object` 自动解析为 dict），下游 guard/script 可分支——符合"框架不额外调度、不插入即不执行"

### 模板用法（roadmap 示例完整版）

```json
{
  "C": {
    "type": "harness",
    "harness": "align_check",
    "inputs": {
      "spec": "{spec}", "tasklist": "{tasklist}", "node": "{node}",
      "output_a": "A", "output_b": "B"
    },
    "prompt": "节点 A 输出：{output_a}\n节点 B 输出：{output_b}\n判断以上输出是否偏离 spec 目标。"
  },
  "Flow": "A --> B --> C"
}
```

---

## 错误处理与边界汇总

| 场景 | 行为 |
|------|------|
| status.json 不存在 | `query_run_status` 返回 None |
| status.json JSON 损坏 | 返回 None + log warning |
| DB 锁/读失败 | 降级返回 phase-only |
| persist=False（status_file=True） | 只写 status.json：phase 可查，tick 级字段降级 None/空 |
| status_file=False | 零残留：不写 status.json（与 persist=False 组合为快速模式） |
| 同一 module_id 并发 | 文档禁止（双写者冲突） |
| `{spec}` 未传 spec | 空 dict JSON |
| prompt 占位符缺值 | 保留字面量 `{spec}`（渲染器现有行为） |
| align_check 未注册即引用 | TasklistValidator 现有报错路径 |

## 测试计划

- `test_run_status.py`（新）：status.json 写入时机与内容、原子写、`query_run_status` 全字段、无 DB 降级、None 返回、损坏 JSON 容错、DB 读失败降级
- `test_graph_builder.py`：三 token 解析为 spec_inputs、不进入图输入/别名
- `test_align.py`（新）：注册、配置形状、端到端（mock LLM 返回 `{"aligned":...}` → 节点输出 dict、prompt 含 spec/tasklist/位置）
- 既有测试全量回归（`python -m pytest module_harness/tests/ -q`）

## 文档更新

- `docs/progress/module-roadmap.md`：标记 #7、#2 完成（15 → 17/19），更新实现顺序说明
- `AGENTS.md` 如有必要补充 .specmodule/runs 约定引用
