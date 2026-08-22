# SpecModule

可审计、可调试、可完全掌控的 LLM 使用框架。

将 LLM 调用拆分为可组合的 Petri 网节点，每个节点是最小执行单元——翻译、审查、shell 命令、Python 函数。节点通过有向边连接（支持 AND/OR 汇合、循环），引擎以同步步进执行，所有状态集中记录。**每 tick 落盘轻量快照**，快照、暂停、精确回退（tick 号）都是低开销的。

## 架构

```
SpecModule/
├── tickflow                # Petri 网工作流引擎（外部 pip 依赖 tickflow-py，import 名 tickflow）
├── llm/                    # LLM 客户端（Anthropic + OpenAI 兼容）
│   ├── client.py
│   └── config.py
├── module_harness/        # Module 上层抽象
│   ├── module.py          #   Module 编排器（run/resume/snapshot/rollback）
│   ├── registry.py        #   HarnessRegistry（harness / script / command 注册）
│   ├── harness.py         #   Harness 类（LLM 调用节点，三层 prompt）
│   ├── command.py         #   Command 节点（shell 子进程）
│   ├── prompt.py          #   三层 prompt 渲染
│   ├── outputfmt.py       #   输出格式校验 + 自动提取
│   ├── spec.py            #   Spec, Tasklist, TasklistTemplate 数据模型
│   ├── translator.py      #   spec → tasklist 翻译 + 校验 + 模板加载
│   ├── graph_builder.py   #   tasklist → tickflow Graph
│   ├── consistency.py     #   spec + tasklist 一致性审核
│   ├── align.py           #   对齐检查 harness
│   ├── checkpoint.py      #   运行输入存档 + resume 兼容性校验
│   ├── status.py          #   跨进程运行状态查询
│   ├── submodule.py       #   类式 module 定义 + 打包发布
│   ├── loader.py          #   module 加载 + 依赖校验
│   ├── builtins.py        #   内置 harness 集
│   ├── events.py          #   EventBus + 类型化事件
│   ├── entry.py           #   ModuleEntry 入口合约 + 目录发现
│   ├── scaffold.py        #   init 脚手架生成（单文件 + --as-dir 目录形态）
│   ├── store.py           #   store 共享层（家目录/搜索路径/枚举/安装管理）
│   ├── feed.py            #   零依赖运行 feed（http.server，CLI feed 命令）
│   ├── query.py           #   共享查询层（时间线/检查点，CLI/MCP/Web 复用）
│   ├── cli.py             #   specmodule CLI（18 子命令，argparse 零依赖）
│   ├── templates/         #   内置任务模板
│   └── tests/             #   pytest 测试套件（含真实 LLM smoke）
└── docs/                  # 设计文档、实现计划、路线图
```

## 安装依赖

```bash
# 库：pip 安装（pyproject.toml + console script `specmodule`）
pip install specmodule

# 开发（本仓库）：源码 + 测试依赖
pip install -r requirements.txt
```

| 包 | 用途 | 必需 |
|----|------|------|
| **`specmodule`** | 库本体（PyPI 名；`pyproject.toml` 打包：`llm` + `module_harness` + CLI `specmodule`） | ✅ 必需 |
| **`tickflow-py`** | Petri 网工作流引擎。⚠️ PyPI 包名为 `tickflow-py`，**import 名仍为 `tickflow`**（`import tickflow`，不是 `import tickflow_py`）。上游仓库：https://github.com/MountLynx/tickflow- | ✅ 必需 |
| `anthropic` | Claude 后端（`provider=anthropic` 时） | 按 provider 选装 |
| `openai` | OpenAI 及兼容后端（`provider=openai` / `openai-compatible` 时） | 按 provider 选装 |
| `jsonschema` | `json_schema` 输出格式校验（未安装则跳过 schema 校验，仅保证是 JSON） | 推荐 |
| `pytest` | 测试套件（`python -m pytest module_harness/tests/ -q`） | 仅开发 |

安装后 CLI 即用（`specmodule run/status/review/...`，18 个子命令，见 `docs/cli-usage.md`）；不写 module 的使用者走 store 闭环：`specmodule setup` 配 key → `install` 装模块 → `list`/`run` → `update`/`uninstall`。

## 快速开始

