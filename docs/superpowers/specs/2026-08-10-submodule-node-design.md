# Submodule 一等节点类型设计

> 日期：2026-08-10 | 状态：已确认，待实现

## 背景

roadmap「模块组合讨论与决策」（`module-roadmap.md:168-186`）基于错误前提做了判定：声明式 submodule 节点被曲解为"图级组合"（"仅省 flow 骨架几行边，成本高"），转而采纳"嵌套执行"（async script 手写 `await 子 module.run()`）。

用户的期望不是省骨架，而是**组合/封装的声明式能力**：submodule 是与 harness/script **同级的一等节点类型**——tasklist 里直接写 `{type: "submodule", submodule: "fact_review_loop", ...}`。节点是黑盒处理单元（内部过程无实际审查意义，只有产出有意义 → 不进审计/快照/回滚）；定义是完整的 `SubModule` 类（双重身份：既可独立运行/打包，也可被引用为节点）；打包时随父模块内置分发（无跨模块依赖）。

本设计：**submodule 节点类型** + **框架两个既有缺口修复**（校验器丢 registry、SubModule 无 guards 通道——submodule 内部要跑 loop 的前置）。

## 定位

| 维度 | 结论 |
|------|------|
| 定义单元 | `SubModule` 类（完整模块能力：spec_schema / harnesses / scripts / commands / guards / tasklist；未来模板等模块能力同属） |
| 节点类型 | tasklist 中 `submodule` 与 harness/script 同级——`{type: "submodule", submodule: "<modules 中的名字>", ...}` |
| 父模块声明 | 类属性 `modules: dict[str, type[SubModule]]`（声明定义在父模块内，无全局注册表）；`Module` 构造参数同样支持（过程式 API） |
| 打包分发 | 父 `pack()` 时把 modules 引用的子模块**递归完整打包进 `submodules/<name>/`**；`ModuleLoader` 递归加载 → 无运行时依赖（"不存在依赖"） |
| 运行语义 | 黑盒嵌入模式：`EventBus.null()` + `keep_records=False` + 无 backend 落盘——内部过程不进审计/快照/回滚，只有终点输出暴露给父图 |
| 与嵌套执行的关系 | async script 内 `await module.run()` 保留为**另一场景**：任务中间过程本身有可复用、需要审计的完整 module 时，module 之间**平级**组合（不存在"sub"） |

## 数据模型

### TaskDefinition 扩展（spec.py）

```python
@dataclass
class TaskDefinition:
    type: Literal["harness", "script", "command", "submodule"]
    submodule: str | None = None             # type="submodule" 时引用名（父模块 modules 解析）
    inputs: dict[str, str] | None = None     # 已有：{子 spec 输入字段: 父图引用}
    outputs: dict[str, str] | None = None    # 新增：{节点输出字段: 子输出字段}；缺省 = 全量
    # 复用已有 LLM 覆盖字段（与 harness 节点同款语义）：
    #   model / temperature / think / api_params —— 传播到子模块内部所有 harness
    # 不传播：promptmode / prompt / outputformat / notdo（harness 级语义，对整组 harness 无意义）
```

### SubModule 扩展（submodule.py）

```python
class SubModule:
    modules: dict[str, type[SubModule]] = {}   # 类属性；__init_subclass__ 复制防污染（harnesses 同款）
    guards: list[tuple[str, Callable]] = []    # 缺口 2 修复：guard 声明（自包含函数）

    async def run(self, spec: dict, *, tasklist=None, audit=False, max_ticks=100,
                  harness_overrides: dict | None = None) -> list[Firing]:
        # harness_overrides：构建 registry 时对每个 harness 批量应用覆盖
        # （model / temperature / think / api_params），同 _register_harness 的覆盖逻辑
```

### Tasklist 语法

```json
{
  "Tasks": {
    "A": { "type": "harness", "harness": "organize", "inputs": {"raw_text": "{spec.raw_text}"} },
    "B": {
      "type": "submodule", "submodule": "fact_review_loop",
      "inputs": {"original_text": "{spec.raw_text}", "draft_text": "A"},
      "outputs": {"text": "text", "attempt": "attempt"},
      "model": "deepseek-chat",
      "temperature": 0.2,
      "think": true,
      "api_params": {"max_tokens": 2000}
    },
    "C": { "type": "script", "script": "finalize", "inputs": {"data": "B"} }
  },
  "Flow": "A --> B --> C"
}
```

