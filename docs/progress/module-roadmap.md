# SpecModule 开发进度与路线

> 最后更新：2026-08-10

本文档追踪 SpecModule module 核心功能的开发状态与实现方向。

**战略定位（2026-08-10 更新）**：框架能力（第一级：开发者层面）基本完成——执行元件、编排、状态数据、快照/回滚均已就绪。下一阶段聚焦**第二级：使用者层面**——让使用者能方便地运行、观察、审阅、接续工作流。实现方式遵循 **"跨形态共享的逻辑进共享层，重复出现才抽"** 的流程约定：CLI 先行，查询组合逻辑作为共享层沉淀，功能形态（AGENT / Web）渐进叠加。

## 完成度速览

已实现：**18** / 下一阶段：**3 个 Phase**（CLI → AGENT → Web）

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

> 战略：框架能力已就绪，聚焦使用者体验。**双线推进**：
>
> - **形态线**（消费方，渐进叠加）：**CLI（终端）→ AGENT（MCP/ACP）→ Web（可视化）**
> - **实践线**（验收驱动，贯穿各 Phase）：开发**真实落地的 module**（论文优化 → 论文转 PPT），每个 Phase 以实践线 module 为验收用例
>
> 流程约定：
> - **跨形态共享的逻辑进共享层，重复出现才抽**——不做前置接口设计；逻辑要进共享层（module_harness）的唯一条件是**确定有第二个消费者**（例：查询组合逻辑已被 CLI 宿主/查询形态、MCP、Web 共同消费 → 放查询模块，形态只 import 不实现）
> - **内置工具提炼**——开发过程中凡出现有通用价值的实现，主动提炼为内置工具（见下），不留死在 module 内部

### Phase 0：CLI 使用者界面 + 历史审阅

**✅ 子集已交付（2026-08-10，spec：docs/superpowers/specs/2026-08-10-specmodule-cli-design.md）**：
- `specmodule run` — 按名选模块 + spec/tasklist（终端内联或文件）+ 三级实时显示（tick/节点/状态 → +产出预览 → 完整块）+ `--mock` 冒烟 + `--verbose {1,2,3}`
- `specmodule status` — 复用 `query_run_status`（文本/JSON）
- `specmodule review` — tick 时间线 + `--tick/--node/--failed` 过滤 + `--json`
- 模块入口：`modules/<name>.py` 声明 `ModuleEntry`（`--modules-dir` 默认 `modules/`，兼容未来 init 布局）
- 查询组合逻辑沉淀 `module_harness/query.py`（CLI/MCP/Web 三形态复用）

**后续迭代（本次未做，roadmap 记录）**：截断/暂停续跑（Ctrl+C 保存状态 → `resume`）、`snapshot / rollback` CLI 命令、`visualize`（mermaid 导出）、`init` 脚手架（scripts/harnesses/submodules/modules 分目录实例搭建）。快照/回滚能力本身已就位（Module.snapshot/restore/checkpoint/rollback_to），仅缺 CLI 命令形态。

**说明**：第一个消费形态。扩展 tickflow/cli.py 之外的 Module 层命令（`specmodule` CLI），覆盖使用者的完整工作流：运行、观察、审阅、接续。

**实现方向**：
- [x] `specmodule run` — 从 spec/tasklist/模板运行工作流
- [x] `specmodule status` — 查询运行状态（对齐 `query_run_status`）
- [x] `specmodule review` — **历史审阅**（本阶段核心新功能）：按 tick 列出每节点产出/错误（tick ↔ 产出对应，fired 轨迹 + firings 表已铺好基础），定位问题 tick
- [ ] `specmodule snapshot / resume / rollback` — 快照/回滚/续跑操作
- [ ] `specmodule visualize` — mermaid/文本图导出（Graph.to_mermaid 已存在）
- 依赖：快照/回滚已就位；查询组合逻辑按提炼纪律长在 module_harness 查询模块，CLI 只 import 不实现
- **验收用例**：论文优化 module 全流程 CLI 化——`specmodule run`（默认 spec 一键优化）→ `review`（逐 tick 看每段优化产出）→ 不满意 `rollback`/`resume` 换 promptmode 重跑。**Phase 0 完成标志 = 论文优化 module 在 CLI 里跑通**

**2026-08-10 里程碑**：M1 在 CLI 的 `--mock` 冒烟已跑通（run → status → review 全链路）；真实 LLM 验收待补（`--spec-file example/spec.academic_writer.json`）。Phase 0 完成标志 = 论文优化 module 在 CLI 里跑通（真实 LLM）。

### Phase 1：AGENT 接口（MCP / ACP）

**说明**：第二个消费形态。把查询能力暴露给外部 agent（MCP 服务或 ACP），让 agent 能读取工作流数据、发起运行、审阅产出。

**实现方向**：
- MCP 服务：查询函数映射为 MCP 工具（run/status/review/snapshot/resume）
- 或 ACP（Agent Client Protocol）——实现时二选一，以生态成熟度与目标 agent 环境为准
- 依赖：Phase 0 沉淀的查询函数（MCP 薄层，零逻辑）
- **验收用例**：论文→PPT module 开发启动（agent 通过 MCP 发起运行与审阅）

