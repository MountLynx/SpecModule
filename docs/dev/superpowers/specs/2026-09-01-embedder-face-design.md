# 嵌入者消费面设计 — task 级 API 地板 `call_harness` + 分层红线论证

> 日期：2026-09-01
> 状态：设计已确认（brainstorm 三分叉：完整嵌入者面 / 显式注入 / 自由函数），待实施
> 关联：`docs/dev/progress/module-roadmap.md`「三种消费形式」章节（嵌入者面三个 🔜 checkbox）；
> `openspec/changes/archive/2026-08-25-decouple-embed-events-from-records`（宿主事件语义，已完成一半）；
> `docs/guides/embedding.md`（现有图级嵌入指南，本文扩展之）

## 0. 缘起与边界

roadmap「三种消费形式」章节确认了嵌入者消费形式长期缺位：嵌入者消费的价值单位是
**一次 LLM 任务调用**（其次才是嵌进宿主进程的图），而现有 API 金字塔
（tasklist → graph → run）底下没有 task 级地板。症状：`Harness` docstring
"用户不直接使用"；`ConsistencyReviewer` 手工内联独立调用全套仪式代码；
`embed_minimal` 一跑即暴露 `register_builtin_harnesses` 未导出。

本设计覆盖嵌入者面全部三个待做项：`call_harness` task 级地板、嵌入者 import 契约、
嵌入指南补全；并给出「分层方向别反」红线的完整论证（从规定变成推导）。

## 1. 推演：嵌入者使用模型

### 1.1 三个真实场景（从使用倒推 API）

| 场景 | 价值单位 | 要什么 | 现状 |
|------|---------|--------|------|
| S1 宿主单次 LLM 任务（翻译/抽取/审核） | 一次函数调用 | HarnessConfig 复用、输出校验提取、raw/usage、事件可选 | **无 API**（唯一缺口） |
| S2 宿主进程内嵌完整 workflow | 一次 run | Module persist=False 全内存 | 已有（embedding.md + embed_minimal） |
| S3 框架内部当嵌入者（ConsistencyReviewer） | 一次函数调用 | 同 S1 | 手搓 DictView 仪式代码 |

### 1.2 API 金字塔补全

现有三层 tasklist → graph → run，S1 缺金字塔底的 **task 层地板**。`call_harness`
落位后金字塔为 **task → graph → run**，三种消费形式各取一层：

- **嵌入者**：主要消费 task 层；需要运行期保证时爬到 graph 层（S2）
- **作者**：消费 tasklist / 模板 / 翻译
- **运行者**：消费 run（CLI / store / query）

### 1.3 保证边界（核心结论）

- task 层**得到**的：三层 prompt、输出校验与自动提取、raw/usage、可选事件流
  ——全部是 harness 节点**已有**的执行语义，`call_harness` 零新语义。
- task 层**得不到**的：审计落盘、快照回滚、失败隔离（Failure → 下游跳过是**图**概念）、
  断点续跑——这些是 run 级保证。嵌入者要它们时**往上爬一层建图**，不在函数里重建。

「task 级地板不许长成迷你引擎」由此不是禁令而是**分层保证的纯度**：每层的保证不同，
混了就两层都不纯。

## 2. 分层红线论证

**命题**：函数住 module_harness，调用方是应用层/模块层；基础库若想 import 它即
分层警报——该 LLM 调用应上移（script 节点或应用层），而非底层反向依赖顶层。

1. **依赖顺序即高度**。库内分层严格单向（tickflow ← llm ← module_harness ← 宿主），
   module_harness 是库内顶层——任何 import 它的代码按定义位于它之上。
   "上下"不是风格判断，是 import 箭头方向的事实。

2. **底层 import 顶层的三个实际代价**（不是洁癖）：
   - **依赖传染**：基础库的每个消费者被迫拖入 module_harness + tickflow + llm 全家——
     引文库的使用者只想解析 BibTeX，却装进一个 workflow 引擎。
   - **环的风险**：若上层将来反向需要基础库，唯一解只剩循环导入。
   - **演化绑架**：call_harness 进 API 冻结面后，基础库的演化节奏被上层契约锁死。

