# Module Harness & Script 设计文档

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> 日期：2026-06-29 | 状态：已确认，待实现

## 概述

为 SpecModule 实现 harness 和 script 两个执行元件，作为 tickflow 的上层抽象。
两者在 module 设计阶段定义好，tasklist 中按名引用，经翻译层映射为 tickflow graph
的 node.body。

**不包含**：submodule（后续开发，当前不实现）。

## 项目结构

```
SpecModule/
├── tickflow/              # 独立项目，不修改
│   ├── registry.py        # Registry 保持原样
│   └── ...
├── llm/                   # LLM 客户端，不修改（仅 harness 内部引用）
│   ├── client.py
│   └── config.py
│
├── module_harness/        # ★ 新增包
│   ├── __init__.py
│   ├── config.py          # HarnessConfig dataclass
│   ├── harness.py         # Harness 类（配置持有 + body 生成）
│   ├── registry.py        # HarnessRegistry(Registry) ← tickflow Registry 子类
│   ├── prompt.py          # PromptRenderer — 三层 prompt 拼接 + 关键词替换
│   ├── outputfmt.py       # OutputFormat + OutputValidator — 校验 + 自动提取
│   └── events.py          # EventBus + 事件类型定义（harness / script 通用）
│                          # script 注册逻辑内嵌于 registry.py 的 script() 方法
```

### 依赖方向

```
module_harness  →  tickflow   （继承 Registry，生成 Body callable）
module_harness  →  llm        （调用 LLM 客户端）
tickflow        →  （不依赖 module_harness，保持独立）
```

### 关键边界

- `HarnessRegistry` 是 tickflow `Registry` 的子类。tickflow 零修改。
- `HarnessRegistry.get_body(name)` 无需 override——body 在注册时就已构建好，
  存入父类 `_bodies`。Runner 只调 `get_body()`，不感知 harness/script/body 的区别。
- `Harness.build_body()` 返回一个 `async def body(DictView) -> Any`，完全符合 tickflow
  的 `Body` 类型约定。harness body 为 async（LLM 调用本身异步），要求 `AsyncRunner`。
- EventBus 在 tickflow hooks（`on_fire` 等）之外独立运作，不侵入引擎。

---

## 模块设计

### 1. HarnessConfig — 配置数据模型 (`config.py`)

```python
@dataclass
class HarnessConfig:
    """harness 节点的完整配置。

    对标 tasklist 中 Task 定义的字段，翻译层使用 from_task_definition() 直接构造。
    """

    # ── 三层 prompt ──
    prompt_core: str                              # Layer 1：核心提示词，含 {key} 占位符
    prompt_modes: dict[str, str] = field(         # Layer 2：动态 prompt 选项集
        default_factory=dict                      # {"formal": "...", "casual": "..."}
    )

    # ── 输出约束 ──
    output_format: OutputFormat | None = None     # 输出格式约束
    notdo: list[str] = field(default_factory=list)  # 否定性约束列表

    # ── LLM 默认参数（Task 可逐项覆盖）──
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None

    @classmethod
    def from_task_definition(cls, task: dict) -> "HarnessConfig":
        """从 tasklist Task dict 构造配置。预留翻译层入口。"""
        ...
```

**设计决策**：
- `promptmode` 不在 HarnessConfig 中。它属于"本次调用选哪个 Layer 2 变体"，
  由 Task 的 promptmode 字段指定，在 `reg.harness()` 注册时传入。选错了 → KeyError，
  框架不兜底。
- 关键词替换不做显式 mapping。占位符 `{key}` 直接当作 DictView 的 key 查找：
  `view.key.value`。prompt 里写 `{source_text}`，运行时从 `view.source_text.value` 取值。
- `input_keys` 不在 HarnessConfig 中。node 的输入由 graph 的 `node.inputs` 声明管理，
  harness body 通过 `view.xxx.value` 取即可。避免两套输入描述机制。

### 2. OutputFormat + OutputValidator (`outputfmt.py`)

