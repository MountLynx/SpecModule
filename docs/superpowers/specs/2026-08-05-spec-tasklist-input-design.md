# spec + 自定义 tasklist 输入通道 与 一致性审核 设计

> 日期：2026-08-05 | 状态：已确认，待实现
> 对应 roadmap：#1（spec + 自定义 tasklist 输入）+ #4（一致性审核）

## 背景

当前 `Module` 只接受 `spec + template_name` 一种输入通道：模板 → 翻译 → tasklist。
缺少"用户直接提供完整 tasklist，跳过翻译"的通道。同时，用户自定义的 tasklist
是否真的实现了 spec 的目标，需要 LLM 审核——这就是一致性审核（#4）。

本次将 #1 与 #4 一起实现：#1 的"一致性检查"步骤正是 #4 的落地。

## 语义：完全自定义

自定义 tasklist 通道下，用户传入**完整** `Tasklist`（Tasks + Flow），不经过翻译、
不经过模板：

```
用户自定义 Tasklist（Tasks + Flow）
        │
        ├─ TasklistValidator 结构/引用校验（必做，复用已有）
        ├─ LLM 一致性审核（可选开关，本次新增）
        │
        ▼
TasklistTranslator.build(tasklist, spec) → AsyncRunner
```

- **Flow 字段是完整 tickflow DSL**：多起点、AND/OR join（`C.join: OR`）、
  guard 边（`B--|g1|-->C`，guard 函数由用户在 registry 注册）、环、固定火次输入。
  用户完全掌控 graph 结构。
- **spec 的双重角色**：数据源（task 字段中 `{spec.xxx}` 引用，`graph_builder` 已支持）
  与审核参照（LLM 判断 tasklist 是否实现 spec 目标）。
- **不是"模板 + 局部覆盖"模式**：基于模板的部分覆盖合并语义复杂，roadmap 未列入，本次不做。

## 范围

**包含**：
- `Module` 新增 `tasklist` 输入通道（与 `template_name` 互斥）
- 独立审核 harness（`spec_tasklist_review`）+ 内置审核配置
- `ConsistencyReviewer`、`ConsistencyReport`、`ConsistencyError`
- `ConsistencyReviewed` 事件（EventBus）
- 测试 + roadmap 状态更新

**不包含**：模板局部覆盖、对齐检查（#2）、submodule（#3/#5）、快照回滚封装（#6）。

## 数据模型

### ConsistencyReport

```python
@dataclass
class ConsistencyReport:
    consistent: bool       # 审核是否通过
    suggestions: str       # LLM 建议（不通过时的问题描述）
    raw: str               # LLM 原始输出（进 audit 链）
```

### ConsistencyError

```python
class ConsistencyError(ValueError):
    """一致性审核未通过。携带完整 report。"""
    def __init__(self, report: ConsistencyReport): ...
    # str() 输出含 suggestions，便于直接展示问题描述
```

## 审核 harness：独立通道（方案 A）

用户通过 Roadmap #4 的字面方案是"复用 `spec_to_tasklist` + 新 prompt_mode"，
但存在三个问题：用户注册的 `spec_to_tasklist` 无 `prompt_modes`（键缺失即
KeyError，隐式契约不可发现）；审核 prompt 需要比 mode 字符串更丰富的结构；
script 翻译场景根本没有 `spec_to_tasklist` harness。故采用**独立审核 harness**：

```python
# module_harness/consistency.py
REVIEW_HARNESS_CONFIG = HarnessConfig(
    prompt_core="""你是一致性审核器。判断给定 tasklist 是否能实现 spec 的目标。
审核要点：
1. spec 的每个目标/需求是否被 tasklist 中的任务覆盖
2. task 中引用的字段（{spec.xxx}、inputs）在 spec 中是否存在
3. flow 是否可达、是否有死路或未定义节点
spec: {spec}
tasklist: {tasklist}
输出 JSON：{"consistent": true/false, "suggestions": "..."}""",
    output_format=OutputFormat(type="json_object"),
    temperature=0.1,
)

def register_review_harness(reg: HarnessRegistry,
                            name: str = "spec_tasklist_review") -> None:
    """用户显式注册内置审核 harness。等价于 reg.harness(name, REVIEW_HARNESS_CONFIG)。"""
```

- 默认注册名 `spec_tasklist_review`，`Module` 的 `review_harness` 参数可覆盖为
  用户自定义的更严格审核 harness。
- 审核 harness 未注册 → 明确 `ValueError`（不静默跳过，符合"无隐式行为"）。

## ConsistencyReviewer