```python
from module_harness import Module, HarnessRegistry, HarnessConfig, EventBus
from module_harness import TemplateLoader, OutputFormat
from llm import create_llm_client, LLMConfig

# 1. 准备 LLM 客户端（LLM_PROVIDER / API key 从环境或 .env 读取）
config = LLMConfig.from_env()
client = create_llm_client(config)
bus = EventBus()

# 2. 注册 harness 和 script
reg = HarnessRegistry(llm_client=client, event_bus=bus)

reg.harness("translate", HarnessConfig(
    prompt_core="将以下文本翻译为中文：{text}",
    output_format=OutputFormat(type="json_object"),
    notdo=["不要添加解释"],
    temperature=0.3,
))

@reg.script("format_output")
def format_output(view):
    data = view.A.value
    return {"result": data["translation"].strip()}

# 3. 加载内置模板（spec only → 翻译通道）
loader = TemplateLoader()
loader.load_builtins()

# 4. 运行（persist=True 时每 tick 落盘轻量快照，可精确回退）
module = Module(
    spec={"source_text": "Hello world", "style": "formal"},
    template_name="translate",
    llm_client=client,
    event_bus=bus,
    template_loader=loader,
)

firings = await module.run()
for f in firings:
    print(f"{f.node}: {f.output}")

# 5. 续跑与回退（跨进程）
await module.resume(rollback_to=3)          # 精确回退到 tick 3 后重跑
module.list_checkpoints()                    # [(tick, fired 节点列表, kind), ...]

# 6. 封装打包：类式定义 → pack 发布 → 加载运行
from module_harness import SubModule, SpecSchema, TaskDefinition, ModuleLoader, script

class Translator(SubModule):
    """带风格选择的翻译 module（类式定义 + spec_schema 输入契约）。"""
    name = "my_translator"
    version = "1.0.0"
    description = "带风格选择的翻译 module"
    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )
    harnesses = [HarnessConfig(
        name="translate",
        prompt_core="翻译：{text}",
        prompt_modes={"formal": "正式", "casual": "随意"},
        output_format=OutputFormat(type="json_object"),
    )]
    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                promptmode="{spec.style}",          # spec 字段驱动 promptmode
                inputs={"text": "{spec.source_text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_output", inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )

    @script("format_output")
    def format_output(view):
        return {"translation": view.A.value["translation"].strip()}

# 直接运行（spec 经 spec_schema 契约校验）
await Translator(llm_client=client).run({"source_text": "Hello", "style": "formal"})

# 打包发布：导出 module.json + harnesses/ + scripts/ + commands/
dist = Translator().pack("dist/my_translator")

# 另一进程/项目加载运行（requires 依赖校验，无需重新定义）
loaded = ModuleLoader().load(dist)
await loaded.run({"source_text": "Hello", "style": "casual"})
```

## 嵌入式使用（宿主项目 import 库）

`pip install specmodule` 后另建项目直接 `import` 库面编程 API，把 SpecModule
当 LLM 工具套件嵌入自己的服务/IDE 插件/Web 后端。最小 demo 见
[`examples/embed_minimal/`](examples/embed_minimal/main.py)（包含 `--mock`
免 key 冒烟，可直接运行验证）：

```bash
pip install specmodule
cd examples/embed_minimal && python main.py --mock
```

```python
from module_harness import (
    EventBus, HarnessConfig, HarnessRegistry, Module,
    TemplateLoader, OutputFormat, register_builtin_harnesses,
)
from llm import LLMConfig, create_llm_client

client = create_llm_client(LLMConfig.from_env())   # .env / 环境变量
bus = EventBus()

reg = HarnessRegistry(llm_client=client, event_bus=bus)
register_builtin_harnesses(reg)                    # spec_to_tasklist 等内置集
reg.harness("translate", HarnessConfig(
    prompt_core="将以下文本翻译为中文：{text}",
    output_format=OutputFormat(type="json_object"),
))

loader = TemplateLoader(); loader.load_builtins()
module = Module(
    spec={"source_text": "Hello world", "style": "formal"},
    template_name="translate",
    llm_client=client, event_bus=bus, registry=reg, template_loader=loader,
    persist=False, status_file=False, keep_records=False,   # 嵌入方零落盘/零残留
)
await module.run()
```

嵌入要点：

- **事件与 records 解耦**（`decouple-embed-events`）——宿主传 `event_bus` 即收
  `OutputValidated` / `HarnessFailed` 等事件做反馈，不拖审计与落盘；不传则静默零开销。
- **零残留可选**——`persist=False` + `status_file=False` + `keep_records=False`
  时嵌入方磁盘上不留任何 `.specmodule/` 产物。
- **内置 harness 显式注册**——翻译/审核/对齐 harness 不走隐式加载，
  宿主对 `HarnessRegistry` 调 `register_builtin_harnesses(reg)` 注册。
- 库面 = `module_harness` 顶层导出（`Module / HarnessRegistry / SubModule /
  Translator / query` 共享层……），导入勿触达内部子模块。

## 核心概念

### 三种节点类型

| 类型 | 用途 | 注册方式 |
|------|------|----------|
| **harness** | LLM 调用 — 三层 prompt、输出校验、流式 token | `reg.harness("name", config)` |
| **script** | 纯 Python 函数 — 处理、计算、IO | `@reg.script("name")` |
| **command** | Shell 命令 — 一行字符串即节点 | `reg.command("name", CommandConfig(...))` |