3. **为什么"上移"总是可行**：LLM 调用的本质是"数据进 → 数据出"。基础库保持
   **纯机制**（纯函数），LLM 编排是**策略**，策略住上层。两个上移落点：
   - 应用层直接 `call_harness`（嵌入者场景）；
   - 包成 **script 节点**：script 调基础库处理 + harness 调 LLM，由图编排（module 场景）。

   上移不是惩罚：恰恰因为 LLM 调用要享受 run 级保证（审计/快照/失败隔离）时**必须进图**，
   把它放上层让"进图"永远可达——不存在"底层已经调了 LLM、想进图要重写"的死路。

4. **判定口诀**（机制化，与规则 6 提炼判定并列）：**看 import 箭头——高层 import
   低层永远合法；箭头向上即警报**。call_harness 本身不违反此线（它是顶层向外提供的
   服务面），违反的是"llm/ 或宿主基础库 import module_harness"。

**Originating case**（学术写作实践线）：引文管理/数据管理库是宿主侧基础库——纯 Python
（解析、去重、排序、存储）；"从图表提取 claims"这类 LLM 步骤住应用层，应用层左手
import 引文库（拿数据）、右手 import specmodule（调 LLM），两者在应用层汇合。
**引文库永远不知道 specmodule 存在。**

## 3. API 设计 — `module_harness/call.py`（~60 行）

```python
@dataclass
class HarnessCallResult:
    value: Any      # 校验后的输出（json_object → dict；text → str）
    raw: str        # LLM 原始输出（审计链用）
    usage: dict     # token 用量

class HarnessCallError(RuntimeError):
    """LLM 错误 / 输出不合法。携带诊断链（无审计轨迹，异常即审计）。"""
    failure: Failure | None   # tickflow Failure 原件（LLMError 路径 type="infrastructure"）
    prompt: str | None        # 渲染后的完整 prompt
    raw: str | None
    usage: dict | None

async def call_harness(
    config: HarnessConfig,
    values: dict[str, Any],
    *,
    llm_client: Any,                     # 显式必传
    promptmode: str | None = None,
    prompt_extra: str | None = None,
    event_bus: EventBus | None = None,   # 缺省 EventBus.null()，零开销
) -> HarnessCallResult: ...
```

语义细节（每条都是决策，不是顺手）：

1. **零新执行语义**：内部即 `Harness(config, llm_client, bus).build_body(
   promptmode=..., prompt_extra=...)` + 一次 body 调用。节点行为的任何未来演化
   （notdo、api_params 等）自动惠及 task 层——只有一份执行配方。
2. **values → view**：`DictView({k: Resolved(value=v, k=None) for ...}, state={},
   node="__call__")`。`__call__` 是保留字面量，与 ConsistencyReviewer 的
   `__review__` 同族。body 写进局部 state 的 `_prompt/_llm_raw/_usage` 调完即捞进
   结果/异常，state 随手丢弃——嵌入者磁盘零残留，但诊断链完整。
3. **Failure → HarnessCallError（两种 Failure 同途）**：LLMError（infrastructure）
   与校验失败都返回 Failure；task 层没有"下游跳过"概念，统一翻译成类型化异常。
4. **promptmode 缺 key → KeyError 原样冒出**：与节点内行为一致，框架不猜。
5. **spec_inputs / input_aliases 不进 task 层**：它们是图概念（spec 常量注入是
   TasklistTranslator 的事，跨节点别名需要 view 里有其他节点）。task 层的占位符
   兜底就是 values 本身——"不许长成迷你引擎"在签名上的体现。
6. **事件可选**：传 bus 收 PromptRendered / LlmToken / ... 全套；不传零开销
   （`EventBus.null()` 已存在，events.py）。
7. **同步包装不做**：宿主 `asyncio.run(call_harness(...))` 一行即可；事件与流式
   回调本就是 async 语义。

