# Spec & Tasklist 设计文档

> 日期：2026-06-30 | 状态：已确认，待实现

## 概述

实现 SpecModule 的 spec 与 tasklist 层——Module 的配置输入层，将用户的目标描述（spec）和流程计划（tasklist）转化为 tickflow + module_harness 可执行的 runner。

**MVP 范围**：
- 结构化 spec + tasklist 数据模型
- Tasklist 模板机制（内置模板 + 翻译器）
- spec → tasklist 翻译（LLM harness 翻译 + script 函数翻译）
- tasklist → tickflow Graph 翻译器
- Module 编排器（加载模板 → 翻译 → 构建 runner）
- 命名空间隔离（module_id 前缀）

**不包含**：一致性审核、对齐检查、全局字典、submodule。
（但设计预留了后续拓展接口，见兼容性评估部分。）

---

## 数据模型

### Spec — 结构化键值对

用户自由定义字段，框架不强加 schema：

```json
{
  "task_type": "translate",
  "style": "formal",
  "source_text": "Hello world",
  "description": "翻译这段英文到中文"
}
```

字段无预定义——完全由 module 设计者和模板定义。

### TaskDefinition — 单个 Task 条目

```python
@dataclass
class TaskDefinition:
    """tasklist 中单个 Task 的定义。与 module_harness 的 HarnessConfig 对齐。"""

    type: Literal["harness", "script"]  # submodule 未来加入
    harness: str | None = None           # type="harness" 时：引用已注册 harness 名
    script: str | None = None            # type="script" 时：引用已注册 script 名
    promptmode: str | None = None        # harness 的动态 prompt 选择键
    prompt: str | None = None            # harness 人工注入 prompt（Layer 3）
    outputformat: dict | None = None     # 输出格式（→ OutputFormat）
    notdo: list[str] = None             # 否定性约束
    model: str | None = None            # LLM 模型覆盖
    temperature: float | None = None
    think: bool | dict | None = None
    inputs: dict[str, str] = None       # 输入映射 {input_key: global_key}
```

### Tasklist — Tasks + Flow

```python
@dataclass
class Tasklist:
    tasks: dict[str, TaskDefinition]  # {"A": TaskDef(...), "B": TaskDef(...)}
    flow: str                         # mermaid 语法，"A --> B\nB --> C"
```

JSON 表示：

```json
{
  "Tasks": {
    "A": {
      "type": "harness",
      "harness": "translate",
      "promptmode": "formal",
      "inputs": {"text": "source_text"}
    },
    "B": {
      "type": "script",
      "script": "post_process",
      "inputs": {"data": "A"}
    }
  },
  "Flow": "A --> B"
}
```

### TasklistTemplate — 翻译声明 + tasklist 骨架

```python
@dataclass
class TranslationSpec:
    """翻译方式声明——翻译本身也是 harness/script/submodule 调用。"""
    type: Literal["harness", "script"]  # submodule 未来加入
    harness: str | None = None          # type="harness"：引用的 harness 名
    script: str | None = None           # type="script"：引用的 script 名
    prompt: str | None = None           # harness 翻译的提示词


@dataclass
class TasklistTemplate:
    name: str
    description: str
    translation: TranslationSpec
    tasklist: Tasklist                  # 骨架，含 {spec.xxx} 占位符
```

JSON 表示：

```json
{
  "name": "translate_module",
  "description": "通用翻译模块",
  "translation": {
    "type": "harness",
    "harness": "spec_to_tasklist",
    "prompt": "你是一个流程设计器。根据以下 spec 生成合法的 tasklist JSON..."
  },
  "tasklist": {
    "Tasks": {
      "A": {
        "type": "harness",
        "harness": "{spec.harness_name}",
        "promptmode": "{spec.style}",
        "inputs": {"text": "{spec.source_text}"}
      },
      "B": {
        "type": "script",
        "script": "post_process",
        "inputs": {"data": "A"}
      }
    },
    "Flow": "A --> B"
  }
}
```

**关键设计**：
- `tasklist` 中 `{spec.xxx}` 占位符由翻译阶段填充为最终 tasklist。
- `translation.type` 显式声明翻译方式：`"harness"` 用 LLM 翻译，`"script"` 用函数直映。
- 翻译结果经 `TasklistValidator` 校验后才进入 graph 构建阶段。

---

## 翻译流程

### 翻译方式

翻译的本质：`(Spec, TasklistTemplate) → Tasklist`。两种翻译方式，由 `translation.type` 显式指定：

**1. Script 翻译** — 函数直接映射，用于简单固定的流程。

`TranslationSpec` 提供 `script` 名称，指向已注册的脚本函数。函数接收 spec dict，返回 tasks dict（含已填充值的 Task 定义）。等价于模板中 `{spec.xxx}` 的填值操作，精度更高——可加条件分支、默认值、字段转换。

```python
@reg.script("translate_translator")
def translate_translator(view):
    spec = view.spec.value
    return {
        "A": {
            "type": "harness",
            "harness": spec["harness_name"],
            "promptmode": spec.get("style", "formal"),
            "inputs": {"text": spec["source_text"]}
        }
    }
```