### spec 与 tasklist

- **spec** — 结构化键值对，描述"想要什么"。无预定义 schema，字段由模板设计者定义。
- **tasklist** — `{Tasks: {A: {...}, B: {...}}, Flow: "A --> B"}`。描述"如何做"，每个 Task 映射为一个 tickflow 节点。
- **两种输入**：① 只传 spec（通过模板翻译为 tasklist）② 传 spec + tasklist（一致性审核后直入 graph builder）。

### 快照与回滚（roadmap #5）

- **每 tick 轻量快照**：persist=True 时由引擎逐 tick 落盘（剥离审计 records，O(节点+边) 恒定大小），任意 tick 可回退
- **精确 tick 号回退**：`resume(tick)` 跨进程续跑，只重跑未执行部分（已执行节点输出保留）；手动检查点 `checkpoint("label")` / `rollback_to("label")` 永久保留
- **`list_checkpoints()`**：显示 `(tick, fired 节点列表, kind)`——tick ↔ 节点轨迹，历史审阅的雏形
- **进程内** `snapshot()` / `restore()` 全量快照，任意分支/回退

### 运行状态查询（roadmap #7）

跨进程查询：`status.json`（阶段机：idle → translating → reviewing → ... → done）+ `run.sqlite` 最新快照（tick 级：status/tick/fireable/fired + 每节点最新输出）。任何进程可查，不依赖 Module 实例。

```python
from module_harness import query_run_status
st = query_run_status("my_module")     # ModuleStatus：phase/tick/fired/outputs/node_states
```

### submodule — 类式 module + 打包发布

```python
from module_harness import SubModule, script, SpecSchema

class Dig(SubModule):
    name = "dig"
    spec_schema = SpecSchema(input={"url": "str"})
    tasklist = Tasklist(tasks={...}, flow="...")

    @script("fetch")
    def fetch(view):
        return {"html": ...}
```

`SubModule` 类式声明（含 `spec_schema` 输入契约）→ `pack()` 导出可发布清单 → `ModuleLoader` 加载（`requires` 依赖校验）。`mode = "fast"` 零落盘运行。

### 一致性审核与对齐检查

- **一致性审核** — 自定义 tasklist 通道默认经内置审核 harness（`spec_tasklist_review`）做 spec↔tasklist 语义一致性 LLM 审核，不通过抛 `ConsistencyError` 阻塞
- **对齐检查** — 内置 `align_check` 节点，对比 spec 目标与产出，输出对齐/偏离分析 + 建议

### 事件系统

EventBus 提供两层事件——流程级（tickflow hooks：`on_fire`、`on_tick_end`）和节点内部事件（EventBus：prompt 渲染、token 流、命令执行、校验结果）。消费者按需订阅。

### 命名空间隔离

多个 Module 可在同一进程中共存，body 以 `{module_id}:{key}` 前缀隔离注册。

## 当前状态

**库核心框架能力已完成**（18 项）；**库自身主线已完成**（2026-08-22）：打包接线
（`pyproject.toml` + `specmodule` CLI 随库分发）、module-user-store 全系列
（store 家目录 / 配置回退链 / 统一枚举 run 打通 / CLI 管理面 setup-install-list-info-
uninstall-publish-update / init 目录形态）、独立线（嵌入式验证 demo + stdlib 可视化
feed）。待做：M2 实践线（store 真实验收）、收口 API 稳定化、生态项目（TUI/MCP/Web）。
完整进度与路线图见 [module-roadmap.md](docs/progress/module-roadmap.md)。

## 开发原则

- **tickflow 零修改（有条件的）** — tickflow 是外部依赖（PyPI 包 `tickflow-py`，import 名 `tickflow`，上游仓库 https://github.com/MountLynx/tickflow-），仓库内无 tickflow 代码。修改前先判断：改动是否有普适性、是否真正有助于优化 tickflow 本身？**没有 → 不碰**（模块层功能一律通过 `Registry` 子类扩展）；**有 → 在上游改**，发布新版 `tickflow-py` 并升级安装版本
- **两级用户定位** — 框架服务两类用户：**开发者用户**（写 module 并发布）与**使用者用户**（只写 spec/tasklist）。边界不硬——开发者也是使用者，使用者也能按需修改。本质是两个使用场景（**开发场景** vs **使用场景**），新功能开发时明确主要为哪个场景服务
- **完全掌控** — 无隐式行为，promptmode 选错直接 KeyError，框架不兜底
- **审计即设计** — 所有状态记录在 RunState 中，快照与回滚是内置能力
- **SDK 先行** — 新功能实现前先设计所需的数据查询接口，消费形态（CLI/agent/Web）只是 SDK 的薄封装
- **YAGNI** — 每项功能有明确使用场景才加入

## 许可证

MIT
