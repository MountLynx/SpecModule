# call_harness 节点内透传设计 — ppt_master LLM 全链进审计

> 日期：2026-10-01
> 状态：设计已确认（brainstorm 三案：in-node 透传 / 单调用原生化+透传 /
> 框架 receipt 模式——取 in-node 透传，用户选定）；设计五节经用户逐节确认
> 关联：`module_harness/core/call.py`（API 地板）、
> `example/ppt_master/llm_nodes.py`（六工厂）、
> `docs/dev/superpowers/specs/2026-09-15-ppt-master-module-design.md`（上位设计）

## 0. 背景与问题

ppt_master 的六个 LLM 节点（Plan / Research / Page×N / Repair / Notes /
ImageAcquire）以 **script 包 `call_harness`** 的形态注册——理由成立且不变：
harness body 返回 `Failure` 会让所有出边写 False、AND-join 饿死
（tickflow engine.py Phase B 语义），页节点必须"永不 Failure"，LLM 错误
降级为失败收据由质量门判定。

但包装引入了两处**审计断点**（均在 `call_harness` 的合成视图）：

1. **事件归属丢失。** `call_harness` 自建
   `NodeView(node="__call__", state={})`，harness body 发的全部事件
   （PromptRendered / LlmCallStarted / LlmToken / LlmCallCompleted /
   OutputValidated / HarnessFailed）`node` 均为 `"__call__"`——persist=True
   时 stream.log 的 call_start/token/thinking/call_end/image_saved/call_error
   六类记录**无法区分属于哪个节点**。
2. **LLM 链路不进记录。** harness body 写入 state 的
   `_prompt` / `_llm_raw` / `_usage` / `_llm_error` /
   `_validation_attempts` / `_validation_retry_errors` 落进一次性局部
   dict，不进真实 `NodeState.mutable_state` → records / 持久化 /
   查询层全空。`llm_nodes.py` 六工厂只取 `result.value`，raw/usage
   直接丢弃。

审计管道本身是通的：`query.py` 已支持 `state.<node>.<key>` 查询（含
`_llm_raw` 等调试字段）、`Module._on_stream_event` 把事件按
`event.node` 写入 stream.log、`build_timeline` 读 records——断的只是
"script 包一层"处的视图替换。原生 harness 节点（body 直挂图）这些全自动
可见。

## 1. 目标与非目标

**目标**：ppt_master 的 LLM 调用全链（渲染 prompt / 原始输出 / token
用量 / 校验重试轨迹 / 生命周期事件）按**真实节点归属**进入审计三层
（stream.log 事件、NodeState → records → 持久化、`specmodule review` /
feed 查询层）。

**非目标**（YAGNI，逐条有因）：

- tasklist 里 LLM 任务仍为 script 型——task 级 model/temperature/
  validate_retries 覆盖不生效（仍走 `prompts_config.py` 注册期硬编码）。
  原生化的增量价值与本目标（审计可见性）无关，见 §2 方案二。
- 图可视化上 LLM 节点显示为 harness 类型——同上，非本目标。
- harness body 事件 `tick=0` 是框架既有行为（原生 harness 节点同样
  `tick=0`，见 `harness.py` 全部事件构造点），不属于本设计。
- 不新增 LLM 评审/审查环节（门仍是机械化 checker + 收据裁决）。
- 不动 tickflow（无上游改动、无发版依赖）。

## 2. 方案决策链

| 案 | 内容 | 取舍 |
|---|---|---|
| **一：in-node 透传（采纳）** | `call_harness` 加可选 `view=`；六工厂传真实视图 | 100% 达成目标；零语义回归；改动集中两文件 |
| 二：单调用原生化+透传 | Plan/Research/Notes/Page×N 声明为原生 harness 任务 + 落盘 script；多调用节点走案一 | 失败语义回归（LLM 瞬断从"失败收据→修复环重画"变 ABORTED 停图，修复环自愈失效）；Repair（按页多调用）/ImageAcquire（运行期行数）结构上无法单任务化；两种模式并存；门 OR-join 语义与既有测试大改 |
| 三：框架 receipt 模式 | `HarnessConfig` 加失败收据模式（module_harness core 改动），页节点原生且保自愈 | 框架 Failure 契约出第二形态，影响所有 harness 使用方；Repair/ImageAcquire 仍透传；改动面最大 |

取案一：用户痛点是"LLM 看不到"，案一直接命中；案二为显示效果付出
自愈回归，不值得；案三的框架语义扩张无第二消费方（架构规则 6）。

## 3. API 形态 — `module_harness/core/call.py`

`call_harness` 增加可选关键字参数 `view: NodeView | None = None`：

- **独立调用形态（缺省 None，现状不变）**：合成视图
  `node="__call__"` + 一次性局部 state。现有独立调用方
  （`orchestrate/consistency.py:87` 审核 harness）零改动。
  注：`translator._call_harness_translator` 走 `reg.get_body` 直接调用，
  不经 `call_harness`，本设计不涉。
