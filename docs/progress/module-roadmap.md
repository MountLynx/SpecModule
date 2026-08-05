# SpecModule 开发进度与路线

> 最后更新：2026-08-05

本文档追踪 SpecModule module 核心功能的开发状态与实现方向。
范围限定：module 内核（harness / script / command / spec / tasklist / submodule / 编排 / 状态数据）。
不包含：前端可视化、外部 agent 交互（MCP / 斜杠指令）、独立进程部署。

## 完成度速览

已实现：**14** / 待实现：**5**

---

## 已实现 ✅

### 执行元件

| 功能 | 实现 | 关键文件 |
|------|------|----------|
| **harness** — LLM 调用节点，三层 prompt（核心/动态/人工注入）、outputformat 校验与自动提取、notdo 否定性约束 | `Harness` + `HarnessConfig` + `PromptRenderer` + `OutputValidator` | `harness.py`, `config.py`, `prompt.py`, `outputfmt.py` |
| **script** — 纯 Python 函数节点，装饰器注册，事件包裹（start/complete/failed） | `@reg.script()` | `registry.py` |
| **command** — Shell 命令节点，subprocess 执行，一行字符串即节点 | `Command` + `CommandConfig` + `reg.command()` | `command.py`, `registry.py` |

### 配置与翻译

| 功能 | 实现 | 关键文件 |
|------|------|----------|
| **spec 数据模型** — 结构化键值对，无预定义 schema | `Spec` | `spec.py` |
| **tasklist 数据模型** — Tasks + Flow 结构化定义 | `Tasklist` + `TaskDefinition` + `TasklistTemplate` | `spec.py` |
| **spec only → tasklist 翻译** — LLM harness 翻译 + script 函数翻译 | `Translator` + `TemplateLoader` | `translator.py` |
| **内置 tasklist 模板** — JSON 文件模板，代码/目录加载 | `TemplateLoader.load_builtins()` | `translator.py`, `templates/builtin/` |
| **tasklist → tickflow Graph** — 每个 Task 映射为 node body，命名空间隔离 | `TasklistTranslator` | `graph_builder.py` |
| **spec + 自定义 tasklist 输入** — tasklist 参数直入 graph builder，跳过翻译（与 template_name 互斥） | `Module` | `module.py` |
| **一致性审核** — 独立审核 harness `spec_tasklist_review`，spec+tasklist 语义一致性 LLM 审核，不通过抛 `ConsistencyError` 阻塞 | `ConsistencyReviewer` + `register_review_harness` | `consistency.py`, `events.py` |

### 编排与基础设施

| 功能 | 实现 | 关键文件 |
|------|------|----------|
| **Module 编排器** — spec + template → 翻译 → graph → runner | `Module.build_runner()` / `Module.run()` | `module.py` |
| **命名空间隔离** — body 注册名 `{module_id}:{key}`，同进程多 module 不冲突 | `TasklistTranslator` | `graph_builder.py` |
| **EventBus** — 类型安全的事件发布订阅，harness 9 种 + script 3 种 + command 3 种事件 | `EventBus` | `events.py` |
| **tickflow 零修改集成** — `HarnessRegistry` 子类化 `Registry`，不修改 tickflow 任何代码 | `HarnessRegistry(Registry)` | `registry.py` |

---

## 待实现 🔲

### 2. 对齐检查

**说明**：执行中判断当前产出是否偏离 spec 目标。不是框架强制行为，而是一个可复用的内置 harness 节点。模板设计者在 flow 中自行插入（通常放在关键产出节点之后），框架不额外调度。

**实现方向**：
- 提供一个内置 harness `align_check`，已注册在默认 registry 中
- prompt 接受 spec + tasklist + 当前位置 + 所有前置节点输出，返回"对齐/偏离 + 建议"
- tasklist 模板中使用示例：

```json
{
  "C": {
    "type": "harness",
    "harness": "align_check",
    "inputs": {"output_a": "A", "output_b": "B"},
    "prompt": "判断以上输出是否偏离 spec 目标..."
  },
  "Flow": "A --> B --> C"
}
```

- 最小开销：不插入即不执行

---

### 4. submodule（含模块打包/发布）

**说明**：tasklist 固定、spec 强模板化的嵌入式 module——一个特定 I/O 的"箱子"。同时也是 module 作为一等公民的打包与发布单元。submodule 的定义结构天然包含打包清单所需的一切——一次设计覆盖"嵌入式运行"+"自描述"+"发布"三个需求。

**核心概念**：一个 submodule 是一个自包含目录，内含 `module.json` 清单文件：

```
my_translator/
├── module.json          # 清单 = submodule 定义 + 打包单元
├── harnesses/            # 自带 harness 配置（可选）
├── scripts/              # 自带 script（可选）
└── commands/             # 自带 command 配置（可选）
```

**module.json 结构**：