### Phase 2：Web 可视化

**说明**：第三个消费形态。浏览器面板：实时运行状态、历史审阅时间线、产出对比。独立前端，消费查询层。

**实现方向**：
- 前端：实时 tick 流、节点状态图、审阅面板
- 后端：查询层的 HTTP 封装（或直接消费跨进程查询）
- 依赖：Phase 0 沉淀的查询函数 + 历史审阅语义
- **验收用例**：论文优化 + 论文→PPT 双 module 全量接入（运行可视化 + 产出对比）

---

## 实践线：落地 module（贯穿各 Phase，验收驱动）

> 开发**真实可用的 module**，每个 Phase 以它为验收用例；开发过程中**反哺框架**（查询层需求、内置工具提炼、submodule/script 完善）。

### M1 论文优化（轻量，Phase 0 验收用例）

**目标**：将中英混杂、混乱重复的灵感式写作文段，逐步整合优化为符合学术英语写作要求的文段。

**形态**：输入内容 + 默认 spec（或少量 spec 覆盖）——"零配置可用"是核心体验。

**流程方向**（默认 spec 定义）：清洗（去重复/规范化）→ 逐段优化 → 学术英语化 → 整合输出 + 变更说明

**框架验证点**：
- **多轮迭代**（tickflow loop）：优化不满意 → 快照回退 → 换 promptmode 重跑——快照/rollback/resume 的真实使用
- 失败节点不阻断（llm Failure → 下游跳过、运行继续）
- 默认 spec 的"零配置可用"体验（翻译通道：spec only → tasklist）
- 历史审阅：逐段优化前后对比（Phase 0 review 命令的核心场景）

### M2 论文 → PPT（重量级，Phase 0 后启动）

**目标**：从论文（长文）生成演示文稿（PPT），每页内容与布局由完整细致 spec 定义。

**形态**：完整 spec 驱动（页数、每页标题/要点/布局/风格）+ 复杂流水线。

**流程方向**：章节拆解 → 大纲生成 → 逐页内容生成（并行）→ 渲染（script/command 生成 pptx/json）

**框架验证点**：
- **完整 spec 驱动**（结构化描述每页布局与约束）
- 复杂流图（AND/OR join、并行页生成与合并）
- command/script 节点（渲染工具、格式转换）
- submodule 打包发布（M2 发布为可复用 submodule + spec schema 契约）

---

## 内置工具提炼机制

> 双通道：**规划预设**（M1/M2 的已知通用需求 → 预设计方向）+ **开发提炼**（开发中出现的通用价值实现 → 主动提炼）。

**提炼入口与去向**：

| 提炼入口（module 开发中出现） | 判定标准（三者齐备才提炼） | 去向 |
|---------|---------|------|
| 文本处理/清洗/检测逻辑（script） | 跨场景可复用 + 接口稳定 + 有文档 | 内置 script 库（`builtins.py` 扩展或新 `scripts.py`） |
| 打磨后的 prompt/harness 配置 | 抽象为通用模式（如"学术写作优化"） | 内置 harness（`register_builtin_harnesses` 扩充） |
| 常用流水线骨架 | 可作为新场景起点 | 内置 tasklist 模板（`templates/builtin/`） |
| 通用节点/工具（渲染、格式转换） | 不绑定 module 业务语义 | 内置 script/command 注册 |

**配套约定**：
- 提炼时机：每个功能完成时（或提交前）做一次"通用性自检"
- YAGNI 对冲：不满足判定标准的不提炼，避免为提炼而提炼
- 内置 script 库由提炼机制驱动，从"不在当前范围"移入第二阶段

---

## 模块组合讨论与决策（2026-08-10，已修正）

**背景**：example 模块开发（灵感式写作 → 学术英语，含两阶段"原始↔当前稿"事实审阅 loop）
引出"模块组合"讨论。**原判定（本段早先版本）基于错误前提**：把声明式 submodule 节点
曲解为"图级组合"（"仅省 flow 骨架几行边"），转而采纳"嵌套执行"。用户期望的是组合/封装
的**声明式能力**，不是省骨架。2026-08-10 修正，见 `docs/superpowers/specs/2026-08-10-submodule-node-design.md`。

**三种复用模型（修正后）**：

| 模型 | 机制 | 复用对象 | 审计 | 结论 |
|------|------|---------|------|------|
| **submodule 一等节点** | tasklist 直接写 `{type: "submodule", submodule: "名", inputs, outputs, [LLM 覆盖]}`；父模块 `modules` 类属性声明；打包内置 `submodules/` | 完整模块（黑盒处理单元） | 内部过程无审查意义 → 嵌入模式（不进审计/快照/回滚），只暴露终点输出 | ✅ **采纳**（本次） |
| **嵌套执行** | async script node 内 `await module.run()`（收 spec → 返回结果） | 整个黑盒模块 | 子 run 独立落盘可审计；父子 run 分离 | ✅ 保留为另一场景：任务中间过程有可复用、**需要审计**的完整 module 时，module 之间平级组合（不存在"sub"） |
| **图级组合**（子流程嵌入） | 子流程展开进主图（前缀隔离 / spec 透传 / 跨子图死锁分析） | 图结构（边+guard 语义） | 好（节点进主图） | ⏸️ 不做：用户定位 submodule 为黑盒处理单元，非图结构复用；骨架复用模板机制兜底 |

