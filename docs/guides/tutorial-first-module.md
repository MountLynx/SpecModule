# 教程：从零到第一个模块

本篇带你从空目录写出第一个可运行的 SpecModule 模块并发布。配套案例（本篇每一步的真实产物）在 [`examples/tutorial/`](../../examples/tutorial/README.md)，边读边对照，或直接照抄。

**前置**：已 `pip install specmodule`（或本仓库源码环境）。不需要 API key——第 4 步的 `--mock` 冒烟免 key 免网络。

---

## 0. 30 秒概念预热

一个 module 是四个概念的组合：

| 概念 | 一句话 | 本篇出现于 |
|------|--------|-----------|
| **spec** | "想要什么"——结构化键值对，无预定义 schema | 第 1 步 |
| **tasklist** | "如何做"——`{Tasks, Flow}`，每个 Task 一个执行节点 | 第 3 步 |
| **harness** | LLM 调用节点（prompt + 输出格式） | 第 2 步 |
| **script** | 纯 Python 函数节点（处理、计算、IO） | 第 2 步 |

执行语义（join、guard、循环、快照）不在此展开，见 [`references/tickflow-integration.md`](../references/tickflow-integration.md)。

## 1. 想清楚 spec（输入契约）

spec 是模块的输入。案例模块"summarizer"的 spec：

```json
{"text": "要总结的文本……", "max_words": 50}
```

字段含义由**你**定义——框架不校验 schema（除非你用 `spec_schema` 声明输入契约，见 submodule 文档）。

## 2. 建模块文件：`modules/summarizer.py`

一个模块一个 py 文件，声明模块级 `entry`（`ModuleEntry`）。CLI 经 `discover_modules()` 扫描 `modules/` 目录发现它。完整代码见 [`examples/tutorial/modules/summarizer.py`](../../examples/tutorial/modules/summarizer.py)，骨架：

```python
from module_harness.cli.entry import ModuleEntry

def build_registry(llm_client, template_name, event_bus):
    """构建执行元件注册表。CLI 传入外部 event_bus（否则收不到 harness 事件）。"""
    from module_harness import HarnessConfig, HarnessRegistry, OutputFormat
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    # 节点 A：harness——LLM 调用。三层 prompt 最小可用：只写 prompt_core。
    reg.harness("summarize", HarnessConfig(
        prompt_core="用不超过 {max_words} 字总结以下文本，输出 JSON {\"summary\": \"...\"}：\n{text}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.3,
    ))

    # 节点 B：script——纯 Python 处理（清洗 harness 输出）
    @reg.script("format_summary")
    def format_summary(view):
        data = view.field("data")  # bind 字段名访问上游节点输出
        if isinstance(data, dict):
            text = str(data.get("summary", "")) or str(data)
        else:
            text = str(data)
        return {"summary": text.strip()}

    return reg

entry = ModuleEntry(
    name="summarizer",
    description="教程示例：LLM 总结模块",
    templates={...},          # 第 3.2 步补
    build_registry=build_registry,
    default_spec={"text": "...", "max_words": 50},  # 无 --spec 时的兜底
    default_template="summarize",
    review_harness=None,      # 固定流程模板，发布前已验证（跳过一致性审核）
)
```

三个要点：

- **`{text}` / `{max_words}` 是 prompt 占位符**，运行时由 Task 的 `inputs` 解析（第 3 步）。
- **script 读上游输出用 bind 字段名**（`view.field("data")`）——字段名即 `task.inputs` 的键，由 graph_builder 写成具名 bind 供数；producer 节点名不是 view 访问名。
- **`review_harness=None`**：自定义 tasklist 默认经 LLM 一致性审核（spec↔tasklist 语义检查）；固定流水线模板发布前已验证，置 `None` 跳过。想保留审核就不设此字段（默认 `"spec_tasklist_review"`，需注册内置审核 harness，见 `register_review_harness`）。

## 3. 写 tasklist：`{Tasks, Flow}`

tasklist 是流程控制。两个节点：A（harness 总结）→ B（script 清洗）。

```json
{
  "Tasks": {
    "A": {
      "type": "harness",
      "harness": "summarize",
      "inputs": {"text": "{spec.text}", "max_words": "{spec.max_words}"},
      "outputformat": {"type": "json_object"}
    },
    "B": {"type": "script", "script": "format_summary", "inputs": {"data": "A"}}
  },
  "Flow": "[A] --> B"
}
```

