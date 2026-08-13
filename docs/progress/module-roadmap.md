# SpecModule 开发进度与路线

> 最后更新：2026-08-11

## 战略定位（当前仓库 = 库）

本仓库是 **SpecModule 的库（framework）**，不是某一形态的产品。它提供：

- **执行引擎**：`tickflow`（独立上游）+ `llm` + `module_harness`（spec → tasklist → Petri-net 图 → 运行）
- **共享查询层**：`module_harness/query.py`——运行状态/历史时间线的纯函数，被 CLI / MCP / Web / 嵌入方共同 import
- **编程 API**：`Module / HarnessRegistry / SubModule / Translator` 等，可被其他项目直接 `import`（嵌入式：作为 LLM 工具套件开发）
- **CLI（随库分发）**：`specmodule run/status/review/init/visualize`——参考 Django 自建管理壳，库内建最基础的终端入口，随 `pip install specmodule` 提供
- **可选零依赖可视化开关**：stdlib `http.server` 推极简运行 feed；富交互终端界面 → TUI 生态项目；富交互编辑器 → webview 生态项目

**库是中性的**——不扛某一形态的完整 UX。形态定位由生态项目各自承担。

**分层依赖（严格，无环）**：

```
tickflow（独立上游仓库，零依赖）
    ↑
llm（自包含：仅 os/dataclasses/pathlib/typing）
    ↑
module_harness（依赖 tickflow + llm；含 query 共享层）
```

## 库 vs 生态：边界

| 形态 | 归属 | 说明 |
|------|------|------|
| **CLI** | 库内（参考 Django 管理壳） | 最基础终端入口，随 `pip install specmodule` 分发 |
| **TUI** | 生态项目 `SpecModule_tui/` | 富交互终端界面（扩展 CLI，面板/实时流） |
| **MCP** | 生态项目 `SpecModule_mcp/` | 薄层：query 函数→MCP 工具，供 agent 用 |
| **Web 可视化** | 生态项目 `SpecModule_webview/` | 独立前端 + HTTP API，消费查询层 |
| **嵌入式** | 库本身 | 编程 API 被别项目 import |

原则：**协议适配器（MCP / FastAPI / TUI 框架）永远活在生态项目里**；库只给"纯函数查询层 + 编排 helper + 薄 CLI"，保持依赖轻、可嵌入。

### CLI 在库内（薄壳，随库分发）

CLI 是最基础的终端入口，**留在库内**（参考 Django `django-admin`/`manage.py`）。理由：
- **零依赖**——只靠 `argparse` + 库本身，进库不增加任何依赖重量
- **init 必须与库版本咬合**——脚手架生成的是 `ModuleEntry / module.json / config.json` 这些库契约，同版本分发永不 drift
- 使用者必然 `pip install specmodule`，CLI 随包即得

**库内 CLI 命令**：
- `specmodule run` — 按名选模块 + spec/tasklist，三级实时显示（`--mock` 冒烟）
- `specmodule status` — 查询运行状态（文本/JSON）
- `specmodule review` — 历史时间线 + `--tick/--node/--failed` 过滤（文本/JSON）
- `specmodule init <name>` — python 原生单文件模块脚手架（已实现；声明式目录形态为后续）
- `specmodule snapshot / rollback / resume` — 快照/回退/续跑（库能力已就位）
- `specmodule visualize` — mermaid 导出（`Graph.to_mermaid` 已存在）

CLI 双身份：既是使用者最基础入口，也是开发者终端工作台（init/mock 冒烟/快照调试图）。

### TUI 独立（生态项目 `SpecModule_tui/`）

把 CLI **扩增成更便捷的富交互终端界面**（面板 / 实时流 / 键盘导航 / 交互式回滚）——这是独立产品，
依赖 TUI 框架（如 Textual），故独立成仓库。消费库的 query 层 + CLI，只 import 不实现。

## 完成度速览

已实现（库核心）：**18** 项框架能力（第一级）
下一阶段（库自身）：打包 / init 脚手架 / 嵌入式验证 / stdlib 可视化开关 / API 稳定化

---

## 已实现 ✅（库核心：框架能力）

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

## 下一阶段 🔜（库自身的路线图）

战略：框架能力已就绪。库接下来要做的不是"某一形态"，而是把它打磨成**干净、可嵌入、可打包的库**。

### 库路线：打包与嵌入