**submodule 节点语义要点**：
- 定义单元 = `SubModule` 类（双重身份：可独立运行/打包，也可被引用为节点）；与 harness/script
  同级（tasklist 节点实现类型），不冲突
- 节点级 LLM 设置（model/temperature/think/api_params）传播到子模块内部所有 harness
- 输出 = 子流程终点输出全量，可选 `outputs` 字段挑选/重命名；子 run 嵌入模式零落盘
- 依赖声明在父模块类属性 `modules`（无全局注册表）；pack 递归内置子模块 → 加载无运行时依赖
- guard 打包强制注册名与函数名一致（与 @script 同约定）；框架两缺口已修复（`_check_flow` 传
  registry、SubModule guards 通道）

**触发条件**：图级组合 / 零件提炼在实践线（M1/M2）出现第二个真实使用方时再评估（YAGNI 对冲）。

**本次 example 计划**（记于此处）：
- `example/fact_review_loop.py`：`FactReviewLoop(SubModule)`——通用事实审阅循环（spec: `{original_text, draft_text}` → `{text, attempt, clean, issues_remaining}`）。OR-join Merge 合并种子/修复稿并计数轮次，2 个 guard（`has_issues`/`clean`，严格互补）路由循环与退出，轮次上限 3 为 guard 内联常量（自包含、可打包），完全通用可打包
- `example/academic_writer.py`：**普通 Module 过程式组装**（2026-08-10 修正：**仅 loop 为 SubModule**，academic_writer 为顶层工作流——整机消费零件，不定义 SubModule 类）——流水线 `[A]Organize → Loop1 → Polish → Loop2 → Finalize → Report`（Loop1/2 为 **submodule 节点**引用 `fact_review_loop`，`Module(modules={"fact_review_loop": FactReviewLoop})` 声明解析）；spec: `{raw_text, target_field?, max_words?}` → `{final_text, modification_notes}`；两阶段复用同一 `fact_review_loop` 处理单元，节点级 LLM 配置可传播
- **框架缺口修复**（模块无关，通用价值，随 example 一并做）：
  1. `TasklistValidator._check_flow` 解析 flow 时未传 registry → 任何 guard 边在校验阶段必被拒（`translator.py`，一行修复）——✅ **已修复**
  2. `SubModule` 无 guard 声明/收集/注册/pack 导出/加载机制（`submodule.py` + `loader.py`）——loop 必须用 guard，当前类式模块无法声明——✅ **已修复**

**✅ 已完成（2026-08-10，`example/` 落地）**：`example/fact_review_loop.py`
（FactReviewLoop + 自包含 guards，pack 含 guards/ 导出，roundtrip 测试覆盖）、
`example/academic_writer.py`（普通 Module **双模板**：`academic_writer` 默认——
Loop1/Loop2 为 submodule 节点引用 fact_review_loop；`academic_writer_detailed`
详细模式——loop 内联展开到主图全程可审计，`run_writer(spec, mode=...)` 切换，
输出 final_text + modification_notes）、demo 入口（`--mock` 免 key 冒烟 +
`--detailed`）、示例草稿、两级用户 README、mock 测试（`pytest example/ -q`，17 项）。
框架缺口修复已随 436dbcc 前的系列提交落地。设计见
`docs/superpowers/specs/2026-08-10-academic-writer-design.md`（状态：已实现）。

---

## 不在当前范围 ⏸️

| 功能 | 说明 |
|------|------|
| 自建 agent (斜杠指令) | 见 agent 专项设计（未来） |
| module 独立进程 | 多进程架构（未来） |
| 图级模块组合（子流程嵌入） | 判定不做，见"模块组合讨论与决策"（submodule 一等节点已覆盖当前需求） |
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

第二阶段（进行中 🔜）：使用者层面（双线推进）
┌────────────────────────────────────────────────────────┐
│ 形态线（消费方）               实践线（验收驱动）        │
│  0. CLI + 历史审阅       ←──  M1 全流程 CLI 化（验收）   │
│  1. AGENT (MCP/ACP)      ←──  M2 论文→PPT module 开发     │
│  2. Web 可视化           ←──  双 module 全量接入（验收）  │
└────────────────────────────────────────────────────────┘
并行机制：内置工具提炼（开发中通用价值 → 内置 script/harness/模板）
流程约定：跨形态共享的逻辑进共享层，重复出现才抽，不为将来抽象
```

依赖链：Phase 0 → 1 → 2 顺序推进；每 Phase 独立 spec → plan → 实现。历史审阅（Phase 0 核心）依赖 fired 轨迹 + firings 表（已就位）。AGENT/Web 形态消费 Phase 0 沉淀的查询函数。实践线 M1 先于 M2（基础验证 → 重量级验证），每个 Phase 完成标志 = 对应实践线 module 在形态线跑通。
