# specmodule CLI 设计（Phase 0 子集：run / status / review + 实时观察）

> 日期：2026-08-10
> 状态：已批准（brainstorming 流程，2026-08-10）
> 场景归属：**使用者层面（usage scenario）**——第二级用户只写 spec/tasklist，不写 Python

## 背景与目标

roadmap 第二阶段（使用者层面）形态线第一步：CLI。本次交付 roadmap Phase 0 的**子集**：

- `specmodule run` —— 按名选择 module，传入 spec/tasklist（终端内联或文件），终端**实时显示处理过程**（三级 verbose）
- `specmodule status` —— 查询运行状态（复用既有 `query_run_status`）
- `specmodule review` —— 历史审阅：按 tick 分组时间线 + 过滤 + JSON 出口

**明确不做**（后续迭代，记入 roadmap）：截断/暂停续跑、rollback、snapshot、visualize、交互式终端 UI、二次打包分发。

**架构约束**：

1. 查询组合逻辑（review 时间线）沉淀共享层 `module_harness/query.py`——CLI、MCP、Web 三者是确定消费者，CLI 只 import 不实现
2. 未来 `init` 脚手架布局兼容：`scripts/ harnesses/ submodules/ modules/` 分目录，一个 module 一个 py 文件
3. 使用者零 Python：模块按名选择（`--module <名>`），入口注册由开发者完成
4. 不引入打包（无 pyproject），入口 `python -m module_harness.cli`（与 `python -m tickflow` 一致）

## 架构总览

```
module_harness/
  entry.py      # 新增 — ModuleEntry 合约 + discover_modules() 目录发现
  query.py      # 新增 — 共享查询层：review 时间线组合（firings → 分组/去重/过滤/JSON）
  cli.py        # 新增 — specmodule CLI（argparse 子命令 run/status/review）
  module.py     # 小改 — Module 增加可选 runner hooks 透传
example/
  modules/academic_writer.py   # M1 模块入口文件（声明 ModuleEntry，导入 example.academic_writer）
```

**数据流**：

```
run:     CLI → discover(modules/) → 解析 spec/tasklist → 构造 Module(llm_client, event_bus,
        loader, registry, modules, hooks) → asyncio.run() → on_fire 逐 firing 实时打印 → 结束汇总
status:  CLI → query_run_status()（既有）→ 文本/JSON 呈现
review:  CLI → query.build_timeline()（共享层）→ filter_* → 文本/JSON 呈现
```

**职责边界**：

- `query.py` 只做数据组合（分组/去重/过滤/序列化），不碰终端渲染；渲染是 CLI 形态专属
- `cli.py` 只 import 查询层，绝不重实现
- `entry.py` 只做发现与入口声明，不含运行逻辑

## 实时显示的机制选择

**使用 tickflow `AsyncRunner` hooks，而非扩展事件**：

- `on_fire(NodeState)` —— NodeState 自带 tick/node/inputs/output/status/error，**全部三级显示所需数据一次到位**
- `on_tick_start(tick, fireable_names)`、`on_tick_end(tick, firings)` —— tick 边界
- 现有事件（harness 6 + script 3 + command 3）完成类事件**不带产出且 tick=0 硬编码**——不足以支撑产出预览，故不扩展事件（避免改动 harness/script/command 发射点），hooks 是观察通道的单一来源
- 钩子异常 tickflow 已吞（不阻断运行），错误只 log

Hook 签名（tickflow 既有）：

```python
AsyncFireHook = Callable[[NodeState], Awaitable[None]]
TickEndHook   = Callable[[int, list[NodeState]], None]
TickStartHook = Callable[[int, list[str]], None]   # (tick, fireable_node_names)
```

CLI 用 async 回调（`async def on_fire(ns)`）；`_maybe_await` 兼容 sync/async。

### Module hooks 透传（module.py 小改）

`Module.__init__` 新增参数 `hooks: dict | None = None`，键为 `on_tick_start` / `on_fire` / `on_tick_end`，值为回调（async 或 sync）；`_build_runner_async` 构造 runner 后按键注册。注册失败/回调异常不阻断运行（tickflow 已保障）。

## ModuleEntry 合约（entry.py）

```python
@dataclass
class ModuleEntry:
    name: str                              # 模块名（CLI --module 用）
    description: str                       # 展示给使用者的说明
    templates: dict[str, dict]             # {模板名: TasklistTemplate JSON}，注册进 TemplateLoader
    submodules: dict[str, type[SubModule]] = field(default_factory=dict)  # {tasklist 名: SubModule 类}
    build_registry: Callable[[Any, str, EventBus], HarnessRegistry] | None = None
                                           # (llm_client, template_name, event_bus) -> registry
                                           # None 时 CLI 构造默认 HarnessRegistry（仅内置 harness）
    default_spec: dict[str, Any] | None = None   # 无 --spec/--spec-file 时的兜底
    default_template: str | None = None          # 无 --template 时的兜底（须在 templates 中）
    spec_schema: dict[str, str] | None = None    # {字段: 类型名}，可选用 SpecSchema 校验
    review_harness: str | None = "spec_tasklist_review"  # 一致性审核 harness；固定流程可置 None

def discover_modules(modules_dir: Path) -> dict[str, ModuleEntry]:
    """扫描 modules_dir/*.py，导入后收集模块级 `entry` 变量。"""
```