```python
@dataclass
class OutputFormat:
    """输出格式约束定义。"""
    type: Literal["json_object", "json_schema", "text"]
    schema: dict[str, Any] | None = None       # JSON Schema，type="json_schema" 时必填
    instruction: str | None = None             # 自定义格式说明，覆盖默认注入 prompt


class OutputValidator:
    """两步校验：先解析，失败则自动提取；提取再失败 → Failure(type="llm")。"""

    def __init__(self, fmt: OutputFormat) -> None:
        self.fmt = fmt
        self._extractors: list[Callable[[str], str | None]] = [
            _strip_markdown_fences,   # ```json ... ``` → 内容
            _extract_first_json,      # 从文本中切出第一个合法 JSON 块
            _strip_trailing_junk,     # 去尾部非 JSON 字符
        ]

    def prompt_instruction(self) -> str:
        """生成注入到 user prompt 的格式指令。"""

    def validate(self, raw: str) -> Any:
        """返回解析后的值，或 Failure(type="llm")。失败时记录自动修复尝试。"""

    def register_extractor(self, fn: Callable[[str], str | None]) -> None:
        """注册自定义提取策略。"""
```

**三种格式类型的区别**：

| 类型 | 约束 | 校验 | 注入到 prompt |
|------|------|------|--------------|
| `text` | 无约束 | 直接返回原文本 | 无 |
| `json_object` | 合法 JSON 即可 | `json.loads()` | "请输出合法 JSON" |
| `json_schema` | 合法 JSON 且符合 schema | `json.loads()` + `jsonschema.validate()` | "请按此 schema 输出" + schema 文本 |

**校验流程**：

```
raw content
  │
  ▼
validate(raw)
  │
  ├── type="text"       → 直接返回原文本
  │
  ├── type="json_object" → json.loads(raw)
  │      ├── 成功 → 返回解析值
  │      └── JSONDecodeError → 逐个尝试提取器
  │              ├── 某提取器成功 → 返回解析值
  │              └── 全部失败 → Failure(type="llm")
  │
  └── type="json_schema" → json.loads(raw) + jsonschema.validate()
         ├── 全部通过 → 返回解析值
         └── 任一失败 → 提取器尝试 + schema 再校验
                 ├── 成功 → 返回解析值
                 └── 失败 → Failure(type="llm")
```

**内置提取器**：

| 提取器 | 处理内容 |
|--------|---------|
| `_strip_markdown_fences` | 去掉 ```json ... ``` 包裹 |
| `_extract_first_json` | 正则匹配第一个完整 `{...}` 或 `[...]` |
| `_strip_trailing_junk` | 去掉尾部非 JSON 字符后重试 json.loads |

提取器顺序执行，第一个返回非 None 即为成功。

### 3. PromptRenderer — 三层 prompt 渲染 (`prompt.py`)

```python
class PromptRenderer:
    """三层 prompt 拼接 + 关键词替换。

    数据来源：
      Layer 1: config.prompt_core      —— 核心提示词模板，含 {key} 占位符
      Layer 2: config.prompt_modes[pm] —— 由 Task promptmode 选出的动态 prompt
      Layer 3: prompt_extra            —— Task prompt 字段，人工注入部分

    关键词：模板中的 {key} 从 DictView 取值（view.key.value）。
    """

    def __init__(self, config: HarnessConfig) -> None: ...

    def render(
        self,
        view: DictView,
        *,
        promptmode: str | None = None,    # Task 的 promptmode
        prompt_extra: str | None = None,  # Task 的 prompt 字段
    ) -> str:
        """返回渲染完毕的最终 user prompt。"""
```

**渲染流程**：

```
prompt_core           ┐
prompt_modes[mode]    ├─ 三层文本拼接（空层跳过）
prompt_extra          ┘
         ↓
  "{key}" 关键词替换 → view.key.value
         ↓
  返回最终 prompt
```

**注意**：notdo 不在此处理。notdo 在 LLM 调用层通过 `_build_system()` 拼入 system prompt，
保持"否定性约束 → system 层"的语义分离。PromptRenderer 只管 user prompt。