**2. Harness 翻译** — LLM 根据 prompt + spec 生成完整 tasklist JSON。

`TranslationSpec` 提供 `harness` 名称（引用已注册 harness）和 `prompt`。prompt 中注入 spec 原文、模板骨架，要求按格式输出。翻译 harness 注册时应有 `output_format` 约束为合法 tasklist 结构。

### Translator 类

翻译**不走 tickflow**——直接调用 harness body 或 script 函数。避免为单次翻译构建 graph/runner 的额外开销。

```python
class Translator:
    """直接调用翻译器 body/script，拿到 tasklist。"""

    def __init__(self, registry: HarnessRegistry): ...

    async def translate(self, spec: dict, template: TasklistTemplate) -> Tasklist:
        # 1. 构造 view（含 spec 数据）
        # 2. 调 body（harness → LLM，script → 函数）
        # 3. TasklistValidator.validate(result) → Tasklist
        # 4. 校验失败 → 返回错误建议（spec 不够清晰等）
```

### TasklistValidator

校验翻译结果的合法性：

1. **结构合法**：Tasks 和 Flow 字段存在，Flow 可被 tickflow parse
2. **引用存在**：Task 中引用的 harness/script 名称已在 registry 中注册
3. **type 一致**：Task type 与字段匹配（harness 有 harness 字段，script 有 script 字段）
4. **Flow 节点对齐**：Flow 中以 `[X]` 标记或入度为零的节点在 Tasks 中都有定义

---

## Tasklist → tickflow 翻译

将 `Tasklist` + `HarnessRegistry` → `(Graph, HarnessRegistry)`。每个 Task 转换为一个 node body。

```python
class TasklistTranslator:
    def __init__(self, registry: HarnessRegistry, module_id: str): ...

    def build(self, tasklist: Tasklist) -> tuple[Graph, HarnessRegistry]:
        """
        1. 遍历 Tasks：按 type 注册 body
           - harness → reg.harness(f"{module_id}:{key}", config, ...)
           - script  → 验证 reg.is_script(script_name) 为 True
        2. 生成 tickflow graph 文本
        3. parse(graph_text, registry=reg) → Graph
        4. 返回 (Graph, reg)
        """
```

### 映射规则

| Tasklist | tickflow |
|----------|----------|
| Task key `"A"` | node 名 `"A"`，body 名 `"{module_id}:A"` |
| `type: "harness"` | `reg.harness("{module_id}:A", cfg, promptmode, prompt_extra)` |
| `type: "script"` | 验证存在，body 引用为 `"{module_id}:A"`（已装饰注册） |
| `inputs: {"text": "source"}` | `A.inputs: text: source`（tickflow 输入绑定） |
| `Flow: "A --> B"` | graph edge，`[A]` 标记 start node |
| `Flow: "B --|guard|-->C"` | 带 guard 的边 |

### 命名空间隔离

为避免 module 设计者与使用者的名称冲突，每个 module 实例有独立的 `module_id`：

```
Task key "A" → body 注册名 "{module_id}:A" → graph 中 A.body: module_1:A
```

- `module_id` 由 Module 构造时自动生成（可传入或随机 UUID 前缀）
- 原始 harness/script 名仅用于 `reg.harness()` 调用时关联 `HarnessConfig`（查 `harness_config`）
- script 节点：用户以 `@reg.script("post_process")` 注册原始名，翻译时 `TasklistTranslator` 将它以隔离名注册到同一个函数

---

## Module 编排器

最小的 Module 类，串联三个步骤：

```python
class Module:
    """SpecModule 的核心编排器。"""

    def __init__(
        self,
        spec: dict,
        template_name: str,
        llm_client,
        event_bus: EventBus | None = None,
        module_id: str | None = None,
    ): ...

    def build_runner(self) -> AsyncRunner:
        """执行翻译 → 构建 graph → 返回 AsyncRunner。"""
        # 1. template = TemplateLoader.get(self.template_name)
        # 2. tasklist = await Translator.translate(self.spec, template)
        # 3. graph, reg = TasklistTranslator.build(tasklist)
        # 4. return AsyncRunner(graph, registry=reg)

    async def run(self, max_ticks: int = 100):
        runner = self.build_runner()
        return await runner.run_until_idle(max_ticks=max_ticks)
```

### TemplateLoader

加载模板。MVP 支持：

- 内置模板（代码中注册或文件加载）
- JSON/YAML 文件模板（`templates/` 目录）
- `get(name) → TasklistTemplate | None`

后续可扩展远程模板、模板版本管理。

---

## 文件结构

```
module_harness/
    __init__.py          # 已有，新增导出
    events.py            # 已有
    outputfmt.py         # 已有
    config.py            # 已有
    prompt.py            # 已有
    harness.py           # 已有
    registry.py          # 已有
    spec.py              # ★ Spec, Tasklist, TasklistTemplate, TranslationSpec, TaskDefinition
    translator.py        # ★ Translator, TasklistValidator, TemplateLoader
    graph_builder.py     # ★ TasklistTranslator
    module.py            # ★ Module 编排器
```