**发现规则**：扫描 `modules_dir/*.py` 并导入；缺 `entry` 变量或非 `ModuleEntry` 类型的文件跳过并 log 警告；同名冲突后者覆盖 + 警告。

**build_registry 契约要点**：收 `event_bus`——模块作者的 registry 构建函数必须把外部 bus 接入（现有的 `EventBus.null()` 需改为接收参数），否则 CLI 收不到事件。

**M1 入口示例**（example/modules/academic_writer.py）：

```python
from module_harness.entry import ModuleEntry
from example.academic_writer import (
    ACADEMIC_TEMPLATE, DETAILED_TEMPLATE, FactReviewLoop, _build_registry,
)

# 注：example.academic_writer._build_registry 需扩展 event_bus 参数——原实现
# 拼接 EventBus.null()，改为接收外部 bus，签名变为 (llm_client, mode, event_bus)。

def _registry_for(llm_client, template_name, event_bus):
    """ModuleEntry.build_registry 适配：按模板名映射现有模式。"""
    mode = "detailed" if template_name == "academic_writer_detailed" else "submodule"
    return _build_registry(llm_client, mode, event_bus)

entry = ModuleEntry(
    name="academic_writer",
    description="灵感式写作 → 学术英语（默认=全文优化，详细=逐段可审计）",
    templates={"academic_writer": ACADEMIC_TEMPLATE,
               "academic_writer_detailed": DETAILED_TEMPLATE},
    submodules={"fact_review_loop": FactReviewLoop},
    build_registry=_build_registry,
    default_template="academic_writer",
    review_harness=None,          # 固定流程模板，发布前已验证
)
```

**default_spec 定位**：主要给"纯模板化、无必需输入的模块"用。M1 的 `raw_text` 是必需输入，CLI 缺 spec 时报错并提示 `--spec` / `--spec-file`，不设默认 spec。

## run 命令

用法：

```
specmodule run --module academic_writer [--modules-dir modules]
               [--template academic_writer | --tasklist tl.json]
               [--spec '{"raw_text":"..."}' | --spec-file spec.json]
               [--run-id xxx] [--verbose {1,2,3}] [--max-ticks 100] [--mock]
```

**参数解析优先级**：

- spec：`--spec`（内联 JSON）> `--spec-file`（文件）> `entry.default_spec` > 报错（提示可用参数）
- 流程：`--tasklist`（文件，跳过翻译直入 graph builder，与 `--template` 互斥——对齐 Module "template/tasklist 二选一"不变量）；默认 `entry.default_template`
- `--run-id` 默认 = 模块名；`.specmodule/runs/<run_id>/` 落盘，同模块多次运行累积（与现有语义一致）
- `--mock`：免 key 冒烟（CLI 内置通用假客户端，测试/演示用）
- `--modules-dir` 默认 `modules/`（cwd 相对），未来 init 实例布局即此目录

**执行流程**：

1. `discover_modules(modules_dir)` → 按名取 entry；未找到 → 打印可用模块列表退出
2. 解析 spec（含 `spec_schema` 校验，失败列出全部错误）
3. `TemplateLoader` 注册 entry.templates；`build_registry(llm_client, template_name, event_bus)` 构建 registry
4. 构造 `Module(..., module_id=run_id, hooks={...})`；`asyncio.run` 走 `_build_runner_async` → （Module 内部注册 hooks）→ `_run_with_phases`
5. 实时显示 + 结束汇总（最终 tick、运行状态、每节点最新输出摘要）

**三级实时显示**（`--verbose` 递增，默认 1）——由 `on_fire(NodeState)` 驱动：

| 级别 | 输出 | 失败节点附加 |
|------|------|-------------|
| L1（默认） | `tick 3  Organize  ✓`（tick+节点+状态） | 换行缩进：error + 产出预览截断 |
| L2 | L1 行 + 产出预览（约 80 字符截断） | 同 L1 |
| L3 | 完整块：tick/节点/状态/输入摘要/完整产出/error | 同 L1 |

- `on_tick_start` 打印 `═══ tick N ═══` 分隔：仅 L3（L1/L2 行内已有 tick 号，避免刷屏）
- 结束汇总：`运行完成: tick=47 status=idle` + 各节点最新输出摘要
- `Ctrl+C`：捕获 `KeyboardInterrupt` → 打印已执行 tick 数与提示（`specmodule status/review` 可查），退出码 2；运行已落盘，本轮不提供续跑

