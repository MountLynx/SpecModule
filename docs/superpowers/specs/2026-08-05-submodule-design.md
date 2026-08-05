# Submodule 系统设计文档

> 日期：2026-08-05 | 状态：已确认，已实现

## 背景

SpecModule 面向两层用户：

- **第一层（module 开发者）**：在框架上制作 module——内置 harness / script / command 设定，声明 tasklist 与 spec 模板（`spec_schema`，告诉第二层用户期待什么样的 spec），封装后发布。
- **第二层（module 使用者）**：只负责两件事——① 配置 LLM config（`.env` 文件）；② 写 spec（或自定义 tasklist）。

当前框架只有"过程式 py 脚本"一种搭建方式（README.md:37-82）：创建 client → 注册 harness → 注册 script → 构造 `Module`。问题有二：

1. **样板重复**：每次搭建都要重写固定胶水代码（`LLMConfig.from_env()`、`HarnessRegistry(...)`、模板加载），没有"快速搭建"入口。
2. **无发布形态**：tasklist 中 `"harness": "translate"` 是名字引用（`spec.py:49`），实现（尤其 `prompt_core`）在注册代码里。拿到一个 tasklist 文件无法脱离注册代码运行——不能分发。

本设计为第一层开发者提供**类式快速搭建 + `pack()` 封装发布**，为第二层用户提供**纯 Python 的 `ModuleLoader` 入口**。对应 roadmap #5（submodule，含模块打包/发布）。

## 两层用户模型

### 第一层：类式开发

```python
from module_harness import SubModule, HarnessConfig, CommandConfig, SpecSchema, script

class MyTranslator(SubModule):
    name = "my_translator"
    version = "1.0.0"
    description = "专业翻译模块"

    # 期待第二层用户写的 spec（input 运行前校验，output 仅声明）
    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )

    harnesses = [
        HarnessConfig(name="translate", prompt_core="将以下文本翻译为中文：{text}", ...),
    ]
    commands = []
    requires = []                      # 引用了非自带实现（含内置集）时才声明

    # 固定流程；{spec.xxx} 占位符在构建时渲染，无需 LLM 翻译
    tasklist = Tasklist(tasks={...}, flow="A --> B")

    @script("format_output")           # 类体内普通函数（不绑定 self，与 @reg.script 语义一致）
    def format_output(view):
        return {"result": view.A.value["translation"].strip()}
```

发布（可选，需要分发时才做）：

```python
MyTranslator().pack("./dist/my_translator/")
```

### 第二层：纯 Python 加载使用

```python
from module_harness import ModuleLoader

loader = ModuleLoader()                      # llm config 自动 from .env
module = loader.load("./dist/my_translator/")
result = await module.run({"source_text": "Hello", "style": "formal"})
```

## 范围

**MVP 包含**：

- `SubModule` 基类：类式声明（spec_schema / harnesses / commands / requires / tasklist）+ `@script` 类方法收集 + 嵌入/完整两种运行模式
- `pack()`：导出发布目录（`module.json` + `harnesses/` + `scripts/` + `commands/`）
- `ModuleLoader`：解析清单 → 注册 provides → 校验 requires → 构建实例
- `HarnessConfig` / `CommandConfig` JSON 序列化（to_dict / from_dict）
- `SpecSchema` 模型 + 输入校验
- 内置 harness 集（requires 的默认提供方）
- `Module` 小改：`keep_records` 参数化（嵌入模式）

**不包含**（YAGNI，后续按需）：

- 模块注册表、版本依赖解析、远程安装（roadmap 后续）
- script 多文件包（先单文件 .py，含函数源码 + 必要 import）
- 第二层 CLI（保持纯 Python 入口）
- LLM 翻译生成 tasklist 的 translation 通道——tasklist 固定 + `{spec.xxx}` 占位符已覆盖 spec-only 输入
- 快照/回滚封装（roadmap #6，独立任务）
- submodule 嵌入子 module（module 内调用另一 module）

