# TreeChat 对话引擎设计 — 对话树一等公民 + 卡片式上下文产出

> 日期：2026-09-06
> 状态：设计已确认（brainstorm 四轮定位澄清 + nanobot 参考修订），待实施
> 交付物：**独立新仓库**的 Python 库 + 薄 REPL CLI（库名 `treechat` 为占位，可改）
> 关联：
> - `docs/dev/progress/module-roadmap.md`「三种消费形式」——本项目是**嵌入者消费形式**的实践设计：宿主（TreeChat）拥有对话逻辑，SpecModule 是依赖
> - `docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md`——本项目消费其产物 `call_harness`
> - 参考项目：nanobot（`开发/参考/nanobot`）——chat 机制借鉴对照见 §9

## 0. 缘起与边界

### 0.1 需求

与 LLM 连续对话时，两类痛点没有产品以**一等公民**解决：

1. **分支对话**：对 AI 提的概念性追问、对 AI 简略描述的深挖，会以无用信息污染上下文；现行产品里"回到上一处开分支/新开对话"是附加功能，没有对话树管理能力。
2. **上下文产出管理**：分支讨论常产出对主干有用的结论，但中间过程不需要——回主干后要手工复述；superpowers 式工作流（brainstorm→spec→plan→execute）每阶段只有最新产物是有效上下文；连续处理文档时最终汇总只需要每次的整理输出。这些指向同一个机制：**把"分支对话的产出"沉淀为可引用的卡片**。

### 0.2 三个动机场景 = 设计验证基准（非特化目标）

| 场景 | 在本设计中的对应 |
|---|---|
| superpowers 式流程（brainstorm→spec→plan→execute，只有最新产物有效） | 分支产出 spec 卡片 → 切换/回主干 pin 该卡片——通用卡片机制的自然结果 |
| 分支讨论回灌主干（中间过程不需要） | `/card` 提炼分支段 → `card_create` 默认 pinned → 主干轮次自动注入 |
| 连续文档处理（只留每次输出） | 编程 API 驱动循环 turn，每轮产出 `card_create`——无需历史文档入上下文 |

**判定标准**：这三个场景必须作为通用机制（分支 + 卡片引用）的自然结果成立；若设计里出现为任一场景特化的字段/命令，即为设计错误信号。

### 0.3 边界

- **不做**：富交互 TUI/Web（未来独立项目）、工具调用/agent 循环（agent 框架领地）、媒体消息（V1 纯文本）、跨用户/多端同步。
- **关键路径上不修改 SpecModule**：SpecModule 侧 issue #1（跨 session 快照派生分支）不在本项目关键路径（见 §1.2 关键修正）。

## 1. 关键决策记录（brainstorm 结论）

| 决策点 | 选定 | 备选与否决理由 |
|---|---|---|
| 项目定位 | 独立项目，SpecModule 是依赖（嵌入者） | 做在 SpecModule 库内——否决：对话树不是 Petri 网运行时的事；改库走 issue #1 阻塞且方向不对 |
| SpecModule 依赖深度 | `llm` 客户端 + `call_harness` | 仅 `llm`——卡片格式/校验纪律要自己重建；深度绑定 Module/tickflow——spec 驱动一次性 run 与交互式对话阻抗大，且每轮走图引擎过重 |
| 交付形态 | 库 + 薄 REPL CLI | 纯库——日用诉求要等消费方；库+TUI——范围重，库面验证被拖慢 |
| 核心架构 | **事件溯源消息树**（JSONL 追加日志） | SQLite 关系模型——主操作是"追加+按路径取"，查询优势用不上；内存+整树快照——崩溃丢尾、无审计、全量重写 |

### 1.2 关键修正（相对最初可行性判断）

最初设想"对话树长在 SpecModule run 体系里 → 分支需要 fork API（issue #1）"。定位澄清后修正：**对话的本质是追加式消息树**——分支 = 在任意历史节点挂新消息（parent 指针），分支上下文 = 根到该节点路径，成本为零，**不需要快照/回滚机制**。"谁最长谁是主干"是纯视图规则。SpecModule 在本项目中只承担 LLM 调用层，不承担树/分支语义。

## 2. 核心模型：事件溯源消息树

### 2.1 持久层

一个会话 = 一个 JSONL 事件文件（`<data_dir>/sessions/<name>.jsonl`，`data_dir` 默认 `~/.treechat`，可配置）。**只追加**；追加后 fsync。

事件类型共 5 种（刻意少；`seq` 显式写入，等于行序号，加载时校验连续性）：