---

## 执行流程总览

```
用户/Agent
    │ spec + template_name
    ▼
Module.__init__()
    │
    ├─ TemplateLoader.get(template_name) → TasklistTemplate
    │
    ├─ Translator.translate(spec, template) → Tasklist
    │     │
    │     ├── translation.type == "harness" → 调 LLM body → 校验
    │     └── translation.type == "script"  → 调 script 函数 → 校验
    │
    ├─ TasklistTranslator.build(tasklist) → (Graph, HarnessRegistry)
    │     │
    │     ├── 遍历 Tasks → reg.harness() 注册
    │     ├── 生成 graph 文本
    │     └── parse() → Graph
    │
    └─ AsyncRunner(graph, registry=reg)
          │
          └─ runner.run_until_idle() → 执行
```

---

## 错误处理

| 场景 | 行为 |
|------|------|
| 模板不存在 | 抛 `ValueError("unknown template: xxx")` |
| 翻译 harness 调用失败 | 抛 `TranslationError`，含 LLM 返回的建议 |
| 翻译结果校验失败 | 抛 `ValidationError`，指出具体字段问题 |
| Task 引用不存在的 harness/script | 校验阶段阻止，抛 `ValidationError` |
| Flow parse 失败 | 校验或 graph 构建阶段抛 `tickflow.ParseError` |

---

## 兼容性评估

### 后续功能预留

| 后续功能 | 预留方式 |
|---------|---------|
| 一致性审核 | `TasklistValidator` 可扩展为一致性检查器（spec vs tasklist 语义匹配） |
| 对齐检查 | Module 上预留 `alignment_interval` 参数（当前不实现），EventBus 预留 `ModuleTickChecked` 事件类型 |
| 全局字典 | spec/tasklist 的 `inputs` 字段就是全局 key 引用的雏形；后续在 Module 层加 `GlobalDict` 包装 |
| submodule | `TaskDefinition.type` 枚举已预留 `"submodule"`；`TranslationSpec.type` 同 |
| spec→tasklist 生成优化 | 翻译流程的三种方式（LLM/script/submodule）一开始就区分清楚 |
| 多模板组合 | `TemplateLoader` 可扩展为模板注册表，支持模板继承/组合 |
| 可视化编辑器 | Flow 字段是 mermaid 语法，编辑器直读直写；Task 字段结构与 HarnessConfig 对齐 |

### 与已有模块的关系

- **module_harness**：TaskDefinition 的 harness/script 字段直接映射到 `HarnessConfig` 和 `@reg.script()`。翻译器调用 `reg.harness()` 注册，完全复用已有机制。
- **tickflow**：Flow 字段直接喂给 `parse()`。Graph 和 Runner 无需任何修改。
- **llm**：翻译 harness 复用已有 LLM 客户端，无新依赖。

---

## 模板示例

### 内嵌模板：翻译模块

```json
{
  "name": "translate",
  "description": "将文本翻译为目标语言",
  "translation": {
    "type": "harness",
    "harness": "spec_to_tasklist",
    "prompt": "根据以下 spec 生成 tasklist。spec 包含 task_type、source_text、target_lang、style 字段。生成的 tasklist 应包含两个节点：A 执行翻译，B 执行后处理。"
  },
  "tasklist": {
    "Tasks": {
      "A": {
        "type": "harness",
        "harness": "translate",
        "promptmode": "{spec.style}",
        "inputs": {"text": "{spec.source_text}"},
        "outputformat": {"type": "json_object"}
      },
      "B": {
        "type": "script",
        "script": "format_output",
        "inputs": {"data": "A"}
      }
    },
    "Flow": "A --> B"
  }
}
```

### 内嵌模板：代码审查模块（script 翻译）

```json
{
  "name": "code_review",
  "description": "审查代码片段",
  "translation": {
    "type": "script",
    "script": "review_translator"
  },
  "tasklist": {
    "Tasks": {},
    "Flow": ""
  }
}
```

script 翻译中 tasklist 骨架可为空——由 script 函数根据 spec 完全动态生成。

---

## 使用示例

```python
from module_harness import Module, HarnessRegistry, EventBus
from llm import create_llm_client, LLMConfig

# 准备
config = LLMConfig.from_env()
client = create_llm_client(config)
bus = EventBus()
reg = HarnessRegistry(llm_client=client, event_bus=bus)

# 注册内置 harness/script
reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}", ...))
reg.harness("spec_to_tasklist", HarnessConfig(prompt_core="...", output_format=...))

@reg.script("format_output")
def format_output(view): ...

# 加载模板（从内置或文件）
from module_harness.translator import TemplateLoader
loader = TemplateLoader()
loader.load_builtins()  # 注册内置模板

# 创建并运行 module
module = Module(
    spec={"task_type": "translate", "style": "formal", "source_text": "Hello"},
    template_name="translate",
    llm_client=client,
    event_bus=bus,
)

results = await module.run(max_ticks=100)
```