## 数据模型

### module.json（发布清单）

```json
{
  "name": "my_translator",
  "version": "1.0.0",
  "description": "专业翻译模块",
  "submodule": true,

  "spec_schema": {
    "input": {"source_text": "str", "style": "str"},
    "output": {"translation": "str"}
  },

  "requires": ["spec_tasklist_review"],

  "tasklist": {
    "Tasks": {
      "A": {"type": "harness", "harness": "translate", "inputs": {"text": "{spec.source_text}"}},
      "B": {"type": "script", "script": "format_output", "inputs": {"data": "A"}}
    },
    "Flow": "A --> B"
  }
}
```

- `tasklist`：与 roadmap 草案（module-roadmap.md:113-120）一致，JSON 即 `Tasklist.from_json` 的输入
- `requires`：声明的名字必须在「内置集 ∪ 自身 provides」中可解析，否则加载失败（见错误处理）
- `harnesses/`、`scripts/`、`commands/` 目录不在清单内——provides 的实现分别放对应目录，`module.json` 不重复登记

### SpecSchema

```python
@dataclass
class SpecSchema:
    input: dict[str, str] = field(default_factory=dict)   # {"字段名": "类型"}
    output: dict[str, str] = field(default_factory=dict)  # 仅声明，不校验

    def validate(self, spec: Spec) -> list[str]:  # 返回错误列表；空 = 通过
```

- 字段类型白名单：`"str" / "int" / "float" / "bool" / "list" / "dict" / "any"`
- **input 校验规则**：声明的字段必须存在且类型匹配（缺失/类型错 → 错误）；**未声明的字段允许存在**（spec 保持开放，schema 是契约而非围栏）
- output 不校验：产出在运行后才产生，MVP 只做契约声明（供文档/前端生成）

### SubModule 基类

```python
class SubModule:
    name: str = ""                       # 必须覆写
    version: str = "0.1.0"
    description: str = ""
    spec_schema: SpecSchema = SpecSchema()
    harnesses: list[HarnessConfig] = []
    commands: list[CommandConfig] = []
    requires: list[str] = []
    tasklist: Tasklist | None = None     # 必须覆写（固定流程）

    def __init__(self, llm_client=None, event_bus=None):
        # 两处可用：ModuleLoader 注入；直接类使用（第一层测试/开发）时
        # llm_client=None → 运行时 LLMConfig.from_env() 懒创建
        ...

    async def run(self, spec: dict, *, tasklist=None, audit=False, max_ticks=100):
        ...
```

- **类属性 = 注册信息**；`@script(name)` 装饰器给函数打标记，`__init_subclass__` 收集到 `cls._scripts: dict[str, Callable]`。**脚本是类体内普通函数（不绑定 self）**——与现有 `@reg.script` 语义一致，保证 pack 导出源码后 pack/load round-trip 无需重写签名；需要类常量时通过 `view` 拿节点输出，不依赖实例状态
- **client 注入**：`__init__(llm_client=None, event_bus=None)`——`ModuleLoader` 构造时注入；直接类使用（第一层开发态）传 None，`run()` 时经 `LLMConfig.from_env()` + `create_llm_client()` 懒创建，与 `ModuleLoader` 默认行为一致
- `pack()` 不需要 client，纯序列化导出
- `run()` 不平行实现执行——内部组合现有 `Module`：构建 `HarnessRegistry` → 注册 provides（harness 配置、script 直接注册、command 配置）→ 注册内置集 → 构造 `Module(spec=..., tasklist=...)` → 运行
- `tasklist=None` 时用自身固定 tasklist，**不触发一致性审核**（发布前已验证，且任务流固定，逐次审核是噪音）
- 传入自定义 `tasklist` 时与 `Module` 行为一致：校验 + 一致性审核（review_harness 默认开）
- **嵌入模式**（`audit=False`，默认）：`EventBus.null()` + `keep_records=False`——纯 `(input) -> output`
- **完整模式**（`audit=True`）：EventBus + `keep_records=True`，audit / 事件全开
- 实例化时基于 name 生成唯一 `module_id`（如 `my_translator_3f9a2c`），命名空间隔离沿用 `TasklistTranslator`