```json
{
  "name": "my_translator",
  "version": "1.0.0",
  "description": "专业翻译模块",
  "submodule": true,

  "spec_schema": {
    "input": ["source_text", "style"],
    "output": ["translation"]
  },

  "requires": {
    "harnesses": ["translate"],
    "scripts": ["format_output"]
  },

  "provides": {
    "harnesses": ["translate"],
    "scripts": ["format_output"]
  },

  "tasklist": {
    "Tasks": {
      "A": { "type": "harness", "harness": "translate" },
      "B": { "type": "script", "script": "format_output" }
    },
    "Flow": "A --> B"
  }
}
```

**四层含义**：

| 层次 | 内容 | 用途 |
|------|------|------|
| **I/O 契约** | `spec_schema` | 声明输入/输出字段，外部调用者和前端可据此生成界面 |
| **依赖声明** | `requires` | 依赖的外部 harness/script/command 名称，加载时可校验 |
| **供给声明** | `provides` | 本 module 自带的实现，可被其他 module 引用 |
| **执行定义** | `tasklist` | 内置 workflow，submodule 模式下固定不可修改 |

**运行模式**：

- 作为 submodule 嵌入运行时：默认关闭 audit、EventBus、持久化，对外为 `(input_dict) -> output_dict` 纯函数
- 作为独立 module 运行时：完整 runner，audit + snapshot + 回滚全开

**ModuleLoader**：

```python
loader = ModuleLoader()
module = loader.load("path/to/my_translator/")
# 自动：解析 module.json → 注册 provides → 校验 requires → 构建 SubModule 实例
result = await module.run({"source_text": "Hello", "style": "formal"})
```

**发布/安装**：打包 = 目录压缩或 git repo；安装 = ModuleLoader 从本地目录或远程 URL 加载。后续可支持模块注册表。

**依赖**：spec + tasklist 输入通道（#1）

---

### 5. 快照/回滚 Module 封装

**说明**：tickflow 底层已有 `snapshot()` / `restore()` / `checkpoint()` / `rollback_to()`。Module 层需要封装这些，并支持"回滚时调整 spec 和 tasklist 未执行部分"——对应文档中的"回滚时可以调整 spec 和 tasklist 中未执行的部分"。

**实现方向**：
- `Module.snapshot() → dict` — 包含 spec、tasklist、tickflow snapshot
- `Module.restore(snapshot)` — 回滚 runner + 可选更新 spec/tasklist
- `Module.rollback_to(checkpoint_label)` — 回退到命名检查点
- 回滚后更新未执行的 task 节点时，需校验新 task 与已执行节点的兼容性
- 具体设计留到实现阶段细化

**依赖**：spec + tasklist 输入通道（#1）

---

### 6. 数据暴露 SDK

**说明**：为状态监控、可视化、外部 agent 提供统一的数据查询接口。不是新数据容器，而是对 tickflow RunState + EventBus 的查询封装。随功能开发渐进生长。

**实现方向**：
- `ModuleSDK` 类，构造时注入 runner + event_bus
- 初始骨架预留接口，按需实现：
  - `outputs_history()` → RunState._edges — 带来源标记的输出历史
  - `audit_timeline()` → RunState._records — 可视化进度时间线
  - `node_events(node)` → EventBus 录制 — 节点运行时详情（prompt、token 流、script 运行状态等）
  - `current_state()` → RunState._state — 当前节点状态快照
  - `snapshot()` / `restore_data()` — 预留回滚接口
  - `alignment_status()` — 预留对齐检查结果
- 非 Module 内部实现——独立模块，消费者按需使用
- 核心原则：**不做新数据容器，只做查询封装**。数据唯一真相源是 tickflow RunState + EventBus

---

### 7. 运行状态查询

获取当前 Module 的运行状态（静态值）。

---

## 不在当前范围 ⏸️

| 功能 | 说明 |
|------|------|
| 前端可视化 | 见可视化专项设计（未来） |
| 外部 agent 调用 (MCP) | 见 MCP 专项设计（未来） |
| 自建 agent (斜杠指令) | 见 agent 专项设计（未来） |
| module 独立进程 | 多进程架构（未来） |
| 框架自带 script 库 | 常用场景脚本集合（未来） |

---

## 实现顺序建议

```
┌─────────────────────────┐
│ 1. spec+tasklist 输入   │  ✅ 已完成（含一致性审核 #4）
├─────────────────────────┤
│ 2. 运行状态查询         │  ← 依赖module的搭建完成
├─────────────────────────┤
│ 3. 对齐检查 harness     │  ← 独立，不依赖其他
├─────────────────────────┤
│ 4. 一致性审核           │  ✅ 随 #1 完成
├─────────────────────────┤
│ 5. submodule + 打包/发布│  ← 依赖 #1，含 module.json + ModuleLoader
├─────────────────────────┤
│ 6. 快照/回滚封装        │  ← 依赖 #1
├─────────────────────────┤
│ 7. SDK 数据暴露层       │  ← 可与其他并行，渐进生长
└─────────────────────────┘
```

`#1 → #4 → #5 → #6 & #2` 有依赖链。`#5` 是最大的任务——submodule + 打包发布合并实现，一次设计覆盖"嵌入式运行 + 自描述清单 + 模块发布"。`#3` 和 `#7` 可独立进行。