- [ ] **打包**：`pyproject.toml` + `[project.scripts] specmodule = module_harness.cli:main`（CLI 随库分发；TUI/MCP/Web 生态项目各自加自己的入口），保持依赖轻
- [x] **init 脚手架（python 原生单文件形态）** — `specmodule init <name>`：生成 `modules/<name>.py`
  单文件骨架（harness→script 流水线模板，`--mock` 即冒烟）+ 项目文件缺啥补啥（config.json /
  .env.example / .gitignore / spec.example.json / README.md）。见
  `openspec/changes/cli-init-scaffold/` 与 `docs/cli-usage.md#9`。
  - 方便 module 开发者快速搭建项目结构 ✅
  - module 使用者，创建与管理 module （后续做，例如大致形态可能是 submodule、harness、script、
    command 分别放目录，还有一个 module 目录放 module.json 或者 &lt;modulename&gt;.py，并不是严格的
    目录规范，算是一种推荐管理样式，相关 cli 指令可以看目录下 module 有哪些，做 spec、tasktamplate、
    module 管理等；配套的 load 也要做修改，把加载的 module 组件放到各个目录里面，这样也顺便就有了
    “公共 harness 库”公共 script 库的语义。）
- [ ] **嵌入式验证**：最小 demo 项目 `pip install specmodule` 后 `import Module / HarnessRegistry` 跑通一个 workflow——证明库面干净、可嵌入
- [ ] **stdlib 可视化开关**：`http.server` 极简运行 feed（零第三方依赖）；富交互编辑器在 webview 项目
- [ ] **API 稳定化**：query 层 + 编程 API 向后兼容（三个生态项目都依赖它）

**"嵌入模式"两个含义（不混用）**：
- **宿主整框架嵌入**（本库路线服务）：别项目 `import Module / SubModule` 作 LLM 工具套件。
  宿主传 `event_bus` 即选择性订阅（`HarnessFailed`/`OutputValidated` 等），事件投递与
  `keep_records`/`persist` **解耦**——`audit` 只管 records，不连带关事件；不传 bus 则
  静默零开销。见 `openspec/changes/decouple-embed-events-from-records/`。
- **submodule 作为黑盒节点**（模块组合场景）：父图驱动、只暴露终点输出，子节点内部
  无审查意义 → 真正零可观测性（不进审计/快照/回滚）。与此处宿主嵌入无关。

### 实践线（库验收驱动）

> 开发**真实可用的 module**，每个以库能力为验收用例；开发过程中**反哺框架**（查询层需求、内置工具提炼、submodule/script 完善）。

#### M1 论文优化（轻量，CLI 验收用例）

**目标**：将中英混杂、混乱重复的灵感式写作文段，逐步整合优化为符合学术英语写作要求的文段。

**形态**：输入内容 + 默认 spec（或少量 spec 覆盖）——"零配置可用"是核心体验。

**流程方向**（默认 spec 定义）：清洗（去重复/规范化）→ 逐段优化 → 学术英语化 → 整合输出 + 变更说明

**框架验证点**：
- **多轮迭代**（tickflow loop）：优化不满意 → 快照回退 → 换 promptmode 重跑
- 失败节点不阻断（llm Failure → 下游跳过、运行继续）
- 默认 spec 的"零配置可用"体验（翻译通道：spec only → tasklist）
- 历史审阅：逐段优化前后对比

#### M2 论文 → PPT（重量级，M1 后启动）

**目标**：从论文（长文）生成演示文稿（PPT），每页内容与布局由完整细致 spec 定义。

**形态**：完整 spec 驱动（页数、每页标题/要点/布局/风格）+ 复杂流水线。

**流程方向**：章节拆解 → 大纲生成 → 逐页内容生成（并行）→ 渲染（script/command 生成 pptx/json）

**框架验证点**：
- **完整 spec 驱动**（结构化描述每页布局与约束）
- 复杂流图（AND/OR join、并行页生成与合并）
- command/script 节点（渲染工具、格式转换）
- submodule 打包发布（发布为可复用 submodule + spec schema 契约）

---

## 生态项目（形态，独立仓库）

| 项目 | 定位 | 落地 |
|------|------|------|
| `SpecModule_tui/` | 富交互终端界面（扩增 CLI，面板/实时流） | `SpecModule_tui/roadmap.md` |
| `SpecModule_mcp/` | MCP/ACP 服务器，module 供 agent 用 | `SpecModule_mcp/roadmap.md` |
| `SpecModule_webview/` | 可视化编辑器 + HTTP API（消费查询层） | `SpecModule_webview/roadmap.md` |

