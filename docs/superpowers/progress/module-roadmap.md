# SpecModule 开发进度与路线

> 最后更新：2026-06-30

本文档追踪 SpecModule module 核心功能的开发状态与实现方向。
范围限定：module 内核（harness / script / command / spec / tasklist / submodule / 编排 / 状态数据）。
不包含：前端可视化、外部 agent 交互（MCP / 斜杠指令）、独立进程部署。

## 完成度速览

已实现：**12** / 待实现：**7**

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

### 编排与基础设施

| 功能 | 实现 | 关键文件 |
|------|------|----------|
| **Module 编排器** — spec + template → 翻译 → graph → runner | `Module.build_runner()` / `Module.run()` | `module.py` |
| **命名空间隔离** — body 注册名 `{module_id}:{key}`，同进程多 module 不冲突 | `TasklistTranslator` | `graph_builder.py` |
| **EventBus** — 类型安全的事件发布订阅，harness 9 种 + script 3 种 + command 3 种事件 | `EventBus` | `events.py` |
| **tickflow 零修改集成** — `HarnessRegistry` 子类化 `Registry`，不修改 tickflow 任何代码 | `HarnessRegistry(Registry)` | `registry.py` |

---

## 待实现 🔲

### 1. spec + 自定义 tasklist 输入

**说明**：同时传入 spec 和自定义 tasklist（不经过翻译模版），一致性检查后 tasklist 直入 graph builder。当前 `Module` 只接受 `spec + template_name`，缺少 `spec + tasklist` 的输入通道。

**实现方向**：
- `Module.__init__` 新增 `tasklist: Tasklist | None` 参数
- 若传入 tasklist → 跳过翻译，触发一致性审核（复用翻译 LLM，输入 spec + tasklist，输出 pass/fail + 建议）
- 审核通过后 `TasklistTranslator.build(tasklist)` → runner
- 审核失败 → 阻塞执行，返回问题描述

**依赖**：一致性审核（见下方 #4）

---

### 2. 对齐检查

**说明**：执行中判断当前产出是否偏离 spec 目标。不是框架强制行为，而是一个可复用的内置 harness 节点。模板设计者在 flow 中自行插入（通常放在关键产出节点之后），框架不额外调度。

**实现方向**：
- 提供一个内置 harness `align_check`，已注册在默认 registry 中
- prompt 接受 spec + 所有前置节点输出，返回"对齐/偏离 + 建议"
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

- 后续可补充便捷语法：`Flow: "A --> B => C(align_check)"` 自动生成全前置入边
- 最小开销：不插入即不执行

---

### 3. 一致性审核

**说明**：spec + tasklist 模式下检查二者是否逻辑一致（tasklist 是否能实现 spec 目标）。复用翻译 LLM——它既然能从 spec 生成 tasklist，自然能判断已有的 tasklist 是否合理。

**实现方向**：
- 复用 `spec_to_tasklist` 翻译 harness，传入 spec + 待审 tasklist
- 输出 `{"consistent": true/false, "suggestions": "..."}`
- 在 `Module.build_runner()` 中，若同时传入了 spec + tasklist → 审核 → 通过才构建
- 审核失败 → 阻塞，返回问题供修改

---

### 4. submodule

**说明**：tasklist 固定、spec 强模板化的嵌入式 module——一个特定 I/O 的"箱子"。内部复用 Module 的完整流程，但关闭不必要的开销。

**实现方向**：
- `SubModule(Module)` 或 `Module(inputs, outputs, tasklist, spec, embedded=True)`
- 嵌入式模式默认关闭：
  - `keep_records=False`（关 audit log）
  - `EventBus.null()`（关事件总线）
  - 不持久化快照、不回滚（除非显式开启）
  - 固定 spec 和 tasklist，不可修改
- 输入 → 内部 runner → 输出，对外表现为 `(dict) -> dict` 纯函数
- 核查清单：tickflow 的 `keep_records` 开关 ✅ 已有、`EventBus.null()` ✅ 已有、`Backend` 可选 ✅ 已有。需核查 `runner.py` 的 hooks 注册是否有无开销模式

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

### 7. 模板执行时键值替换

**说明**：当前 `{spec.xxx}` 替换只在翻译阶段。执行阶段需要从 tickflow 当前状态动态取值填入 prompt。文档中提到的 "input" 字段（"用于替换提示词中的关键字，作为输入；内容在 tickflow 全局记录的内容字典里"）实际上就是 node 的 inputs 映射——从上游节点产出中取值注入 prompt。

**实现方向**：
- 当前 `TaskDefinition.inputs` 已映射到 graph 的 `node.inputs`（→ `view.field_name.value`）
- body 内部通过 `view.xxx.value` 取上游产出
- PromptRenderer 的 `{key}` 替换已经是运行时从 DictView 取值
- 此项标记为"验证现有实现是否覆盖"——若已覆盖，移入已实现清单

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
│ 1. spec+tasklist 输入   │  ← 解锁 #3 #4 #5
├─────────────────────────┤
│ 2. 键值替换验证         │  ← 小，验证后可标记为完成
├─────────────────────────┤
│ 3. 对齐检查 harness     │  ← 独立，不依赖其他
├─────────────────────────┤
│ 4. 一致性审核           │  ← 依赖 #1
├─────────────────────────┤
│ 5. submodule            │  ← 依赖 #1
├─────────────────────────┤
│ 6. 快照/回滚封装        │  ← 依赖 #1
├─────────────────────────┤
│ 7. SDK 数据暴露层       │  ← 可与其他并行，渐进生长
└─────────────────────────┘
```

`#1 → #4 → #5 → #6` 有依赖链，`#3` 和 `#7` 可独立进行。