- `inputs` 引用语法与 harness 节点一致：`{spec.xxx}` = 父 spec 字段，`"A"` = 上游节点输出
- 无 `outputs` → 节点输出 = 子模块终点输出全量；有则按 `{节点字段: 子字段}` 挑选/重命名
- 下游引用 `B.text` / `B.attempt`，guard 同样可读

### pack 结构

```
dist/academic_writer/
  module.json               # "modules": ["fact_review_loop"]
  harnesses/ scripts/ commands/ guards/
  submodules/
    fact_review_loop/       # 递归完整打包（自身 module.json + 实现 + 递归 submodules/）
```

## 运行机制

### 节点注册（graph_builder）

`_register_body` 增加 `type == "submodule"` 分支 → `_register_submodule(key, task)`：注册 **async body**（与 script 节点同款隔离名 `{module_id}:{key}`），body 内部：

```
view → 渲染 inputs 引用（{spec.xxx} 构建期解析、节点名运行时取输出，机制与 harness inputs 相同）
     → 懒实例化子模块（Module 持有 _submodule_instances 缓存，注入父的 llm_client）
     → await child.run(spec_dict, audit=False, harness_overrides=...)
     → 取 run 结果最后 firing 的输出 dict（子流程终点产出）
     → 应用 outputs 映射（无 outputs = 全量）
     → 返回节点输出
```

### 关键语义

| 点 | 决定 |
|----|------|
| 子模块输出约定 | 节点输出 = 子模块 run 返回 firings 的**最后一个 firing 的输出 dict**（现状 `SubModule.run()` 语义一致）；`outputs` 映射在其上挑选/重命名 |
| outputs 校验 | 构建期校验：`outputs` 的每个值字段必须存在于子模块 `spec_schema.output` 声明（无隐式行为）；缺失 → 构建报错 |
| LLM 配置继承 | 节点级 `model / temperature / think / api_params` 经 `harness_overrides` 传播到子模块内部**所有** harness（覆盖其自身配置）；未写的字段用默认 |
| client 共享 | 子模块实例化注入父的 `llm_client`（同进程同配置）；父无 client 时子模块按既有逻辑 `from_env()` 懒创建 |
| 实例缓存 | 构建时实例化并缓存（`_register_submodule`），同节点多次触发（loop 回边）复用实例——嵌入模式无状态，每次 run 独立 |
| 命名空间 | 子模块实例有独立 `module_id` → body 名前缀隔离，与父图无冲突；嵌套子模块同理递归 |
| guard 引用 | 节点输出是普通 dict，guard 可读（`view.Loop1.value`），submodule 节点可进 loop |
| 事件/审计 | `audit=False` 嵌入模式：`EventBus.null()` + `keep_records=False` + 无 backend——不进审计/快照/回滚 |

### Module 过程式 API 同步支持

```python
Module(spec=..., tasklist=..., modules={"fact_review_loop": FactReviewLoop})
```

`SubModule` 类属性 `modules` 构造时转传给内部 `Module`——tasklist 语法层（`type: "submodule"`）不绑定类式 API。

## 框架缺口修复（前置，模块无关，通用价值）

submodule 内部要跑 loop（如 fact_review_loop），暴露两个既有框架缺口（`2026-08-10-academic-writer-design.md` 已记录，实测复现）：

### 缺口 1：校验器丢 registry（translator.py）

`_check_flow` 解析 flow 时不传 registry → `parse_graph(prepare_flow(flow))`（`translator.py:119`）在空 registry 上校验 guard 名 → 任何带 guard 边的 tasklist 在校验阶段必被拒（`guard 'xxx' not registered`）。**全框架性 bug**——构建路径已传 registry（`graph_builder.py:75`），卡死的只是校验。

```python
# 修复：validate(tasklist, registry) 把 registry 下传给 _check_flow
#       parse_graph(prepare_flow(flow), registry=registry)
```

### 缺口 2：SubModule 无 guards 通道（submodule.py + loader.py）