```jsonl
{"seq":1,"type":"session_meta","name":"specmodule-branching","created_at":"...","system":"会话级 system 指令"}
{"seq":2,"type":"user_msg","parent":null,"text":"讨论 TreeChat 的分支语义"}        ← 首条 = 会话根
{"seq":3,"type":"assistant_msg","parent":2,"text":"...","meta":{"model":"...","usage":{...}}}
{"seq":4,"type":"user_msg","parent":2,"text":"回到第 2 条，追问一个概念"}          ← parent 指向历史 = 分支
{"seq":5,"type":"user_msg","parent":null,"text":"（无上下文的概念提问）"}          ← 非首条 parent=null = 叶子链
{"seq":6,"type":"card_create","card":{"id":"card_a1b2","title":"...","body":"..."},"from_path":[2,3],"instruction":"..."}
{"seq":7,"type":"pin","card_id":"card_a1b2"}
{"seq":8,"type":"unpin","card_id":"card_a1b2"}
```

**分支/叶子语义就藏在 parent 指针里**，无独立 branch 事件：

- `user_msg.parent` = 当前指针 → 顺延对话
- `user_msg.parent` = 任意历史节点 → **从该点长出新枝**（零复制：上下文即根到该节点路径）
- `user_msg.parent` = null（非首条）→ **叶子**：无上下文新消息链，仍归属本会话管理
- 概念上"分支"操作 = 把指针挪到历史节点后输入（CLI 的 `/branch`），数据层只是 parent 取值

### 2.2 持久时序（nanobot 教训：用户输入先落盘）

`turn(text)` 的持久顺序：

1. 追加 `user_msg` 事件（parent=指针；leaf 模式则 null）+ fsync
2. 组装上下文 → 调 LLM（流式）
3. **成功** → 追加 `assistant_msg`（parent=该 user 节点）+ fsync，指针推进到新 assistant 节点
4. **失败** → 不再追加。留下**悬而未答的用户节点**——对树是诚实状态（"问了没答上"），`/tree` 可见；重试在该节点下补 `assistant_msg`，**问题不丢、不重复**

### 2.3 文件完整性

- **撕裂尾**：末行 JSON 不完整（崩溃时正在写）→ 加载时**警告并忽略该行**
- **中间损坏**：非末行解析失败 → **硬报错并给行号**，不静默跳过
- `seq` 不连续 → 硬报错；未知 `type` → 硬报错（版本前向兼容到需要时再做，不猜）

### 2.4 内存态（`Conversation`：日志 + 重放派生视图）

加载 = 顺序重放事件，派生：

- **节点表**：`seq → MsgNode{seq, parent, role, text, meta}`（消息 id = seq，稳定可引用）
- **子节点索引**：父 → 有序子列表（`/tree` 用）
- **活跃指针**：当前对话位置（下一条 `user_msg` 的 parent）
- **卡片注册表**：`card_id → Card` + pinned 状态

视图函数（纯函数，树上不存储主干标记）：

- `path_to(seq)` → 根到该节点的消息列表（**即该分支的完整上下文**）
- `trunk()` → 所有根→叶路径中**最长者**（按节点数；平局取末端 seq 最大者）；`trunk_end()` 为其末端——"无主干、谁最长谁是主干"由此成立
- `fork_point(seq)` → 路径上最后一个拥有 ≥2 子节点的祖先（卡片提炼默认范围用）

## 3. 卡片系统

### 3.1 Card 结构

```python
@dataclass
class Card:
    id: str            # "card_" + 4 位 hex
    title: str
    body: str          # markdown
    # 以下随 card_create 事件记录
    from_path: list[int]   # 提炼来源消息 seq 路径
    instruction: str       # 用户提炼指令
    created_at: str
```

**body 自包含是硬要求**：脱离原分支也能读懂（提炼 prompt 中显式约束：去对话语气、保留事实/决策/结论、不指代"上面/刚才"）。

### 3.2 提炼 = 一次 `call_harness`

卡片格式可靠性来自 SpecModule 的结构化调用 + 输出校验（不自写解析）：

- `HarnessConfig`：`output_format="json_object"`；`prompt_core` = 提炼模板（输入 `{transcript}` 分支转录 + `{instruction}` 用户指令，输出 `{title, body}`，含自包含约束）；`prompt_modes` = 单一 `default` 模式（满足框架"缺 key 即 KeyError"的显式性约定）；V1 内置一份，随实践按 SpecModule 内置工具提炼机制决定是否上收
- 默认提炼范围：**当前分支段** = `fork_point(pointer)` 之后到指针的消息；`/card all` 全路径；`/card <a>-<b> [指令]` 显式区间
- 产出 → 追加 `card_create` 事件（含 from_path/instruction 审计信息）；**不移动指针**

### 3.3 引用与生命周期

- `card_create` **默认 pinned**（"分支产出回灌主干"是主流场景）；`/unpin <id>` 移除、`/pin <id>` 恢复
- pinned 卡片在每次轮次组装时注入 system 区，格式：

  ```
  [参考卡片 card_a1b2: 标题]
  body…
  ```