各形态路线（TUI 功能、MCP 工具集、Web 功能）见对应项目 roadmap，此处不重复。

---

## 内置工具提炼机制

> 双通道：**规划预设**（M1/M2 的已知通用需求 → 预设计方向）+ **开发提炼**（开发中出现的通用价值实现 → 主动提炼）。

| 提炼入口（module 开发中出现） | 判定标准（三者齐备才提炼） | 去向 |
|---------|---------|------|
| 文本处理/清洗/检测逻辑（script） | 跨场景可复用 + 接口稳定 + 有文档 | 内置 script 库（`builtins.py` 扩展或新 `scripts.py`） |
| 打磨后的 prompt/harness 配置 | 抽象为通用模式（如"学术写作优化"） | 内置 harness（`register_builtin_harnesses` 扩充） |
| 常用流水线骨架 | 可作为新场景起点 | 内置 tasklist 模板（`templates/builtin/`） |
| 通用节点/工具（渲染、格式转换） | 不绑定 module 业务语义 | 内置 script/command 注册 |

**配套约定**：
- 提炼时机：每个功能完成时（或提交前）做一次"通用性自检"
- YAGNI 对冲：不满足判定标准的不提炼，避免为提炼而提炼
- 内置 script 库由提炼机制驱动，从"不在当前范围"移入

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
- 定义单元 = `SubModule` 类（双重身份：可独立运行/打包，也可被引用为节点）；与 harness/script 同级（tasklist 节点实现类型），不冲突
- 节点级 LLM 设置（model/temperature/think/api_params）传播到子模块内部所有 harness
- 输出 = 子流程终点输出全量，可选 `outputs` 字段挑选/重命名；子 run 嵌入模式零落盘
- 依赖声明在父模块类属性 `modules`（无全局注册表）；pack 递归内置子模块 → 加载无运行时依赖
- guard 打包强制注册名与函数名一致（与 @script 同约定）；框架两缺口已修复（`_check_flow` 传 registry、SubModule guards 通道）

**触发条件**：图级组合 / 零件提炼在实践线（M1/M2）出现第二个真实使用方时再评估（YAGNI 对冲）。

**本次 example 计划**（记于此处）：
- `example/fact_review_loop.py`：`FactReviewLoop(SubModule)`——通用事实审阅循环（spec: `{original_text, draft_text}` → `{text, attempt, clean, issues_remaining}`）。OR-join Merge 合并种子/修复稿并计数轮次，2 个 guard（`has_issues`/`clean`，严格互补）路由循环与退出，轮次上限 3 为 guard 内联常量（自包含、可打包）
- `example/academic_writer.py`：普通 Module 过程式组装（仅 loop 为 SubModule）——流水线 `[A]Organize → Loop1 → Polish → Loop2 → Finalize → Report`（Loop1/2 为 submodule 节点引用 `fact_review_loop`）；spec: `{raw_text, target_field?, max_words?}` → `{final_text, modification_notes}`；两阶段复用同一 `fact_review_loop` 处理单元，节点级 LLM 配置可传播
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
| 完整 Web UX / 图编辑器 | 属生态项目 `SpecModule_webview`，不在库内 |

---

## 实现顺序建议

```
第一阶段（已完成 ✅）：库核心框架能力
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

第二阶段（进行中 🔜）：库自身（打包 / 嵌入 / init 脚手架）
┌───────────────────────────────────────────────┐
│ 库：打包(pyproject) → init 脚手架 →            │
│     嵌入式验证(demo) → stdlib 可视化开关 →       │
│     API 稳定化                                  │
├───────────────────────────────────────────────┤
│ 生态（独立仓库，各自 roadmap）：                  │
│   TUI(SpecModule_tui) ← 富交互终端               │
│   MCP(SpecModule_mcp)   ←  M2 验收             │
│   Web(SpecModule_webview) ← 双 module 接入验收  │
└───────────────────────────────────────────────┘
并行机制：内置工具提炼（开发中通用价值 → 内置 script/harness/模板）
流程约定：跨形态共享的逻辑进共享层，重复出现才抽，不为将来抽象
```

依赖链：库先稳定（打包/嵌入/API），生态项目再各自落地。每 Phase 独立 spec → plan → 实现。
AGENT/Web 形态消费库沉淀的查询函数。实践线 M1 先于 M2（基础验证 → 重量级验证）。