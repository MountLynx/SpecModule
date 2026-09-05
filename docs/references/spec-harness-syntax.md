# spec 与 harness 语法参考

SpecModule 模块层的声明语法参考——**spec**（想要什么）、**tasklist**（如何做）、**harness**（执行元件配置）、**模板**（翻译通道）。对标 tickflow Graph 仓库的 README：本文档是上层 module_harness 的语法面。

## 总览：三层声明

```
spec（想要什么）──► tasklist（如何做）──► 执行元件（如何执行）
  自由键值对          Tasks + Flow          harness / script / command
```

两种输入通道：

| 通道 | 用法 | 流程 |
|------|------|------|
| **模板翻译** | 只传 spec + `template_name` | 模板 → 翻译器（LLM harness / script 确定性）→ tasklist → 校验 → 建图 |
| **自定义** | 传 spec + tasklist | 校验 → 一致性审核（默认开）→ 建图 |

```python
module = Module(spec={...}, template_name="translate", ...)   # 通道 ①
module = Module(spec={...}, tasklist=Tasklist(...), ...)       # 通道 ②
```

---

## spec 语法

spec 是任意结构化键值对，**无预定义 schema**——字段含义由模板设计者定义。

```python
spec = {
    "source_text": "Hello world",     # str
    "style": "formal",                # str（驱动 promptmode）
    "max_length": 500,                # int
    "strict": True,                   # bool
    "sections": ["摘要", "结论"],      # list
    "meta": {"author": "x"},          # dict（嵌套自由）
}
```

### 在任务声明中引用 spec

| 引用写法 | 解析时机 | 用途 |
|---------|---------|------|
| `{spec.xxx}` | **注册时**解析为字面值 | `inputs`、`promptmode`、模板 prompt |
| `{spec}` | 注册时解析为 spec 整体 JSON | `inputs` 常量输入 |
| `{tasklist}` | 注册时解析为 tasklist 整体 JSON | `inputs` 常量输入 |
| `{node}` | 注册时解析为当前节点名 | `inputs` 常量输入 |

### spec_schema（submodule 输入契约）

`SubModule` 可用 `SpecSchema` 声明输入契约，运行时校验：

```python
spec_schema = SpecSchema(
    input={"source_text": "str", "style": "str"},   # 声明的字段必须存在且类型匹配
    output={"translation": "str"},                  # 输出仅声明，不校验
)
```

支持类型：`str` / `int` / `float` / `bool` / `list` / `dict` / `any`（bool 与 int 严格区分）。

---

## tasklist 语法

```python
tasklist = Tasklist(
    tasks={
        "A": TaskDefinition(
            type="harness", harness="translate",
            promptmode="{spec.style}",              # spec 字段驱动动态 prompt
            inputs={"text": "{spec.source_text}"},  # 常量输入（注册时解析）
            outputformat={"type": "json_object"},
        ),
        "B": TaskDefinition(
            type="script", script="format_output",
            inputs={"data": "A"},                   # 节点输入（运行时解析）
        ),
    },
    flow="A --> B",
)
```

### TaskDefinition 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"harness" \| "script" \| "command"` | 任务类型（必填） |
| `harness` | `str \| None` | type=harness 时引用的已注册 harness 名 |
| `script` | `str \| None` | type=script 时引用的已注册 script 名 |
| `command` | `str \| None` | type=command 时引用的命令名 |
| `timeout` / `cwd` | `float \| None` / `str \| None` | command 覆盖（超时秒 / 工作目录） |
| `promptmode` | `str \| None` | Layer 2 prompt 选择键（可含 `{spec.xxx}`）；**键不存在 → KeyError** |
| `prompt` | `str \| None` | Layer 3 人工注入 prompt（harness 追加段） |
| `outputformat` | `dict \| None` | 输出格式覆盖，如 `{"type": "json_schema", "schema": {...}}` |
| `notdo` | `list[str] \| None` | 否定性约束（拼入 system prompt） |
| `model` / `temperature` / `think` | 覆盖 harness 基础配置 |
| `api_params` | `dict \| None` | 透传 LLM SDK 额外参数 |
| `inputs` | `dict[str, str] \| None` | `{字段名: 来源}`——来源为节点名或常量 token |

### Flow 语法（tickflow DSL 子集）

```text
[A] --> B                   # [A] = start 节点；普通边（恒 True）
B --|g1|--> C               # guard 边：写入 guard(view) 结果
B --> A                     # 循环边
C.inputs: A, B[2]           # C 读 A（latest）与 B 的第 2 次输出（1-based）
C.body: compute_c           # 绑定已注册 body（通常由 graph_builder 自动绑定）
C.join: OR                  # 覆盖 join（默认 AND）
```

- 裸任务名作整段 flow 时自动包装：`"A"` → `"[A]"`
- 多节点 flow 无 `[` 标记时，**首 token 自动包为 start**（`prepare_flow`）
- 完整语义（AND/OR join、标记、循环）见 tickflow 文档；`TasklistValidator` 在运行时校验：flow 引用未定义节点、任务未被 flow 引用（孤立节点）、DSL 语法错误