### 4. EventBus (`events.py`)

#### 事件类型

**Harness 事件**（基类 `HarnessEvent`）：

| 事件 | 触发时机 | 关键字段 | 频率 |
|------|---------|---------|------|
| `PromptRendered` | prompt 渲染完成 | `rendered: str` | 每次调用 1 次 |
| `LlmCallStarted` | LLM 调用开始 | `model, prompt_chars` | 每次调用 1 次 |
| `LlmToken` | 流式 token | `chunk: str` | 高频 |
| `LlmCallCompleted` | LLM 调用完成 | `content_chars, usage, finish_reason` | 每次调用 1 次 |
| `OutputValidated` | 校验完成 | `passed, extracted, error` | 每次调用 1 次 |
| `HarnessFailed` | 返回 Failure | `reason, failure_type` | 异常时 |

**Script 事件**（基类 `ScriptEvent`）：

| 事件 | 触发时机 | 关键字段 |
|------|---------|---------|
| `ScriptStarted` | script 开始执行 | — |
| `ScriptCompleted` | 执行成功 | `output_type` |
| `ScriptFailed` | 抛异常 | `error` |

**共同字段**：`timestamp: float`（`time.monotonic()`）、`node: str`、`tick: int`

#### EventBus 本身

```python
class EventBus:
    """同步发布/订阅。回调异常 → 记录日志并吞掉（与 tickflow hooks 一致）。

    支持：subscribe / emit / on 装饰器。
    """

    def subscribe(self, event_type: type, callback: Callable) -> None: ...
    def emit(self, event: HarnessEvent | ScriptEvent) -> None: ...
    def on(self, event_type: type): ...  # 装饰器

    @staticmethod
    def null() -> "EventBus":
        """静默模式：emit 无操作。嵌入式场景使用。"""
```

**设计要点**：
- 与 tickflow hooks 分层。tickflow 的 `on_fire` 在节点结束后触发（拿 `NodeState` 摘要），
  EventBus 在 body 内部运行（拿细粒度过程：prompt 渲染、token 流、校验结果）。
  前端可选监听任一套或两套都听。
- `LlmToken` 高频事件不落盘不存全文，订阅者自行聚合。
- 事件均为纯数据 dataclass，方便后续 JSON 序列化推 IPC/WebSocket。
- EventBus 实例注入 `HarnessRegistry`，一个 bus 服务于一个 Runner 内所有节点。
- 后续可增加事件类型（如 `module.tick_checked`）而不破坏现有结构。

### 5. Harness 类 (`harness.py`)

```python
class Harness:
    """持有 HarnessConfig + LLM 客户端 + EventBus。

    由 HarnessRegistry 管理，用户不直接使用。
    """

    def __init__(
        self,
        config: HarnessConfig,
        llm_client,                     # llm.AnthropicClient | OpenAIClient
        event_bus: EventBus,
    ) -> None: ...

    def build_body(
        self,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
    ):
        """返回一个 async body callable。

        body 执行流程：
          1. PromptRenderer.render(view, promptmode, prompt_extra) → user prompt
          2. emit PromptRendered(node, tick, rendered)
          3. emit LlmCallStarted
          4. await llm.complete(
                 prompt=rendered,
                 system=notdo（通过 _build_system 拼入 system prompt）,
                 output_format=config.output_format,
                 on_token=lambda chunk: emit(LlmToken(chunk)),
                 model=config.model（可被 Task 覆盖）,
                 temperature=config.temperature,
                 think=config.think,
             )
          5. emit LlmCallCompleted
          6. OutputValidator.validate(response.content)
             ├── 通过 → emit OutputValidated(passed=True) → return parsed_value
             └── 失败 → emit OutputValidated(passed=False, extracted=...) → return Failure(type="llm")
          7. 若步骤 4 抛 LLMError → emit HarnessFailed(failure_type="infrastructure")
             → return Failure(type="infrastructure")
        """
```

**设计决策**：
- body 为 **async def**。LLM 调用本身异步，要求 `AsyncRunner`。
  同步图中不含 harness 节点即可。
