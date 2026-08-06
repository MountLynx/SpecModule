# SpecModule 开发进度与路线

> 最后更新：2026-08-06

本文档追踪 SpecModule module 核心功能的开发状态与实现方向。

**战略定位（2026-08-06 更新）**：框架能力（第一级：开发者层面）基本完成——执行元件、编排、状态数据、快照/回滚均已就绪。下一阶段聚焦**第二级：使用者层面**——让使用者能方便地运行、观察、审阅、接续工作流。实现方式遵循 **"每次新功能实现前先设计所需 SDK"** 的流程约定：数据查询接口先行，功能形态（CLI / AGENT / Web）作为 SDK 的消费方渐进叠加。

## 完成度速览

已实现：**18** / 下一阶段：**4 个 Phase**（SDK → CLI → AGENT → Web）

---

## 已实现 ✅（第一级：框架能力）

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
| **快照/回滚封装** — 每 tick 轻量快照（剥离 records，O(节点+边)，tickflow `_persist_tick` 落盘）+ 精确 tick 号回退 `resume(tick)` + 跨进程续跑 + 进程内 `snapshot()`/`restore()`/`checkpoint()`/`rollback_to()` + 兼容性校验（2 硬错误 + 3 警告）+ `list_checkpoints()` 显示 (tick, fired) | `ModuleInputStore` + `check_resume_compat` + `Module.resume` | `checkpoint.py`, `module.py` |

---

## 下一阶段 🔜（第二级：使用者层面）

> 战略：框架能力已就绪，聚焦使用者体验。三个消费形态**渐进叠加**：
> **CLI（终端）→ AGENT（MCP/ACP）→ Web（可视化）**。
> 流程约定：**每个新功能实现前先设计所需 SDK**——SDK 是唯一数据查询层，形态只是它的消费方。

### Phase 0：数据暴露 SDK（#6）

**说明**：统一数据查询接口，作为后续所有使用者形态的公共地基。不是新数据容器，而是对 tickflow RunState + EventBus + run.sqlite 的查询封装。进程内（注入 runner）与跨进程（读 run.sqlite）统一语义。

**实现方向**：
- `ModuleSDK`：查询封装，接口先于功能定稿
  - `outputs_history()` → 输出历史（带 tick/来源标记）——firings 表
  - `audit_timeline()` → 每 tick 每节点的 inputs/output/status/error —— firings 表
  - `node_events(node)` → EventBus 录制（prompt、token 流、script 运行状态）
  - `current_state()` → 节点 mutable state
  - `checkpoints()` / `snapshot()` / `restore_data()` → 快照/回滚查询
  - `alignment_status()` → 对齐检查结果
- 核心原则：**不做新数据容器，只做查询封装**。数据唯一真相源是 tickflow RunState + firings 表 + EventBus
- 依赖：#5 快照/回滚（fired 轨迹、自包含快照）已就位

### Phase 1：CLI 使用者界面 + 历史审阅

**说明**：第一个消费形态。扩展 tickflow/cli.py 之外的 Module 层命令（`specmodule` CLI），覆盖使用者的完整工作流：运行、观察、审阅、接续。

**实现方向**：
- `specmodule run` — 从 spec/tasklist/模板运行工作流
- `specmodule status` — 查询运行状态（对齐 `query_run_status`）
- `specmodule review` — **历史审阅**（本阶段核心新功能）：按 tick 列出每节点产出/错误（tick ↔ 产出对应，fired 轨迹 + firings 表已铺好基础），定位问题 tick
- `specmodule snapshot / resume / rollback` — 快照/回滚/续跑操作
- `specmodule visualize` — mermaid/文本图导出（Graph.to_mermaid 已存在）
- 依赖：Phase 0 SDK（review/status 等命令消费 SDK 查询）

### Phase 2：AGENT 接口（MCP / ACP）

**说明**：第二个消费形态。把 SDK 查询能力暴露给外部 agent（MCP 服务或 ACP），让 agent 能读取工作流数据、发起运行、审阅产出。

**实现方向**：
- MCP 服务：SDK 方法映射为 MCP 工具（run/status/review/snapshot/resume）
- 或 ACP（Agent Client Protocol）——实现时二选一，以生态成熟度与目标 agent 环境为准
- 依赖：Phase 0 SDK（MCP 薄层，零逻辑）

### Phase 3：Web 可视化

**说明**：第三个消费形态。浏览器面板：实时运行状态、历史审阅时间线、产出对比。独立前端，消费 SDK。

**实现方向**：
- 前端：实时 tick 流、节点状态图、审阅面板
- 后端：SDK 的 HTTP 封装（或直接消费跨进程查询）
- 依赖：Phase 0 SDK + Phase 1 历史审阅语义

---

## 不在当前范围 ⏸️

| 功能 | 说明 |
|------|------|
| 自建 agent (斜杠指令) | 见 agent 专项设计（未来） |
| module 独立进程 | 多进程架构（未来） |
| 框架自带 script 库 | 常用场景脚本集合（未来） |
| 跨 session 快照重进/派生分支（issue #1） | 底层已就位（自包含快照 + fired），Module 层显式 API 待需求驱动 |

---

## 实现顺序建议

```
第一阶段（已完成 ✅）：框架能力
┌─────────────────────────┐
│ 1. spec+tasklist 输入   │  ✅（含一致性审核）
├─────────────────────────┤
│ 2. 运行状态查询         │  ✅
├─────────────────────────┤
│ 3. 对齐检查 harness     │  ✅
├─────────────────────────┤
│ 4. 一致性审核           │  ✅
├─────────────────────────┤
│ 5. submodule + 打包/发布│  ✅
├─────────────────────────┤
│ 6. 快照/回滚封装        │  ✅（含冗余清理、S3 自包含修正）
└─────────────────────────┘

第二阶段（进行中 🔜）：使用者层面
┌─────────────────────────┐
│ 0. 数据暴露 SDK (#6)    │  ← 地基先行（SDK 先行约定）
├─────────────────────────┤
│ 1. CLI + 历史审阅       │  ← 第一个形态（SDK 首个用例）
├─────────────────────────┤
│ 2. AGENT (MCP/ACP)      │  ← 第二个形态（SDK 薄封装）
├─────────────────────────┤
│ 3. Web 可视化           │  ← 第三个形态（独立前端）
└─────────────────────────┘
```

依赖链：Phase 0 → 1 → 2 → 3 顺序推进；每 Phase 独立 spec → plan → 实现。历史审阅（Phase 1 核心）依赖 fired 轨迹 + firings 表（已就位）。AGENT/Web 形态依赖 SDK 定稿（Phase 0 先行设计的价值所在）。