### 内置 harness 集（builtins）

```python
# module_harness/builtins.py
BUILTIN_HARNESS_NAMES: frozenset[str] = frozenset({"spec_to_tasklist", "spec_tasklist_review"})

def register_builtin_harnesses(reg: HarnessRegistry) -> None:
    ...
```

- `spec_tasklist_review`：复用 `REVIEW_HARNESS_CONFIG`（consistency.py:40-53），与 `register_review_harness` 等价
- `spec_to_tasklist`：最小配置即可——翻译模板的 `prompt_core` 会覆盖已注册配置（translator.py:237-255），只需 `output_format=json_object` 等骨架
- `ModuleLoader` 加载时默认注册内置集；`SubModule` 运行同样注册
- 内置集是 requires 的默认提供方——模块要引用内置名时无需自备实现

### ModuleLoader

```python
class ModuleLoader:
    def __init__(self, llm_config: LLMConfig | None = None, *, llm_client=None, event_bus: EventBus | None = None):
        # llm_client 优先（测试/注入用）；否则 llm_config（None → LLMConfig.from_env()）创建
        ...

    def load(self, path: str | Path) -> SubModule:
        ...
```

加载流程：

1. 读目录下 `module.json`；缺失/格式错误 → `ModuleManifestError`
2. 注册 provides：`harnesses/*.json`（每文件一个 `HarnessConfig` 的序列化，含 `name` 字段）、`scripts/*.py`（exec 后按函数名取注册）、`commands/*.json`
3. 校验 requires：每个名字必须在「内置集 ∪ provides」中，否则 `ModuleRequirementError`
4. 构建并返回 `SubModule` 实例（`run()` 已可调用）

## 执行流程

### 第二层用户视角

```
.env（llm config） + module 目录 + spec
        │
        ▼
ModuleLoader.load()
  ├─ 解析 module.json
  ├─ 注册 provides（harnesses/scripts/commands）
  ├─ 注册内置集
  └─ 校验 requires
        │
        ▼
SubModule.run(spec)            # 或 run(spec, tasklist=自定义)
  ├─ spec_schema.validate()    # 失败 → SpecValidationError
  ├─ 构建内部 HarnessRegistry + 注册
  ├─ 构造 Module(spec, tasklist)  → 构建 runner
  └─ 运行 → 返回 firings
```

### 嵌入模式与完整模式

| 模式 | EventBus | keep_records | 一致性审核 | 适用 |
|------|----------|--------------|------------|------|
| 嵌入（默认） | `EventBus.null()` | `False` | 固定 tasklist 不审 | 第二层日常使用 |
| 完整（`audit=True`） | 传入的 bus | `True` | 自定义 tasklist 时审 | 调试、审计 |

## 错误处理

| 异常 | 触发 | 行为 |
|------|------|------|
| `SpecValidationError` | spec 不满足 `spec_schema.input` | 消息列出全部缺失/类型错误字段，快速失败 |
| `ModuleRequirementError` | `requires` 名不在「内置集 ∪ provides」 | 列出未解析的名字 + 可用名表 |
| `ModuleManifestError` | module.json 缺失/JSON 损坏/缺必需字段 | 指出文件路径与原因 |

与现有异常风格一致（参照 `ConsistencyError`），均继承 `Exception`。

## 测试计划

### test_submodule.py（新建）

沿用现有测试设施（pytest + MagicMock/AsyncMock，见 conftest.py）：