类式模块没有任何 guard 声明/收集/注册/打包/加载机制（grep 确认 submodule.py / loader.py 无 guard 代码）→ 类式模块写不了带 guard 边的 tasklist → 写不了 loop。

```python
class SubModule:
    guards: list[tuple[str, Callable]] = []   # [(名字, 函数)]，名字 = 注册名 = 打包文件名
```

- `__init_subclass__`：harnesses 同款复制（子类显式定义则覆盖，否则继承基类列表），防就地修改污染父类
- `_build_registry`：`for name, fn in self.guards: reg.guard(name, fn)`
- `pack()`：导出 `guards/{name}.py`（`inspect.getsource(fn)` + `from __future__ import annotations` 头；**guard 函数必须自包含**，pack 单文件导出约束）
- `ModuleLoader`：`_load_guards(p)`（exec 加载 `guards/*.py`，取 `ns[f.stem]`）；guard 名不进入 provides/requires（边引用，不是可消费资源），不参与重复名检测

## 错误处理

| 场景 | 行为 |
|------|------|
| 子模块内部 LLM 失败 | 子 run 正常返回（内部 Failure 已按既有语义传播），节点输出 = 最后 firing 输出（可能含 Failure）→ 下游按 Failure 语义跳过，run 继续 |
| 子模块 `spec_schema.validate()` 失败（父 inputs 契约不匹配） | 节点输出 `Failure(msg, type="infrastructure")` → 对应 ABORTED 停机——父 tasklist 与子契约不匹配是配置错误，不静默继续 |
| submodule 名未在父 `modules` 声明 | **构建期报错**（与 harness 未注册报错同风格） |
| `outputs` 值字段不在子 `spec_schema.output` | **构建期报错**（无隐式行为） |
| 子模块非终止 | 子模块自己的 tasklist 负责终止性（子 run 内部 `max_ticks` 兜底），父节点不暴露 |

## 测试计划

### test_submodule_node.py（新建）

沿用现有测试设施（pytest + MagicMock/AsyncMock，见 conftest.py）：

1. 基本运行：submodule 节点 + mock LLM → 节点输出 = 子终点输出
2. inputs 引用渲染：`{spec.xxx}` 与上游节点输出引用
3. outputs：缺省全量 / 显式挑选重命名；字段不在子 `spec_schema.output` → 构建期报错
4. submodule 名未在 `modules` 声明 → 构建期报错
5. LLM 覆盖：节点 model/temperature 传播到子内部所有 harness（mock 断言收到的请求参数）
6. 嵌入模式：子 run 无 records、无落盘（EventBus null）
7. loop 回边多次触发 submodule 节点（guard 读节点输出）
8. 递归嵌套：子模块自身也有 modules
9. pack → load round-trip：`submodules/` 目录加载后 tasklist 引用可解析、运行一致
10. 过程式 `Module(spec, tasklist, modules=...)` 与类式同效
11. 既有测试全量回归

### 框架缺口修复测试（并入既有测试文件）

1. `_check_flow`：带 guard 边的 tasklist 校验通过（registry 含 guard）；guard 未注册仍报错（`test_translator.py`）
2. `SubModule.guards`：类属性收集（子类覆盖/继承）；`_build_registry` 注册；pack 导出 `guards/*.py`；pack → load round-trip 后 guard 可被 flow 引用（`test_submodule.py`）

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `module_harness/spec.py` | `TaskDefinition.type` 扩为 `Literal["harness","script","command","submodule"]`；新增 `submodule`、`outputs` 字段；from_dict/to_dict 同步 |
| `module_harness/translator.py` | 缺口 1：`_check_flow(tasklist, registry)` 传 registry 给 `parse_graph` |
| `module_harness/submodule.py` | 缺口 2：`guards` 类属性 + 收集 + 注册 + pack 导出；`modules` 类属性 + `__init_subclass__` 复制；`run()` 增加 `harness_overrides` 参数；`pack()` 导出 `submodules/<name>/` 递归 |
| `module_harness/module.py` | `Module.__init__` 新增 `modules: dict[str, type[SubModule] \| SubModule]`；懒实例化缓存（注入共享 llm_client） |
| `module_harness/graph_builder.py` | `_register_body` 增加 submodule 分支 → `_register_submodule`（构建期校验 + async body 注册） |
| `module_harness/loader.py` | 缺口 2：`_load_guards`；`_load_submodules` 递归加载 → 填充实例 `modules` dict；module.json 记录 `"modules": [名字]` |
| `module_harness/__init__.py` | 导出同步 |
| `module_harness/tests/test_submodule_node.py` | 新建：节点级测试 |
| `module_harness/tests/test_translator.py` / `test_submodule.py` | 补缺口修复测试 |
| `docs/progress/module-roadmap.md` | 修正 168-186 决策记录（见下） |
| `docs/superpowers/specs/2026-08-05-submodule-design.md` | 更新定位（双重身份 + modules 声明 + pack 内置） |
| `docs/superpowers/specs/2026-08-10-academic-writer-design.md` | Loop1/Loop2 改为 submodule 节点 |