## 4. ConsistencyReviewer 瘦身

- `HarnessRegistry` 加只读属性 `llm_client`（瘦身的硬前提，一行）。
- `review()` 重写：注册校验不变 → `call_harness(reg.harness_config(name),
  {"spec": ..., "tasklist": ...}, llm_client=reg.llm_client)` → 组
  `ConsistencyReport`。删掉手搓 DictView、Failure 判断、state 取 `_llm_raw`
  的仪式（~30 行 → ~15 行）。
- **公共行为不变，一个明确的行为增量**：ValueError 语义、`ConsistencyReport` 结构、
  module.py 的 `ConsistencyError` 链与 `ConsistencyReviewed` 领域事件
  （module.py 直接发射，不经 reviewer）全不动。增量：review 内部 harness body 的
  中间事件（PromptRendered / LlmCall* / LlmToken / OutputValidated，node=`__review__`）
  不再发到 registry bus——call_harness 不传 bus 即静默（嵌入事件语义：
  不传零开销）。这些中间事件属实现细节，审核结果与领域事件才是契约；
  若既有测试断言了它们，随瘦身更新断言。
- JSON 解析和字段校验**留在 reviewer**——那是 ConsistencyReport 的领域逻辑；
  call_harness 只管 output_format 校验层面。
- 边界声明：审核 harness 按 `register_review_harness` 契约只带 config 注册
  （注册期 promptmode/prompt_extra 对审核器无意义）；call_harness 路径不传
  注册期 kwargs——文档声明，不做兼容分支（诚实不猜）。

## 5. 契约与文档

- **`module_harness/__init__.py`**：新增 `HarnessCallResult / HarnessCallError /
  call_harness` 导出；`__all__` 加注释块标注**嵌入者最小面**（call_harness +
  HarnessConfig + OutputFormat + EventBus + Module/HarnessRegistry +
  TemplateLoader + register_builtin_harnesses）。不引入 `EMBEDDER_FACE` 之类的机器——注释 + 指南
  就是契约，正式冻结归 API 稳定化收口。
- **`docs/guides/embedding.md`** 补两节：「task 级调用」（call_harness 示例 +
  asyncio.run 配方）、「嵌入者分层纪律」（红线压缩版 + import 箭头口诀）。
- **roadmap 回写**：嵌入者面三个 checkbox 勾掉；红线条目加"论证详见本 spec"指针。

## 6. 测试

`test_call.py`（mock llm client，沿用 conftest 既有 fixture 风格）：

- text 输出 → value == raw
- json_object 输出 → value 是 dict、raw 是原文
- 校验失败 → HarnessCallError，prompt/raw 挂载完整
- LLMError → HarnessCallError，failure.type == "infrastructure"
- promptmode 命中正常渲染 / 缺失 key → KeyError
- event_bus 传（收到事件）/ 不传（null 不炸）
- reviewer 既有测试全绿（公共行为不变的证明）
- embed_minimal 加 task 级 `--mock` 冒烟段

## 7. 不做清单 ⏸️

同步包装、重试/缓存、`call_harness_env` 便利变体、registry 挂载（`reg.call`）、
轻可调用对象（`HarnessCall` 实例）、`EMBEDDER_FACE` 机器、一切迷你引擎功能
（条件分支/落盘/失败隔离）。每个都被 brainstorm 三分叉明确否决或由保证边界（§1.3）排除。

## 8. 决策记录

| 分叉 | 决策 | 理由 |
|------|------|------|
| 产出边界 | 完整嵌入者面（推演 + 构建） | roadmap 三个 checkbox 一次收掉 |
| llm_client 注入 | 显式必传 | 与 HarnessRegistry/Module 全表面一致；库不隐式读环境（"框架不猜"）；一行宿主配方由指南承担 |
| API 形态 | 自由函数 | 嵌入者不需要 registry（图时代仪式）；冻结面最小；与"最小价值单位是一次函数调用"定位一致 |