```python
class ConsistencyReviewer:
    def __init__(self, registry: HarnessRegistry,
                 harness_name: str = "spec_tasklist_review") -> None: ...

    async def review(self, spec: Spec, tasklist: Tasklist) -> ConsistencyReport:
        # 1. harness 未注册 → ValueError（提示 register_review_harness）
        # 2. view = DictView({
        #       "spec": Resolved(value=spec.to_dict(), k=None),
        #       "tasklist": Resolved(value=json.dumps(tasklist, ensure_ascii=False), k=None),
        #    }, node="__review__")
        # 3. 调 body（不走 tickflow，与 Translator 同模式）
        #    - Failure / LLMError → 抛异常（审核失败即阻塞，不降级放行）
        # 4. json.loads 解析 → 校验 consistent(bool) / suggestions(str) 字段
        #    - 非 JSON / 缺字段 / 类型错 → 抛异常
        # 5. 返回 ConsistencyReport(consistent, suggestions, raw)
```

`tasklist` 以 JSON 字符串注入 view——占位符 `{spec}` / `{tasklist}` 由
`PromptRenderer` 的 `{(\w+)}` 替换机制填充。

## Module 变更

```python
class Module:
    def __init__(
        self,
        spec: dict[str, Any],
        *,
        template_name: str | None = None,          # 改为关键字可选
        tasklist: Tasklist | None = None,          # 新增
        llm_client: Any,
        event_bus: EventBus | None = None,
        template_loader: TemplateLoader | None = None,
        module_id: str | None = None,
        registry: HarnessRegistry | None = None,
        review_harness: str | None = "spec_tasklist_review",  # None = 关闭语义审核
    ) -> None:
        if (template_name is None) == (tasklist is None):
            raise ValueError("template_name 与 tasklist 必须且只能传一个")
        ...
        self.tasklist = tasklist
        self.review_harness = review_harness
        self.review_result: ConsistencyReport | None = None
```

`_build_runner_async()` 分支：

```
tasklist 通道：
    1. TasklistValidator.validate(tasklist, reg) — 结构/引用校验，失败 ValueError
    2. if self.review_harness is not None:
           report = await ConsistencyReviewer(reg, self.review_harness).review(spec, tasklist)
           self.review_result = report
           bus.emit(ConsistencyReviewed(consistent=..., suggestions=..., raw=...))
           if not report.consistent:
               raise ConsistencyError(report)
    3. TasklistTranslator.build(tasklist, spec) → AsyncRunner

template 通道：不变（模板 → 翻译 → build → runner）
```

## EventBus 事件

`events.py` 新增（与现有 typed dataclass 事件同模式）：

```python
@dataclass
class ConsistencyReviewed:
    timestamp: float
    node: str            # "__review__"
    tick: int
    consistent: bool
    suggestions: str
    raw: str
```

## 错误处理

| 场景 | 行为 |
|------|------|
| `template_name` 与 `tasklist` 都传 / 都不传 | `ValueError`（构造时） |
| 审核 harness 未注册 | `ValueError`（提示 `register_review_harness`） |
| 审核 LLM 调用失败（LLMError） | 抛异常（阻塞，不降级放行） |
| 审核输出非 JSON / 缺字段 / 类型错 | 抛异常（阻塞） |
| `consistent=false` | `ConsistencyError`（携带 suggestions），`module.review_result` 保留报告 |
| tasklist 结构/引用校验失败 | `ValueError`（复用 `TasklistValidator`，与翻译通道一致） |

## 测试计划

### test_consistency.py（新建）

- `review` 正常返回 report（mock LLM 返回 `{"consistent": true, ...}`）
- `consistent=false` → `ConsistencyError` 且 report 保留
- LLM 返回非 JSON → 抛异常
- 缺 `consistent` / `suggestions` 字段、类型错 → 抛异常
- harness 未注册 → `ValueError`
- LLMError（infrastructure）→ 抛异常
- `register_review_harness` 注册后 reviewer 可用

### test_module.py（追加）

- tasklist 通道：构建 runner 成功（mock 审核通过），`review_result` 已填充
- tasklist 通道：审核不通过 → `ConsistencyError`
- tasklist 通道：`review_harness=None` → 跳过审核直接构建
- `template_name` + `tasklist` 都传 / 都不传 → `ValueError`
- tasklist 引用未注册 harness → `ValueError`（结构校验）

## 文件变更清单

```
module_harness/consistency.py        ★ 新建 — REVIEW_HARNESS_CONFIG,
                                        register_review_harness,
                                        ConsistencyReviewer, ConsistencyReport,
                                        ConsistencyError
module_harness/events.py             + 新增 ConsistencyReviewed 事件
module_harness/module.py             + tasklist/review_harness 参数 + 分支
module_harness/__init__.py           + 导出新符号
module_harness/tests/test_consistency.py  ★ 新建
module_harness/tests/test_module.py  + tasklist 通道测试
docs/progress/module-roadmap.md      + #1 #4 标记完成
```

## 与已有模块的关系

- **translator.py**：零修改。`TasklistValidator` 复用；审核不走 `Translator`。
- **graph_builder.py**：零修改。`TasklistTranslator.build(tasklist, spec)` 已支持
  `{spec.xxx}` 引用解析与输入绑定。
- **tickflow**：零修改。审核是 Module 层调用，不走 runner。
- **llm**：审核复用同一 LLM 客户端，无新依赖。
