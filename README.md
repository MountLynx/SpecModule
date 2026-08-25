# SpecModule

可审计、可调试、可完全掌控的 LLM 使用框架。

将 LLM 调用拆分为可组合的 Petri 网节点，每个节点是最小执行单元——翻译、审查、shell 命令、Python 函数。节点通过有向边连接（支持 AND/OR 汇合、循环），引擎以同步步进执行，所有状态集中记录。**每 tick 落盘轻量快照**，快照、暂停、精确回退（tick 号）都是低开销的。

## 架构

```
SpecModule/
├── tickflow                # Petri 网工作流引擎（外部 pip 依赖 tickflow-py，import 名 tickflow）
├── llm/                    # LLM 客户端（Anthropic + OpenAI 兼容）
│   ├── client.py
│   └── config.py           #   LLMConfig.from_env()：配置回退链（env > 项目根 > store）
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
├── examples/              # 嵌入式最小 demo（embed_minimal）+ 教程案例（tutorial）
└── docs/                  # 用户文档（guides/references/concepts）+ 内部文档（dev/）
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

## 快速开始

```bash
pip install specmodule
specmodule setup                    # 一次性配置 provider/model/key（写 store 级配置）
specmodule install <模块 pack 目录或 git URL>   # 获取模块（见 store-walkthrough）
specmodule run --module <名> --spec '{"text": "……"}' --mock   # --mock 免 key 冒烟
specmodule review --run-id <名>     # 审阅 tick 时间线
```

写第一个模块（入口声明 → harness/script 注册 → tasklist → 发布）见 [**从零到第一个模块（教程）**](docs/guides/tutorial-first-module.md)；store 使用闭环见 [**store-walkthrough**](docs/guides/store-walkthrough.md)；配置见 [**config-guide**](docs/guides/config-guide.md)。

## 文档导航

| 你是 | 入口 |
|------|------|
| 用 module（CLI 用户） | [store-walkthrough](docs/guides/store-walkthrough.md) → [cli-usage 参考](docs/references/cli-usage.md) |
| 写 module（开发者） | [教程：从零到第一个模块](docs/guides/tutorial-first-module.md) → [tasklist 执行语义](docs/references/tickflow-integration.md) → [语法参考](docs/references/spec-harness-syntax.md) |
| 理解框架（概念） | [concepts/SpecModule.md](docs/concepts/SpecModule.md) |
| 嵌入宿主项目 | [embedding.md](docs/guides/embedding.md)（demo：`examples/embed_minimal/`） |
| 完整索引 | [docs/README.md](docs/README.md) |

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
- **两种输入**：① 只传 spec（通过模板翻译为 tasklist）② 传 spec + tasklist（一致性审核后直入 graph builder）。选择依据与模板通道见 [concepts](docs/concepts/SpecModule.md)。

### 快照与回滚

每 tick 轻量快照（persist=True 时逐 tick 落盘），任意 tick 可精确回退（`resume(tick)` / `rollback`）；手动检查点 `checkpoint("label")` 永久保留；进程内 `snapshot()` / `restore()` 支持任意分支。持久化约定与敏感数据注意见 [concepts](docs/concepts/SpecModule.md)。

### submodule — 类式 module + 打包发布

`SubModule` 类式声明（含 `spec_schema` 输入契约）→ `pack()` 导出可发布清单（module.json + harnesses/ + scripts/ + commands/）→ `ModuleLoader` 加载（`requires` 依赖校验）。`mode = "fast"` 零落盘运行。

## 当前状态

**库核心框架能力已完成**（18 项）；**库自身主线已完成**（2026-08-22）：打包接线、module-user-store 全系列（store 家目录 / 配置回退链 / 统一枚举 run / CLI 管理面）、独立线（嵌入式验证 demo + stdlib 可视化 feed）；0.1.1（2026-08-23）init 脚手架修复 + git 来源安装完善。待做：M2 实践线、收口 API 稳定化、生态项目（TUI/MCP/Web）。完整进度与路线图见 [docs/dev/progress/module-roadmap.md](docs/dev/progress/module-roadmap.md)（内部文档）。

## 开发原则

- **tickflow 零修改（有条件的）** — tickflow 是外部依赖（PyPI 包 `tickflow-py`，import 名 `tickflow`，上游仓库 https://github.com/MountLynx/tickflow-），仓库内无 tickflow 代码。修改前先判断：改动是否有普适性、是否真正有助于优化 tickflow 本身？**没有 → 不碰**（模块层功能一律通过 `Registry` 子类扩展）；**有 → 在上游改**，发布新版 `tickflow-py` 并升级安装版本
- **两级用户定位** — 框架服务两类用户：**开发者用户**（写 module 并发布）与**使用者用户**（只写 spec/tasklist）。边界不硬——开发者也是使用者，使用者也能按需修改。本质是两个使用场景（**开发场景** vs **使用场景**）
- **完全掌控** — 无隐式行为，promptmode 选错直接 KeyError，框架不兜底

## 许可证

MIT