### inputs 的两种来源

| 来源 | 例子 | 语义 |
|------|------|------|
| 常量 token | `{"text": "{spec.source_text}"}`、`{"doc": "{spec}"}`、`{"self": "{node}"}` | 注册时解析为字面值（spec 字段 / 整体 JSON / 节点名） |
| 节点名 | `{"data": "A"}` | 运行时从 producer A 的输出解析 |

> **具名 bind 语义**：非常量节点输入写成 field→producer 的**具名 bind**（tickflow 0.2），`inputs` 只保留 producer 键。body 按字段名消费：`view.field("字段名")` / `view.named`；prompt 的 `{field}` 占位符运行时经 `v.named` 渲染 producer 值（未点火字段值为 Missing，走常量兜底）。

---

## harness 语法（HarnessConfig）

```python
reg.harness("translate", HarnessConfig(
    # Layer 1：核心提示词（必填），{key} 占位符从 view 取
    prompt_core="将以下文本翻译为中文：{text}",
    # Layer 2：动态 prompt 选项集（Task 的 promptmode 选择）
    prompt_modes={"formal": "请使用正式语气", "casual": "请使用随意语气"},
    # 输出约束（None = 不约束）
    output_format=OutputFormat(type="json_object"),
    # 否定性约束（拼入 system prompt）
    notdo=["不要添加解释"],
    # LLM 参数（Task 可逐项覆盖）
    model="deepseek-v4-flash",
    temperature=0.3,
    think=True,
    # SDK 透传（与独立字段合并，api_params 优先级更高）
    api_params={"thinking": {"type": "enabled"}},
))
```

### 三层 prompt

| Layer | 字段 | 来源 | 说明 |
|-------|------|------|------|
| 1 | `prompt_core` | 模板/注册时 | 核心提示词，必填 |
| 2 | `prompt_modes[mode]` | Task 的 `promptmode` 选择 | **mode 键不存在 → KeyError**（无静默回退） |
| 3 | `prompt_extra` | Task 的 `prompt` 字段 | 人工注入段 |

渲染顺序：`prompt_core` + `prompt_modes[mode]` + `prompt_extra`，以空行拼接。模板中的 `{key}` 从视图的具名 bind 字段取值（`v.named`），未匹配的 key **保留原样**，不隐藏问题。

### output_format

| type | 校验 | 说明 |
|------|------|------|
| `"json_object"` | 必须是合法 JSON | 失败 → `Failure(type="llm")`，下游跳过、运行继续 |
| `"json_schema"` | 合法 JSON 且通过 `schema` 校验（需 `jsonschema`） | 同上 |
| `"text"` | 不校验 | 直接返回原文本 |

校验失败时自动尝试修复提取：``` ```json ``` 围栏剥离 → 首个完整 JSON 对象 → 尾部垃圾截断；全部失败 → `Failure`。类型写错（如 `"json"`）在**运行时**报错——框架不兜底。

### 注册方式

```python
reg.harness("name", HarnessConfig(...))                       # harness（LLM 节点）
@reg.script("name")                                           # script（纯 Python）
def fn(view): return ...
reg.command("name", CommandConfig(command="echo {text}"))     # command（shell）
```

submodule 的 `harnesses` 列表中的 `HarnessConfig` 必须带 `name` 字段（打包发布时按名导出）。

---

## 模板语法（翻译通道）

```json
{
  "name": "translate",
  "description": "通用翻译模块",
  "translation": {
    "type": "harness",
    "harness": "spec_to_tasklist",
    "prompt": "你是一个流程设计器。根据以下 spec 生成合法的 tasklist JSON。...",
    "prompt_core": "你是一个 tasklist JSON 生成器。..."
  },
  "tasklist": {
    "Tasks": {
      "A": {
        "type": "harness", "harness": "translate",
        "promptmode": "{spec.style}",
        "inputs": {"text": "{spec.source_text}"},
        "outputformat": {"type": "json_object"}
      },
      "B": {"type": "script", "script": "format_output", "inputs": {"data": "A"}}
    },
    "Flow": "A --> B"
  }
}
```

| 字段 | 说明 |
|------|------|
| `name` / `description` | 模板元数据 |
| `translation` | 翻译声明（TranslationSpec）：`type: "harness" \| "script"`、`harness`/`script`（执行翻译的元件）、`prompt`/`prompt_core`（翻译指令） |
| `tasklist` | **特定流程 tasklist 的定义**（翻译的目标形态）——LLM 翻译时作骨架示例与 Flow 兜底（`translate()` 用 `template.tasklist.flow` 兜底）；script 翻译时返回值可完全覆盖。Task 的 `promptmode`/`inputs` 用 `{spec.xxx}` 引用运行时 spec |

内置模板：`translate` / `summarize` / `codereview` / `docwrite`（`loader.load_builtins()`），详见下文「内置模板」。

### 模板语义：翻译声明 + 特定流程 tasklist 定义

`TasklistTemplate` 是**一枚硬币的两面**：`translation` 声明"由谁翻译"，`tasklist` 字段定义"翻译成什么样的特定流程"。

**翻译器 = 产出 tasklist 的通道，可产出任意形式的 tasklist**（submodule 黑盒形式 / loop 内联展开形式 / 任意节点组合）——由翻译器返回值决定，与"固定流水线 vs 动态流程"无关。

| 翻译器类型 | 语义 | 适用 |
|-----------|------|------|
| `type: "harness"`（LLM） | 读 spec + translation.prompt 生成 tasklist JSON | spec 驱动、流程由 LLM 设计的场景（内置模板） |
| `type: "script"`（确定性） | 直接调用已注册 script 函数，返回 tasklist dict（需 spec 时读合成视图的具名字段 `view.field("spec")`） | **固定流水线的多形态封装**——零 LLM 成本、流程稳定 |

```python
# script 翻译器：确定性返回"特定形式的 tasklist"（是否读 spec 由实现决定）
def tl_academic(view):                        # 形式 1：submodule 黑盒（loop 在子模块内）
    return academic_tasklist.to_dict()