## 文档更新要点（module-roadmap.md 168-186）

- 记录：原「模块组合讨论与决策」基于错误前提（把声明式节点曲解为"图级组合、仅省几行边"），判定"嵌套执行"替代
- 新决策：**submodule 一等节点类型**（黑盒/嵌入模式/打包内置/与 harness 同级）——组合与封装的声明式能力
- async script 嵌套保留为另一场景：平级 module 组合（可审计）
- 图级组合维持不做

## 设计决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 引用解析 | 父模块类属性 `modules: dict[str, type[SubModule]]` 声明，tasklist 按名字引用（无全局注册表、无完整限定名） |
| 2 | 定义来源 | 完整 `SubModule` 类（内联配置对象不够——submodule 要有自己的 harness/scripts/guards/模板等模块能力）；与"定位上同 harness 同级"不冲突 |
| 3 | 节点输出 | 子模块终点输出全量，可选 `outputs` 字段挑选/重命名 |
| 4 | 分发 | 父 pack 递归内置 `submodules/<name>/`，loader 递归加载——无运行时依赖（"pack 的目的就是把它放在 module 里面"） |
| 5 | 审计 | 嵌入模式（无事件/无 records/无落盘/无快照回滚）——内部过程无审查意义，只有产出有意义 |
| 6 | LLM 配置 | 节点级 model/temperature/think/api_params 传播到子内部所有 harness（与 harness 节点同款覆盖语义）；promptmode/prompt/outputformat/notdo 不传播 |
| 7 | 与嵌套执行关系 | 并存：submodule 节点 = 黑盒处理单元；async script 嵌套 = 平级 module 组合（可审计），不存在"sub" |

## 使用示例

### 定义子模块（双重身份）

```python
class FactReviewLoop(SubModule):
    """独立可复用模块：既是 module，也可被引用为 submodule 节点。"""
    name = "fact_review_loop"
    spec_schema = SpecSchema(
        input={"original_text": "str", "draft_text": "str"},
        output={"text": "str", "attempt": "int", "clean": "bool", "issues_remaining": "list"},
    )
    harnesses = [...], guards = [...]
    tasklist = Tasklist(tasks={...Seed/Merge/Review/Fix...}, flow="...")

# 独立运行 / 独立打包
await FactReviewLoop().run({"original_text": "...", "draft_text": "..."})
FactReviewLoop().pack("dist/fact_review_loop")
```

### 引用为节点

```python
class AcademicWriter(SubModule):
    name = "academic_writer"
    modules = {"fact_review_loop": FactReviewLoop}

    tasklist = Tasklist(tasks={
        "Organize": TaskDefinition(type="harness", harness="organize",
                                   inputs={"raw_text": "{spec.raw_text}"}),
        "Loop1": TaskDefinition(
            type="submodule", submodule="fact_review_loop",
            inputs={"original_text": "{spec.raw_text}", "draft_text": "Organize"},
            outputs={"text": "text", "attempt": "attempt"},
            model="deepseek-chat", temperature=0.2,
        ),
        "Polish": TaskDefinition(type="harness", harness="polish", inputs={"draft": "Loop1"}),
    }, flow="Organize --> Loop1 --> Polish")
```

### 过程式 API

```python
Module(spec=..., tasklist=..., modules={"fact_review_loop": FactReviewLoop})
```