- **Task 字段**：`type`（harness/script/command/submodule）+ 元件引用（`harness`/`script`）+ 覆盖项（`promptmode`/`prompt`/`temperature`/`outputformat`…）。字段全集见 [`references/spec-harness-syntax.md`](../references/spec-harness-syntax.md) 的 TaskDefinition 表。
- **`{spec.xxx}` 引用**：运行时把 spec 字段解析进 Task 输入。
- **`Flow` 是 tickflow DSL**：`[A]` = 起始节点，`-->` = 恒 True 数据流边，`--|guard|-->` = 条件边。执行语义（AND/OR join、死锁陷阱）见 [`references/tickflow-integration.md`](../references/tickflow-integration.md)。

> **一条边只有一种数据流**：B 的 `inputs: {"data": "A"}` 里 `data` 是 bind 字段名（供 prompt 占位符与 script 的 `view.field("data")` 读），`A` 是 producer 节点名。多输入节点写法：`"inputs": {"a": "A", "b": "B"}`，script 侧对应 `view.field("a")` / `view.field("b")`。

## 4. `--mock` 冒烟：免 key 跑通

`--mock` 用内置假 LLM 客户端（`json_object` 输出宽松合法 JSON，`text` 返回占位文本）——**验证流水线形状，不验证内容**。

```bash
# 直写通道：--tasklist 跳过翻译，按给定 tasklist 运行
python -m module_harness.cli run --module summarizer \
  --modules-dir examples/tutorial/modules --tasklist examples/tutorial/tasklist.json --mock --verbose 2
```

预期输出（tick 0 跑 A、tick 1 跑 B，B 输出清洗后的 summary）：

```
tick 0  A                        ✓  output={"result": "mock output", "summary": "mock", ...}
tick 1  B                        ✓  output={"summary": "mock"}
```

### 3.2 补：模板通道（spec-only）

模块的 `templates` 声明"翻译通道"：用户只给 spec，模板把它翻译成 tasklist。案例用 **script 翻译器**（确定性、零 LLM 成本）——在 `build_registry` 里注册翻译器 script，模板 `tasklist` 字段与直写同构：

```python
@reg.script("tl_summarize")
def tl_summarize(view):
    return TASKLIST  # 与 tasklist.json 同构的 dict

# entry.templates = {
#   "summarize": {
#       "translation": {"type": "script", "script": "tl_summarize"},
#       "tasklist": TASKLIST,
#   },
# }
```

然后省略 `--tasklist` 直接跑（默认 `entry.default_template`）：

```bash
python -m module_harness.cli run --module summarizer \
  --modules-dir examples/tutorial/modules --mock
```

两种输入模式（spec-only 翻译 vs spec+tasklist 直写）的选择依据见 [`concepts/SpecModule.md`](../concepts/SpecModule.md)；翻译器两种类型（script 确定性 / harness LLM 动态）见 [`references/spec-harness-syntax.md`](../references/spec-harness-syntax.md)「模板语法」。

## 5. 真 LLM 运行

配置 provider/key（`.env` 或环境变量；回退链与字段说明见 [`guides/config-guide.md`](config-guide.md)），去掉 `--mock`：

```bash
python -m module_harness.cli run --module summarizer \
  --modules-dir examples/tutorial/modules --verbose 2
```

## 6. 观察与调试

每次运行落盘到 `.specmodule/runs/<run_id>/`（默认 run_id = 模块名），用查询命令审阅：

```bash
python -m module_harness.cli status --run-id summarizer     # 运行状态（阶段/tick/节点）
python -m module_harness.cli review --run-id summarizer     # tick 时间线
python -m module_harness.cli review --failed                # 只看失败节点
python -m module_harness.cli checkpoints --run-id summarizer # 可用回退点
python -m module_harness.cli rollback 2 --module summarizer \
  --modules-dir examples/tutorial/modules --mock             # 回退到 tick 2 重跑
```

- **每 tick 落盘轻量快照**：`rollback <tick>` 精确回退到任意 tick（只重跑未执行部分）。
- 运行中 `Ctrl+C` 中断后可用 `resume` 续跑（缺省续最新）。

## 7. 发布到 store

模块写好后发布，别的项目/使用者即可安装运行：

```bash
python -m module_harness.cli publish --module summarizer --modules-dir examples/tutorial/modules
```

发布与安装的完整闭环（setup → publish → install → run → update → uninstall）见 [`guides/store-walkthrough.md`](store-walkthrough.md)。

---

**下一步**：读 [`guides/store-walkthrough.md`](store-walkthrough.md) 完成闭环；理解概念读 [`concepts/SpecModule.md`](../concepts/SpecModule.md)；写复杂流程（分支/循环/多模块）前先读 [`references/tickflow-integration.md`](../references/tickflow-integration.md)。
