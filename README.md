# SpecModule

可审计、可调试、可完全掌控的 LLM 使用框架。

将 LLM 调用拆分为可组合的 Petri 网节点，每个节点是最小执行单元——翻译、审查、shell 命令、Python 函数。节点通过有向边连接（支持 AND/OR 汇合、循环），引擎以同步步进执行，所有状态集中记录。快照、暂停、回退对单个 tick 粒度都是低开销的。

## 架构

```
SpecModule/
├── tickflow/              # Petri 网工作流引擎（独立子项目）
│   ├── engine.py          #   纯函数 tick 引擎
│   ├── runner.py          #   Runner / AsyncRunner
│   ├── state.py           #   RunState — 唯一真相源
│   └── ...
├── llm/                   # LLM 客户端（Anthropic + OpenAI 兼容）
│   ├── client.py
│   └── config.py
├── module_harness/        # Module 上层抽象
│   ├── events.py          #   EventBus + 类型化事件
│   ├── config.py          #   HarnessConfig
│   ├── harness.py         #   Harness 类（LLM 调用节点）
│   ├── registry.py        #   HarnessRegistry（harness / script / command 注册）
│   ├── command.py         #   Command 节点（shell 子进程）
│   ├── prompt.py          #   三层 prompt 渲染
│   ├── outputfmt.py       #   输出格式校验 + 自动提取
│   ├── spec.py            #   Spec, Tasklist, TasklistTemplate 数据模型
│   ├── translator.py      #   spec → tasklist 翻译 + 校验 + 模板加载
│   ├── graph_builder.py   #   tasklist → tickflow Graph
│   ├── module.py          #   Module 编排器
│   └── templates/         #   内置任务模板
└── docs/                  # 设计文档、规范、路线图
```

## 快速开始

```python
from module_harness import Module, HarnessRegistry, HarnessConfig, EventBus
from module_harness import TemplateLoader, OutputFormat
from llm import create_llm_client, LLMConfig

# 1. 准备 LLM 客户端
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

reg.harness("spec_to_tasklist", HarnessConfig(
    prompt_core="你是一个流程设计器。根据 spec 生成 tasklist JSON。",
))

@reg.script("format_output")
def format_output(view):
    data = view.A.value
    return {"result": data["translation"].strip()}

# 3. 加载模板
loader = TemplateLoader()
loader.load_builtins()

# 4. 运行
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
```

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

### 事件系统

EventBus 提供两层事件——流程级（tickflow hooks：`on_fire`、`on_tick_end`）和节点内部事件（EventBus：prompt 渲染、token 流、命令执行、校验结果）。消费者按需订阅。

### 命名空间隔离

多个 Module 可在同一进程中共存，body 以 `{module_id}:{key}` 前缀隔离注册。

## 当前状态

12 项核心功能已实现。完整进度与路线图见 [module-roadmap.md](docs/superpowers/progress/module-roadmap.md)。

## 开发原则

- **tickflow 零修改** — 所有 module 层功能通过 `Registry` 子类扩展
- **完全掌控** — 无隐式行为，promptmode 选错直接 KeyError，框架不兜底
- **审计即设计** — 所有状态记录在 RunState 中，快照与回滚是内置能力
- **YAGNI** — 每项功能有明确使用场景才加入

## 许可证

MIT