def tl_detailed(view):                        # 形式 2：loop 内联展开（详细模式，全程可审计）
    return detailed_tasklist.to_dict()
reg.script("tl_academic")(tl_academic)
reg.script("tl_detailed")(tl_detailed)
```

### 多模板：一个 module 多种使用方式

`TemplateLoader` 按 `name` 注册多个模板（`register()` / `load_directory()` / `load_builtins()`），`Module(template_name=...)` 选择其一——**同一个 module（同一组 harness/script/registry）配多个 tasklist 模板**，经 `template_name` 切换：

```python
loader = TemplateLoader()
loader.register("academic_writer", {...})            # 翻译器返回 submodule 形式
loader.register("academic_writer_detailed", {...})   # 翻译器返回内联 loop 形式（详细模式）

mod = Module(spec=..., template_name="academic_writer_detailed",
             template_loader=loader, registry=reg, ...)
```

`template_name` 与 `tasklist` 参数二选一：**tasklist 直入 = 跳过翻译的模板**——两者是同一事物的两种封装，不是"固定 vs 动态"的对立（先例：`example/academic_writer.py` 的 `run_writer(mode=...)`）。

### 内置模板

`translate` / `summarize` / `codereview` / `docwrite`——**spec only 使用场景的预置包**：用户只写 spec（如 `{"source_text": ..., "target_lang": ...}`），LLM 翻译器按 translation.prompt 生成 tasklist，模板 `tasklist` 字段作骨架与 Flow 兜底。

> ⚠️ **常见误解**：内置模板恰好全部用 LLM 翻译（`spec_to_tasklist` harness，演示动态流程）——**不代表模板必须 LLM 翻译**。固定流水线的多形态封装用 script 翻译器（确定性、可读 spec、返回固定流程 dict），见上。`translation.type` 支持 `"script"` 是机制本身的能力，不是特例。

---

## 引用解析规则汇总

| 位置 | 写法 | 解析时机 | 结果 |
|------|------|---------|------|
| Task `inputs` | `{spec.xxx}` / `{spec}` / `{tasklist}` / `{node}` | 注册时（graph_builder） | 字面值 |
| Task `promptmode` | `{spec.xxx}` | 注册时 | 字面值（选 Layer 2 prompt） |
| Task `inputs` | 节点名 | 运行时 | producer 最新输出（`latest_before`，严格前序 tick） |
| prompt 模板 | `{key}` | 运行时（渲染） | 具名 bind 字段（`v.named`）；常量输入（spec_inputs）兜底；未匹配保留原样 |

---

## 错误处理矩阵

| 场景 | 行为 |
|------|------|
| `promptmode` 键在 `prompt_modes` 中不存在 | `KeyError`（无静默回退） |
| `output_format` type 写错 / 输出不合法 | 校验失败 → `Failure(type="llm")`：节点 failed，下游跳过，运行继续 |
| 任务引用未注册 harness/script/command | `TasklistValidator` 返回错误 → `ValueError` |
| flow 引用未定义节点 / 任务孤立 / DSL 语法错 | `TasklistValidator` 返回错误 → `ValueError` |
| 自定义通道一致性审核不通过 | `ConsistencyError`（可传 `review_harness=None` 关闭） |
| spec 不满足 `SpecSchema` 契约 | `SpecValidationError` |

---

## 最小示例（模板通道）

```python
from module_harness import Module, HarnessRegistry, HarnessConfig, EventBus, TemplateLoader, OutputFormat
from llm import create_llm_client, LLMConfig

reg = HarnessRegistry(llm_client=create_llm_client(LLMConfig.from_env()), event_bus=EventBus())
reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}", output_format=OutputFormat(type="json_object")))
loader = TemplateLoader(); loader.load_builtins()

module = Module(
    spec={"source_text": "Hello", "style": "formal"},
    template_name="translate",        # 模板通道：翻译器（LLM harness / script 确定性）按声明生成 tasklist
    registry=reg,                     # registry 已含 client 与 event_bus
    template_loader=loader,
)
firings = await module.run()
```