- `promptmode` 和 `prompt_extra` 在 `build_body()` 时捕获到闭包中，body 执行时无需额外参数。
- 若 LLM 抛 `LLMError`（基础设施故障），映射为 `Failure(type="infrastructure")`，
  与 tickflow 的终止语义一致（Runner → ABORTED）。

### 6. HarnessRegistry (`registry.py`)

```python
class HarnessRegistry(Registry):
    """tickflow Registry 子类。不修改 tickflow 任何代码。

    Runner 只调 get_body()，不感知 harness/script/body 的区别。
    """

    def __init__(
        self,
        *,
        llm_client,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__()
        self._llm_client = llm_client
        self._event_bus = event_bus or EventBus.null()
        self._harness_cfgs: dict[str, HarnessConfig] = {}
        self._script_names: set[str] = set()

    # ── harness 注册 ──
    def harness(
        self,
        name: str,
        config: HarnessConfig,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
    ) -> "HarnessRegistry":
        """注册 harness body。graph 中 node.body 引用 name。

        返回 self，支持链式调用。
        """
        h = Harness(config, self._llm_client, self._event_bus)
        body = h.build_body(promptmode=promptmode, prompt_extra=prompt_extra)
        self.body(name, body)
        self._harness_cfgs[name] = config
        return self

    # ── script 注册 ──
    def script(self, name: str):
        """@reg.script('name') — 包裹事件发射后注册为 body。"""

    # ── 查询 ──
    def is_harness(self, name: str) -> bool: ...
    def is_script(self, name: str) -> bool: ...
    def harness_config(self, name: str) -> HarnessConfig | None: ...
```

**设计决策**：
- `get_body()` **不需要 override**。body 在注册时已构建好并存入父类 `_bodies`。
- `get_guard()` / `has_guard()` 原样继承，script 和 harness 都用不到 guard（不 override）。
- `reg.harness()` 返回 `self`，支持链式调用。
- `harness_config` 留存配置副本，供前端 introspection/调试。

---

## Script 与 Harness 的协作

### 类型对比

| | harness | script |
|---|---|---|
| 注册 API | `reg.harness(name, config)` | `@reg.script("name")` |
| body 来源 | 框架根据 config 自动生成 | 用户写 Python 函数 |
| body 类型 | async def | 用户函数的原始类型（sync/async 皆可，wrapper 自动适配） |
| 职责 | 标准 LLM 调用链路 | 任意 Python 逻辑（处理/计算/IO） |
| 事件 | 框架内置（细粒度：render/token/validate） | 框架包裹（start/complete/error） |
| 依赖 | LLM 客户端、HarnessConfig、EventBus | 仅 EventBus |

### 典型流程

```
┌─────────┐   JSON 产出    ┌──────────┐  处理后的数据  ┌──────────┐
│ harness │ ──────────────→ │  script  │ ──────────────→ │ harness  │
│ (LLM)   │                │ (Python) │                │ (LLM)    │
└─────────┘                └──────────┘                └─────────┘
```

### tasklist 示例

```json
{
  "Tasks": {
    "A": {
      "type": "harness",
      "harness": "translate",
      "promptmode": "formal",
      "prompt": "请特别注意专业术语",
      "inputs": {"text": "source_text"}
    },
    "B": {
      "type": "script",
      "script": "count_words",
      "inputs": {"data": "A"}
    }
  },
  "Flow": "A --> B"
}
```

翻译层处理：
- 看到 `type: "harness"` → `reg.harness("translate_A", config, promptmode="formal", prompt_extra="...")`
- 看到 `type: "script"` → 已由 `@reg.script("count_words")` 注册
- 生成 Graph：`[A]-->B`，`A.body: translate_A`，`B.body: count_words`

### 单个 tick 内执行流程