- **节点内形态（传 view）**：构造**混合视图**——
  - `node` 取 `view.node`（引擎传入的图节点裸 key，如 `"P01"`；防御性
    剥前缀是 llm_nodes 页 id 派生的既有约定，事件归属用原值，与原生
    harness 节点行为一致）；
  - `state` 取 `view.state`（engine 的 `_NodeStateView`，读写透传、
    底层即记录捕获的 dict；`view.state is None` 的合成视图场景回落
    局部 dict，与 harness body 的 `state is not None` 约定一致）；
  - 占位符解析仍由 `values` 构造合成 fields/resolved——harness body 的
    prompt 占位符语义不变，真实视图的 bind（如页节点的
    plan/calibration）**不**进占位符解析。
- `HarnessCallError` 诊断链（prompt/raw/usage）从 state 读回，两种形态
  统一；`_NodeStateView` 已具备 `get`/`__getitem__`（engine.py:423 实查），
  读回无需适配。
- `HarnessCallResult` 结构不变（value/raw/usage）。

## 4. 六工厂改造 — `example/ppt_master/llm_nodes.py`

- **单调用节点**（plan_node / research_node / page_node / notes_node）：
  `call_harness(...)` 加 `view=view`，完事。harness body 原生写入的
  `_prompt` / `_llm_raw` / `_usage` / `_validation_*` 自动进真实
  NodeState，零额外代码。
- **多调用节点**（repair_node 按失败页循环、image_node 按 ai 行循环）：
  同一节点状态内多次调用互相覆盖标准键。新增模块层累积约定——
  每次调用后向 `view.state["_llm_calls"]` 追加条目：
  `{prompt, raw, usage[, image_path][, error]}`（成功取
  `HarnessCallResult`；失败取 `HarnessCallError` 的诊断链，`error`
  记失败原因；image 模式 raw=None、含 `image_path`）。标准键保持
  last-call-wins（body 原生行为）。累积逻辑放 llm_nodes 本地小助手
  （两处消费），不进框架——架构规则 6，出现第二消费方再提炼。

## 5. 审计效果（三层全通）

| 层 | 现状 | 改后 |
|---|---|---|
| stream.log 事件 | 所有 LLM 事件 `node="__call__"` | `node="P01"` / `"Plan"` / `"Repair"` 真实归属 |
| NodeState → records → 持久化 | 只有收据 dict | 收据 + `_prompt` / `_llm_raw` / `_usage` / `_validation_*`；多调用节点另含 `_llm_calls` 全量（含失败尝试） |
| 查询层（`specmodule review` / feed） | state 查询为空 | `state.<node>._llm_raw` 等现成查询直接可用 |

## 6. 生态影响（SpecModule_webview）

改造落地的 webview 受益分两档（`SpecModule_webview` 为独立消费仓库，
本设计只保证数据面正确，展示面归彼处迭代）：

1. **零改动立即可见**：webview 前端 `ws.ts` 按 `r.node` 索引
   token/thinking 记录、RunView 节点面板已有实时流展示——现状
   ppt_master 的事件全记 `"__call__"`（图上不存在的节点，点击任何真实
   节点均无流）；改后记录带真实节点名，逐节点实时 token/思考流即活。
2. **数据到位、展示待追加**：`GET /api/runs/{id}/status` 载荷已透传
   `node_states`（= mutable state），`_prompt`/`_llm_raw`/`_usage`/
   `_llm_calls` 随载荷可达前端；渲染完整 LLM 链（终态后查看 prompt/
   原始输出全文）需 webview 侧薄层追加（端点或节点面板小节；库侧
   `query_value` 的 `state.<node>.*` 寻址已现成，1:1 包一层即可）。

## 7. 不变式

- **收据结构不变**：六工厂返回的 receipt dict 逐字段不动（被
  example/test_ppt_master_* 大量断言钉死）。
- **"永不 Failure"不变**：`HarnessCallError` 照旧捕获 → 失败收据；
  AND-join 不饿死；修复环轮次上限语义不变。
- **独立调用方不变**：不传 `view` 的调用路径行为逐字节一致。
- **图结构不变**：tasklist / 翻译器 / 门 / 守卫 / OR-join 全不动。
- **tickflow 零依赖**：纯 module_harness + example 改动。

## 8. 测试策略

1. **module_harness 层单测**（新增，`module_harness/tests/`）：
   - 传 `view`：事件 `node` == `view.node`；state 写入调用方提供的
     dict（`_prompt`/`_llm_raw`/`_usage` 落点）；返回 value 不变；
   - 不传 `view`：行为与现状一致（`node="__call__"`、state 为局部
     dict）——回归钉死；
   - 失败路径：`HarnessCallError` 的 prompt/raw/usage 两种形态均可得。
2. **example 层**（扩展现有 mock 测试，`example/test_ppt_master_*`）：
   - mock 视图调 page 工厂 → state 含三标准键、收据结构不变（回归）；
   - repair 两页失败 mock → `_llm_calls` 两条目、失败条目带 `error`；
   - mock 全链跑（现有测试 + 捕获 EventBus——`_build_registry` 已收
     `event_bus` 参数，测试订阅 `LlmCallStarted` 收集）→ 事件流中
     `node` ∈ 真实节点名集合，不含 `"__call__"`。
3. 现有全部测试回归通过（收据断言零改动）。