- `/card export <id> <file>` 导出 markdown（跨会话复用走导出文件；import 与跨会话卡库列后续，见 §10）

## 4. 上下文组装与轮次引擎

### 4.1 组装（`context.py`，策略可插拔）

一次 LLM 调用的消息序列：

1. **system**：会话级 system 指令（存于 `session_meta`，`/system` 查看/修改）+ pinned 卡片块（§3.3 格式）
2. **history**：`path_to(本条 user 节点)` 去掉末条（= 到其父节点为止的路径；叶子分支 parent=null，history 为空）（历史与当前消息分离，参考 nanobot `TranscriptInput`）
3. **current**：本条 user 消息

**V1 窗口策略**（`WindowStrategy` 协议，默认简单实现）：token 预算（默认按 chars/4 估算，预估器可换）超限时——system 与卡片**整块保留**，history 从最新往回装填，装不下的最旧前缀**丢弃并在 system 末尾注入显式警示**（"（更早 N 条消息因预算未纳入）"）。不静默。

**V2（路线）**：自动摘要卡片压缩——某分支路径前缀被丢弃时，自动产出摘要卡片 + 追加 `compact {up_to_seq, card_id}` 事件，组装器遇 compact 标记用卡片内容替代已消费前缀。**卡片机制即压缩机制**（nanobot 用独立纯文本 `_last_summary` 基建；我们的差异点是结构化卡片复用同一套提炼/引用/生命周期）。原始事件永不物理删除。

### 4.2 LLM 桥

- 客户端：`llm.create_llm_client()`（SpecModule env 驱动：`LLM_PROVIDER`/`LLM_MODEL`/key 等），`chat(messages)` 多轮接口 + 流式
- `/model` 会话内临时覆盖（只改本次会话运行时，不写 env）
- 卡片提炼：`module_harness.call_harness(config, values)`（`values = {"transcript": ..., "instruction": ...}`）

## 5. 薄 CLI（REPL）

V1 **纯 stdlib**（`input()` + 逐行流式打印），沿用 SpecModule 零依赖 CLI 哲学；prompt_toolkit/Rich 列为首个依赖升级位（§10）。

非交互：`treechat new <名> [--system "..."]` / `treechat open <名>` / `treechat list`。

REPL 命令：

| 命令 | 作用 |
|---|---|
| 裸输入 | 对当前指针说话（流式回显） |
| `/tree` | ASCII 树视图：`*` 当前指针、分支/叶子可见、卡片来源节点标注 |
| `/branch <seq>` | 指针挪到历史节点 → 下一条输入长新枝 |
| `/trunk` | 指针跳回主干末端（`trunk_end()`） |
| `/leaf` | 下一条输入为无上下文叶子提问 |
| `/card [all\|<a>-<b>\|指令]` | 提炼为卡片（默认当前分支段；首个参数为 all/区间时其余为指令，否则全部为指令） |
| `/cards`；`/card show <id>`；`/pin\|/unpin <id>` | 卡片列表/查看/pin |
| `/card export <id> <file>` | 导出 markdown |
| `/where` | 当前位置（指针 seq、所在分支） |
| `/system [新指令]` | 查看/修改会话级 system |
| `/model [名]` | 查看/临时切换模型 |
| `/help`；`/quit` | |

`/tree` 示意：

```
#12 ◆ 主干（最长）
 ├─ #3 ── #5 ── #7*        ← 分支 A
 │      └─ #9 ── #11 [card_a1b2]   ← 分支 B（已产出卡片）
 └─ #14（叶子，无上下文）
```

## 6. 依赖与项目结构

### 6.1 依赖契约（已核实）

- `specmodule>=0.2.0`（wheel 顶层同时分发 `llm*` 与 `module_harness*`，见 pyproject `packages.find`）
- `from llm import create_llm_client, Message, LLMConfig, LLMError, MockLLMClient`（均在 `llm.__all__`）
- `from module_harness import call_harness, HarnessCallResult, HarnessCallError`（嵌入者面，已在 `module_harness.__all__`）；`HarnessConfig` 自 `module_harness.core.config`

### 6.2 分层纪律

**`core/` 零 SpecModule import**——纯模型层（事件/树/卡片/组装），`llm_bridge.py` 是唯一依赖面：测试 mock 这一层即可全库无网测试；未来换 LLM 后端或接 Module 桥只动此层。