```
AsyncRunner.run_until_idle()
  │
  ▼  tick N
  ├─ Node A fires (harness body):
  │    1. PromptRenderer.render(view, "formal", "...")
  │       → emit PromptRendered
  │    2. await llm.complete(prompt, system=notdo, on_token=emit LlmToken)
  │       → emit LlmCallCompleted
  │    3. OutputValidator.validate(content)
  │       → 通过: emit OutputValidated(passed=True) → return parsed_value
  │       → 失败: emit OutputValidated(passed=False) → return Failure(type="llm")
  │
  ├─ tickflow 引擎: record(NodeState)，消费 A 的 slots，写入 B 的 slots
  ├─ tickflow on_fire(A): 外部观察者拿到 NodeState 摘要
  │
  ▼  tick N+1
  └─ Node B fires (script body):
       1. emit ScriptStarted
       2. result = compute(view)
       3. emit ScriptCompleted
       4. tickflow on_fire(B): 外部观察者拿到 NodeState 摘要
```

### 事件流时间线

```
tick N, Node A (harness):
  PromptRendered ─→ LlmCallStarted ─→ LlmToken ×N ─→ LlmCallCompleted ─→ OutputValidated
                                                                              │
tickflow on_fire(A) ──────────────────────────────────────────────────────────┘

tick N+1, Node B (script):
  ScriptStarted ─→ ScriptCompleted
                       │
tickflow on_fire(B) ───┘
```

---

## 错误处理

| 场景 | 行为 |
|------|------|
| LLM 调用抛 `LLMError` | harness body 返回 `Failure(type="infrastructure")`，Runner → ABORTED |
| 输出校验失败（提取也失败） | harness body 返回 `Failure(type="llm")`，下游 AND-join 不触发，运行继续 |
| promptmode 选错（KeyError） | 不捕获，直接抛异常。流程设计问题，框架不兜底 |
| script 抛异常 | `ScriptFailed` 事件发出后异常继续向上传播，tickflow 记录失败 |
| EventBus 回调异常 | 记录日志并吞掉，不破坏运行（与 tickflow hooks 一致） |

---

## 前端接口预留

### 流式输出

`LlmToken` 事件通过 `EventBus` 推送，前端订阅即可拿到实时 token 流：

```python
@bus.on(LlmToken)
def on_token(event: LlmToken):
    # 通过 WebSocket / SSE 推送到前端
    websocket.send_json({"node": event.node, "chunk": event.chunk})
```

### 状态查询

`HarnessRegistry` 提供 introspection API，前端可通过这些接口展示配置：

```python
reg.is_harness("translate")       # → True
reg.is_script("count_words")      # → True  
reg.harness_config("translate")   # → HarnessConfig
```

tickflow 已有的 `audit_log()`, `node_states()`, `firings_of()` 提供节点级别的摘要视图。

---

## 兼容性评估

### 对后续功能的兼容性

| 后续功能 | 兼容性 | 说明 |
|---------|--------|------|
| submodule | ✅ 无阻碍 | `get_body()` 加一个分支即可，三者平级 |
| tasklist 翻译层 | ✅ 已预留 | `HarnessConfig.from_task_definition()` 一行构造 |
| spec 对齐检查 | ✅ 可复用 | 对齐判断本身是 harness 调用，事件类型可后加 |
| 多进程部署 | ✅ 需适配层 | EventBus 保持纯回调，外部加 JSON 序列化适配器 |
| 前端可视化 | ✅ 接口就绪 | EventBus + tickflow hooks 双层，按需选粒度 |
| 框架自带 script 库 | ✅ 无冲突 | 新增 script 函数注册即可 |

### tickflow 影响

**不修改 tickflow 任何代码。** HarnessRegistry 作为子类扩展，tickflow 的 Registry 保持原样。
Runner 通过多态调用 `get_body()`，无需感知子类。

---

## 角色与职责

| | 提供 body 函数 | 注册时机 | 配置来源 |
|---|---|---|---|
| Harness | 框架根据 HarnessConfig 自动生成 | Module 设计阶段（reg.harness） | HarnessConfig dataclass |
| Script | 用户编写，框架包裹事件 | Module 设计阶段（@reg.script） | Python 函数体 |
| Submodule | 框架内嵌 tickflow 运行 | 未来 | Submodule 图 + spec |