1. **类声明 → 运行**：固定 tasklist + mock LLM，`run(spec)` 返回 firings，script 收到 `view`
2. **spec 校验**：缺字段 / 类型错 → `SpecValidationError`，消息含字段名；未声明字段不报错
3. **pack() 导出**：目录结构（module.json + harnesses/ + scripts/ + commands/）+ module.json 内容正确
4. **round-trip**：`pack()` → `ModuleLoader.load()` → `run()`（mock LLM），结果与直接类运行一致
5. **requires 校验**：`requires=["不存在的名"]` → `ModuleRequirementError`
6. **manifest 错误**：缺 module.json / JSON 损坏 → `ModuleManifestError`
7. **嵌入 vs 完整模式**：audit=False 时 EventBus 为 null、keep_records 关闭；audit=True 时事件可订阅
8. **命名空间隔离**：同进程加载两个实例（或同 module 两实例）互不冲突
9. **序列化 round-trip**：`HarnessConfig.to_dict` → `from_dict` 字段等价（含嵌套 dict/list 字段）

### 既有测试回归

- `module_harness/tests/` 全量跑通（`Module` 的 keep_records 参数化不影响默认行为）

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `module_harness/submodule.py` | **新建**：SubModule 基类、`script` 装饰器、`SpecValidationError` |
| `module_harness/loader.py` | **新建**：ModuleLoader、module.json 解析、`ModuleRequirementError`、`ModuleManifestError` |
| `module_harness/builtins.py` | **新建**：内置 harness 名表 + `register_builtin_harnesses` |
| `module_harness/spec.py` | **追加**：`SpecSchema` 模型 + validate |
| `module_harness/config.py` | **追加**：`HarnessConfig` / `CommandConfig` 的 to_dict / from_dict |
| `module_harness/module.py` | **小改**：`keep_records: bool = True` 构造参数 |
| `module_harness/__init__.py` | **追加导出**：SubModule、ModuleLoader、SpecSchema、script |
| `module_harness/tests/test_submodule.py` | **新建**：上表测试 |
| `docs/progress/module-roadmap.md` | 实现完成后更新 #5 状态 |

## 与已有模块的关系

- **SubModule 组合 Module，不修改执行管线**：复用 `Module._build_runner_async`（spec+tasklist → 校验 → 审核 → TasklistTranslator → AsyncRunner），仅通过构造参数控制模式。遵守"tickflow 零修改、无平行状态"架构规则。
- **script 复用 registry 机制**：`@script` 收集的类方法经 `functools.partial` 绑定后走现有 `reg.script(name)` 注册（含事件包裹）。
- **内置集复用既有配置**：`spec_tasklist_review` 直接引用 `REVIEW_HARNESS_CONFIG`；`spec_to_tasklist` 最小骨架由 `register_builtin_harnesses` 定义。
- **模板与 submodule 的关系**：内置模板（templates/builtin/）保持现状；submodule 是模板之上的"完整封装"形态（含实现 + 契约），二者不合并。

## 使用示例

### 第一层：完整 module 定义 + 发布

```python
from module_harness import SubModule, HarnessConfig, SpecSchema, Tasklist, TaskDefinition, script

class MyTranslator(SubModule):
    name = "my_translator"
    version = "1.0.0"
    description = "专业翻译模块"

    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )

    harnesses = [
        HarnessConfig(
            name="translate",
            prompt_core="将以下文本翻译为中文：{text}",
            prompt_modes={
                "formal": "使用正式书面语，保持原文风格",
                "casual": "使用口语化表达",
            },
            output_format=OutputFormat(type="json_object"),
            temperature=0.3,
        ),
    ]

    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                promptmode="{spec.style}",
                inputs={"text": "{spec.source_text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_output", inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )

    @script("format_output")
    def format_output(view):
        return {"translation": view.A.value["translation"].strip()}

MyTranslator().pack("./dist/my_translator/")
```

### 第二层：加载使用

```python
# .env 已配置 LLM（LLM_PROVIDER / LLM_MODEL / API_KEY）
from module_harness import ModuleLoader

module = ModuleLoader().load("./dist/my_translator/")
result = await module.run({"source_text": "Hello world", "style": "formal"})
for f in result:
    print(f"{f.node}: {f.output}")
```