```
treechat/
  pyproject.toml            # deps: specmodule；requires-python >=3.10；console_script: treechat
  treechat/
    __init__.py             # __all__: Conversation, Card, TreeChatConfig, TreeChatError, …
    core/
      events.py             # 5 种事件 dataclass + 序列化/校验（seq 连续性、未知 type）
      conversation.py       # Conversation：追加/重放/指针/path_to/trunk/fork_point
      cards.py              # Card + 注册表 + pin 状态
      context.py            # 组装（system+卡片+history/current 分离）+ WindowStrategy
      store.py              # JSONL 追加/fsync/加载（撕裂尾/损坏行策略）
    llm_bridge.py           # 唯一 import specmodule 处：turn 调用 + 卡片提炼封装
    cli/
      repl.py               # REPL 主循环（stdlib）
      commands.py           # 斜杠命令表
  tests/
```

约定沿用本仓库习惯：`from __future__ import annotations`、`@dataclass`（config 用 `field(default_factory=...)`）、公共签名类型注解、`__init__.py` 显式 `__all__`、中文 docstring。

## 7. 错误处理（无隐式行为）

| 情形 | 行为 |
|---|---|
| 事件文件撕裂尾（末行不完整） | 警告并忽略该行 |
| 事件文件中间行损坏 / seq 不连续 / 未知 type | **硬报错 + 行号**，不静默跳过 |
| LLM 调用失败 | 已落 `user_msg` 保留（悬而未答节点），不追加 assistant，指针不动，可重试 |
| 卡片提炼校验失败（`HarnessCallError`） | 原样上抛到 REPL 显示，不猜测补救 |
| 窗口超预算 | 丢最旧前缀 + system 注入显式警示（不静默）；卡片/system 永不因预算被丢 |
| pinned 卡片被 V1 窗口影响 | 不会（卡片整块保留） |

## 8. 测试

沿用本仓库手法（pytest + `unittest.mock`；LLM 侧优先用 `llm.MockLLMClient`）：

- **events/store**：roundtrip（tmp_path 写读一致）、重放幂等（同文件重放两次派生态一致）、撕裂尾容忍、中间损坏/seq 断裂/未知 type 报错
- **conversation**：顺延/分支/叶子三种 parent 语义、指针推进、`path_to`/`trunk`（含平局取最新）/`fork_point`
- **cards**：提炼流程（mock `call_harness` 固定 JSON → `card_create` 事件 + 默认 pinned）、pin/unpin、注入格式
- **context**：窗口策略（超预算丢最旧 + 警示标记；system/卡片保留）、history/current 分离
- **cli**：REPL 脚本化输入 smoke（new→说→branch→card→tree）

## 9. 先行参考：nanobot 对照（chat 机制）

| nanobot 机制 | 处置 | 说明 |
|---|---|---|
| JSONL 三段式文件（metadata/provider_state/消息行）+ 原子写 | **采纳（改造）** | 我们是追加式事件日志：首行 `session_meta` + 追加 fsync；撕裂尾/中间损坏分级处理 |
| 用户消息先持久化再调模型（防丢） | **采纳** | turn 时序（§2.2）；树语义下失败留"悬而未答节点"，比原设计（失败不落事件）更树原生 |
| 水位线式压缩（原始历史永不删除 + 摘要存 metadata） | **采纳哲学，V2 落地** | `compact` 事件 + 卡片替代已消费前缀（§4.1 V2）；卡片机制即压缩机制是我们的差异点 |
| fork-by-user-index（前缀克隆成新会话文件） | **不采纳** | 每分支完整拷贝、无父子引用；我们的 parent 指针树零拷贝、原位切换——spec 级差异点 |
| 模板内容去重（未定制不占 token） | **记入参考** | V1 system 为会话级单段，无模板层；若未来加 bootstrap 文件再借鉴 |
| SOUL.md/USER.md 人格文件、MEMORY.md 长期记忆、Dream 机制 | **不采纳** | agent 人格/记忆基建，超出对话引擎边界（§0.3） |
| prompt_toolkit + Rich Live 流式渲染 | **路线采纳** | V1 stdlib；首个依赖升级位（§10） |
| AgentRunner 工具循环 / checkpoint sidecar | **不采纳** | V1 纯对话无工具；turn = 单次 LLM 调用，无 sidecar 必要 |

## 10. 路线

- **V1（本 spec §2–§8）**：核心模型 + 轮次 + 卡片 + stdlib REPL + 测试；新仓库脚手架（git init / pyproject / CI 可后置）
- **V2**：自动摘要卡片压缩（`compact` 事件 + 分支水位线）；`/card import` 与跨会话卡库
- **后续**：Module 桥（卡片 → spec 输入 → SpecModule Module run——届时才回头评估 SpecModule 侧改动，如 issue #1 fork API）；富 TUI（独立项目）；prompt_toolkit/Rich 化 REPL；媒体消息（占位降级持久化，参考 nanobot 图片降级）；token 精确计费统计（用 provider 返回 usage 替代 chars/4 估算）
