# SpecModule 开发进度与路线

> 最后更新：2026-08-06

本文档追踪 SpecModule module 核心功能的开发状态与实现方向。
范围限定：module 内核（harness / script / command / spec / tasklist / submodule / 编排 / 状态数据）。
不包含：前端可视化、外部 agent 交互（MCP / 斜杠指令）、独立进程部署。

## 完成度速览

已实现：**18** / 待实现：**1**

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
| **EventBus** — 类型安全的事件发布订阅，harness 6 种 + script 3 种 + command 3 种 + 一致性审核 1 种事件 | `EventBus` | `events.py` |
| **tickflow 零修改集成** — `HarnessRegistry` 子类化 `Registry`，不修改 tickflow 任何代码 | `HarnessRegistry(Registry)` | `registry.py` |
| **submodule — 类式定义 + 打包发布** | `SubModule`（类式声明 + `@script` + `pack()` 导出）+ `ModuleLoader`（加载 + requires 校验）+ 内置 harness 集 | `submodule.py`, `loader.py`, `builtins.py` |
| **运行状态查询** — 跨进程查询 Module 当前运行状态：status.json 阶段机（9 阶段原子写，status_file 独立开关）+ run.sqlite 最新快照叠加 | `Module._write_phase` + `query_run_status` | `module.py`, `status.py` |
| **对齐检查** — 内置 `align_check` harness 节点，`{spec}`/`{tasklist}`/`{node}` 常量 token 注入，输出对齐/偏离 + 建议 | `ALIGN_CHECK_CONFIG` + graph_builder 常量 token | `align.py`, `graph_builder.py` |
| **快照/回滚封装** — 自动检查点（每 tick 环形保留 20）+ 跨进程 `resume()` 续跑 + 进程内 `snapshot()`/`restore()`/`checkpoint()`/`rollback_to()` + 兼容性校验（2 硬错误 + 3 警告） | `AutoCheckpointStore` + `check_resume_compat` + `Module.resume` | `checkpoint.py`, `module.py` |

---

## 待实现 🔲

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
│ 2. 运行状态查询         │  ✅ 已完成
├─────────────────────────┤
│ 3. 对齐检查 harness     │  ✅ 已完成
├─────────────────────────┤
│ 4. 一致性审核           │  ✅ 随 #1 完成
├─────────────────────────┤
│ 5. submodule + 打包/发布│  ✅ 已完成
├─────────────────────────┤
│ 6. 快照/回滚封装        │  ✅ 已完成
├─────────────────────────┤
│ 7. SDK 数据暴露层       │  ← 可与其他并行，渐进生长
└─────────────────────────┘
```

`#1 → #4 → #5 → #6` 有依赖链。#5 已随 submodule 系统完成。`#5` 是最大的任务——submodule + 打包发布合并实现，一次设计覆盖"嵌入式运行 + 自描述清单 + 模块发布"。`#3`（对齐检查）与 `#7`（运行状态查询）已独立完成。