## 共享查询层（query.py）

```python
@dataclass
class ReviewEntry:
    tick: int
    node: str
    status: str            # ok | failed | aborted
    output: Any
    error: str | None

@dataclass
class ReviewTimeline:
    module_id: str
    entries: list[ReviewEntry]          # firings 表顺序，同 (tick, node) 去重 keep-first
    latest_tick: int | None

def build_timeline(module_id, base_dir=None) -> ReviewTimeline | None
def filter_failed(timeline) -> ReviewTimeline
def filter_tick(timeline, tick) -> ReviewTimeline
def filter_node(timeline, node) -> ReviewTimeline
def timeline_to_dict(timeline) -> dict   # {module_id, latest_tick, entries:[...]}
```

- 数据源：`SqliteBackend.list_firings()`；**容错哲学同 `query_run_status`**——DB 读失败返回 None，监控方绝不被 DB 锁搞崩
- 去重语义与 tickflow `audit()` 一致（同 tick 同 node 保留首条，兼容 restore 后重放）
- 无 run 记录（无 run.sqlite / 无模块 inputs 存档）→ `None`，CLI 提示先运行
- **渲染归属**：时间线分组、错误高亮、输出截断等终端呈现逻辑全部在 `cli.py`；`query.py` 只做数据组合与序列化——MCP/Web 复用数据层，各自实现呈现

## status / review 命令

**status**：

```
specmodule status [--run-id xxx] [--json]
```

- 复用既有 `query_run_status()`（跨进程、DB 读失败降级 phase-only）
- 文本：`模块 academic_writer：phase=running tick=23` + 本 tick fired 节点 + error（如有）
- `--json` 输出 `ModuleStatus` 结构化字段

**review**：

```
specmodule review [--run-id xxx] [--tick N] [--node xxx] [--failed] [--json]
```

- 默认：按 tick 分组时间线（`tick 3 [Organize ✓, Loop1 ✗]`），失败节点行高亮 + error 详情
- `--tick N` 只看单 tick 完整详情（每节点输出）
- `--failed` 只看失败节点（定位问题 tick 的核心路径）
- `--node xxx` 只看某节点全部 firing（含 loop 多轮）
- `--json`：`timeline_to_dict` 原样输出（MCP/Web 直接消费同一函数）

## 错误处理

CLI 统一 `SystemExit` + 非零退出码，错误信息走 stderr：

| 场景 | 行为 |
|------|------|
| module 未找到 | 打印可用模块列表，退出码 1 |
| spec 缺失 | 提示 `--spec` / `--spec-file` / 默认，退出码 1 |
| spec 违反 schema | 列出全部错误字段，退出码 1 |
| 模板未找到 / tasklist 校验失败 | `ValueError` 原样呈现（含逐条错误），退出码 1 |
| LLM 环境缺失 | 提示 `.env` / 环境变量与 `--mock`，退出码 1 |
| tasklist/template 同时给 | 二选一提示，退出码 1 |
| status/review 无运行记录 | 提示先运行 `specmodule run`，退出码 1 |
| 运行中 Ctrl+C | 打印已执行 tick 数 + 提示 status/review 可查，退出码 2 |

## 测试（pytest + unittest.mock，module_harness/tests/）

| 文件 | 覆盖 |
|------|------|
| `test_entry.py` | 发现（临时目录多文件/缺 entry/类型错/同名冲突）、ModuleEntry 字段 |
| `test_query.py` | `build_timeline` 分组/去重 keep-first/无 DB 返回 None；`filter_*` 各过滤；`timeline_to_dict` 结构（临时目录 + SqliteBackend 写假 firings） |
| `test_cli_run.py` | cmd 函数级：mock llm_client 跑通三级 verbose 输出；spec 缺失/模板缺失等错误路径退出码与信息 |
| `test_cli_status_review.py` | status 文本/JSON；review 的 --tick/--failed/--node/--json 各形态 |
| `test_module_hooks.py` | Module hooks 透传：注册后 on_fire 收到 NodeState（tick/node/output 正确） |

## 验收用例（roadmap 完成标志：M1 论文优化在 CLI 跑通）

仓库根目录：

```bash
python -m module_harness.cli run --module academic_writer --spec-file spec.json --verbose 2   # 真实 LLM
python -m module_harness.cli review --failed        # 看失败节点
python -m module_harness.cli review --json           # 结构化数据出口
```

## 后续迭代（记入 roadmap，本次不做）

- 截断/暂停续跑（Ctrl+C 保存状态 → `resume`）
- `rollback` / `snapshot` CLI 命令
- `visualize`（mermaid 导出）
- `init` 脚手架（scripts/harnesses/submodules/modules 分目录实例搭建）
- AGENT（MCP/ACP）与 Web 形态——直接消费 `query.py` 共享层