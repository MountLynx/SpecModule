# SpecModule 概念说明

概念层文档：解释框架**为什么这样设计**、各概念**如何配合**。动手路径见 [`guides/tutorial-first-module.md`](../guides/tutorial-first-module.md)（从零到第一个模块）；语法面见 [`references/spec-harness-syntax.md`](../references/spec-harness-syntax.md)；执行语义见 [`references/tickflow-integration.md`](../references/tickflow-integration.md)。

## 模块基本结构：四个概念

一个 module 是四个概念的组合：

```
spec（想要什么）──► tasklist（如何做）──► 执行元件（如何执行）
        │                                      │
        └──────── spec 与 tasklist 一致性审核 ──┘
```

| 概念 | 定义 | 说明 |
|------|------|------|
| **spec** | 内容约束，"想要什么" | 任意结构化键值对，无预定义 schema——字段含义由模板设计者定义 |
| **tasklist** | 流程控制，"如何做" | `{Tasks, Flow}`；注意：tasklist 是 spec 在执行层面上的**细粒度翻译**，不是简单的 task 拼接排布 |
| **harness** | LLM 调用执行元件 | prompt（三层）+ 输出格式校验 + 流式 token |
| **script** | 纯 Python 函数执行元件 | 模块内部处理；部分 script 实际起到通常 Agent 中 tool 的作用 |
| **submodule** | 固定的"箱子" | tasklist 固定、spec 强模板化的 module，特定输入得到特定输出 |

## 两种输入模式

spec 和 tasklist 都是受支持的输入：

| 模式 | 输入 | 流程 |
|------|------|------|
| **spec-only（翻译通道）** | 只传 spec | 模板把 spec 翻译为 tasklist（翻译器见下），然后执行；要求 spec 足够详细 |
| **spec + tasklist（直写通道）** | 两者都传 | 内部一致性审核通过后，spec 作为目标约束，用于判断执行是否偏移目标——此时 spec 可以简略 |

**选择依据**：流程固定/可预测 → 直写 tasklist（确定性、零翻译成本）；流程由 spec 内容驱动/多变 → 模板翻译。两种模式是同一事物的两种封装，不是"固定 vs 动态"的对立。

### 模板翻译通道

`TasklistTemplate` 是一枚硬币的两面：`translation` 声明"由谁翻译"，`tasklist` 字段定义"翻译成什么样的特定流程"。

| 翻译器类型 | 语义 | 适用 |
|-----------|------|------|
| `type: "harness"`（LLM） | 读 spec + 翻译 prompt，生成 tasklist JSON | spec 驱动、流程由 LLM 设计的场景（内置 translate/summarize/codereview/docwrite 模板） |
| `type: "script"`（确定性） | 直接调用已注册 script 函数，返回 tasklist dict | 固定流水线的多形态封装——零 LLM 成本、流程稳定 |

## 对齐检查

每 n 个 tick 后使用 LLM 对当前状态（主要是 history 中的输出内容）与 spec 进行对齐判断，判定为偏离则截断提醒。对齐检查与一致性审核不同：**一致性审核**在运行前检查 spec↔tasklist 语义一致（`ConsistencyError` 阻塞）；**对齐检查**在运行中检查产出↔spec 目标（内置 `align_check` 节点，输出对齐/偏离分析 + 建议）。

## 状态记录、快照与回滚

一个 module 的运行是一个进程，全程记录。状态记录以最终实现前端可视化展示与监控为准。

- **快照/回滚以 tick 为粒度**：恢复到某个 tick 的全局字典和布尔值表状态；回滚时可以调整 spec 和 tasklist 中未执行的部分。
- **持久化约定**：默认每次 `Module.run` 在 `<工作目录>/.specmodule/runs/<module_id>/run.sqlite` 生成独立 SQLite 数据库（run_id = module_id；SubModule 每次 run() 生成 `{name}_{uuid[:6]}`，互不干扰）。`Module(persist=False)` 或 `SubModule mode="fast"` 关闭落盘（全内存快速模式，无 `.specmodule` 残留）。
- **敏感数据注意**：默认落盘意味着 LLM 产出（代码、prompt）持久化到工作目录——`persist=False` 即关闭开关。
- **跨进程查询**：`status.json`（阶段机：idle → translating → reviewing → … → done）+ `run.sqlite` 最新快照（tick 级：status/tick/fireable/fired + 每节点最新输出），任何进程可查，不依赖 Module 实例（`query_run_status`）。

## 嵌入的两种含义（易混，先厘清）

"嵌入"在文档里指两件独立的事：

| 含义 | 指什么 | 关键点 |
|------|--------|--------|
| **① SubModule 嵌入模式** | 运行形态：`audit=False` 内存不保留审计，但记录仍落盘（`.specmodule/runs/<run_id>/run.sqlite`，完整历史可查）；轻量任务用 `mode = "fast"` 全内存零落盘 | 与 `keep_records` 正交；形成固定"箱子" |
| **② 宿主项目嵌入** | 外部系统把 SpecModule 当库 import（服务 / 插件 / Web 后端） | 见 [`guides/embedding.md`](../guides/embedding.md)；生态形态（TUI/MCP/Web）为独立仓库 |

## harness：三层 prompt

harness 需要指定 LLM 的供应商、模型名和模型相关设置（温度、think 等），兼顾"专一方向"的定制化特用性和应对各种情况的灵活性。

| 层 | 来源 | 作用 |
|----|------|------|
| **prompt_core**（Layer 1） | 写死模板 | 必要能力提示词：是什么、要做什么（只含必要的），仅部分关键词可替换 |
| **prompt_modes**（Layer 2） | 动态注入 | 针对不同情况的选择性注入（如"正式/随意"风格），不是全部注入 |
| **prompt_extra**（Layer 3） | 人工注入 | 除前二者关键词替换之外的部分 |

Layer 2 键不匹配抛 `KeyError`——**无隐式行为，框架不兜底**。

## 为什么 script 代替 tool（设计理念）

一般认为 agent 需要为 LLM 配置 tool；module_harness 中这部分放进 script，单独一个节点。目的：

- 一个节点是任务的一个最小单元，不需要 LLM 进行调用 tool 的循环；
- 减轻 LLM 的上下文压力——无 toollist 和 tool 介绍的注入；
- 奉行**模型能力无关**——使一些没有为工具调用优化过的模型也能用。

## 事件系统

EventBus 提供两层事件——流程级（tickflow hooks：`on_fire` / `on_tick_start` / `on_tick_end`，`node_status_changed` / `tick_started` / `tick_completed`）和节点内部事件（EventBus：prompt 渲染、token 流、命令执行、校验结果）。完整事件类型清单与订阅模式见 `module_harness/infra/events.py`（类型化 dataclass：harness 6 + script 3 + command 3 + 一致性审核 1）。事件与审计解耦：`keep_records=False` 时事件照常可达。

## 设计原则

- **完全掌控**——无隐式行为：promptmode 选错直接 KeyError；空 `output_format` 配 `json_object` 类型报校验错误。框架不猜。
- **审计即设计**——所有状态记录在 RunState 中，快照与回滚是内置能力。
- **命名空间隔离**——多个 Module 同进程共存，body 以 `{module_id}:{key}` 前缀隔离注册。
- **SDK 先行**——新功能实现前先设计所需的数据查询接口，消费形态（CLI/agent/Web）只是 SDK 的薄封装。
