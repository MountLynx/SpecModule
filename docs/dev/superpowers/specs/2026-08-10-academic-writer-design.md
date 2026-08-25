# 灵感写作 → 学术英语（academic_writer）设计文档

> 日期：2026-08-10 | 状态：已实现（example/，mock 测试 + demo 入口；实现计划见
> docs/dev/superpowers/plans/2026-08-10-academic-writer-example.md）
> 状态更新（2026-08-10）：两个框架缺口已修复（随框架实现落地，见 2026-08-10-submodule-node-design.md）；Loop1/Loop2 组合方式改为 submodule 一等节点（本设计的嵌套执行表述作废）。
> 状态更新（2026-08-10 晚）：**外壳修正**——`academic_writer` 改为普通 `Module`（过程式组装），
> **仅 `fact_review_loop` 是 SubModule**。修正前把两个模块都定义成 SubModule 类，系基于错误的
> submodule 认知（零件/整机不分）：SubModule = 可复用/可打包的处理单元（零件）；academic_writer
> = 顶层完整工作流（整机），消费 submodule 节点但自身不做类式声明/打包。submodule 节点机制不变。
> 关联：实践线 M1 论文优化的前置练习；开发提炼（submodule guard 能力）的首次落地

## 概述

制作两个 example module（新 `example/` 目录，不放在 tests）：

1. **`fact_review_loop`** — 独立可复用 submodule：给定原始文段与待审文段，循环执行"事实审阅 → 问题修复 → 回审"，直到无事实问题或达最大轮数。任何"文本需对照原文核验"的工作流（M1 论文优化等）可引用为 submodule 节点复用。
2. **`academic_writer`** — 完整流水线**普通 Module**（过程式组装）：将中英混杂、混乱重复的灵感式写作文段，经「整理 → 事实审阅 loop → 学术润色 → 事实审阅 loop → 整合终稿」产出学术英语文段 + 修改说明两个输出变量。Loop1/Loop2 为 **submodule 一等节点**（tasklist 内 `{type: "submodule", submodule: "fact_review_loop", inputs: {...}}` 引用 + `Module(modules={"fact_review_loop": FactReviewLoop})` 声明解析）；节点级 LLM 配置（model/temperature/think/api_params）可传播到子模块内部所有 harness。**仅 fact_review_loop 是 SubModule**（2026-08-10 修正）。

框架修复（两个缺口，模块无关的通用价值）：`_check_flow` 丢 registry、`SubModule` 无 guards 通道——**均已随框架实现落地**（见 2026-08-10-submodule-node-design.md），本 example 不再涉及框架改动。

## 背景

- 引擎（tickflow）原生支持条件 loop：guarded edge（`--|g|-->`）、OR-join（`Node.join: OR`）、`latest` 输入策略（循环中读到"上一轮输出"）、guard 视图含全部节点最新输出 + 源节点 state。**引擎层无需任何改动。**
- 同一 harness 被多个 task 节点引用是原生能力（`task.harness="fact_review"` + task 级 inputs/prompt 覆盖）——满足"同一个 harness 但不是同一个 node"。
- 框架现实约束：
  - **script 节点读不到 spec**（`graph_builder._register_script` 不解析 `{spec.xxx}`；常量引用只对 harness 生效）→ loop 模块的种子稿需经转发 harness 节点入图。
  - **guard 读不到 spec** → 最大轮数 MAX_ATTEMPTS 为代码常量（3），不进 spec 契约。
  - **submodule 节点类型**（2026-08-10-submodule-node-design.md）支持黑盒引用；两阶段 loop 复用同一 fact_review_loop 处理单元

## 范围

- 框架修复：`module_harness/translator.py`（_check_flow 传 registry）、`module_harness/submodule.py`（guards 收集/注册/打包）、`module_harness/loader.py`（guards 加载）——**已随框架实现落地**（见 2026-08-10-submodule-node-design.md），本 example 不再包含框架改动。
- 新模块：`example/fact_review_loop.py`（SubModule，可复用处理单元）、`example/academic_writer.py`（普通 Module 过程式组装，顶层工作流；含 demo、mock 测试、示例文本、README）。
- **不包含**：图级模块组合能力（独立 spec，触发条件 = M1/M2 真实需要）、模板通道、CLI。

## 框架修复

> 状态更新（2026-08-10）：以下两个缺口均已修复（随框架实现落地，见 2026-08-10-submodule-node-design.md）。

### 缺口 1：_check_flow 丢失 registry（translator.py）

```python
# 现状：parse_graph(prepare_flow(flow))          # 无 registry → guard 边必报错
# 修复：parse_graph(prepare_flow(flow), registry=registry)
```

`_check_flow` 增加 `registry` 参数（`validate(tasklist, registry)` 已有该参数，未下传）。校验语义不变：结构/孤立节点/语法检查 + guard/body 引用完整性（后者由 tickflow parser 完成，需要 registry）。

### 缺口 2：SubModule guards 支持（submodule.py + loader.py）

```python
class SubModule:
    guards: list[tuple[str, Callable]] = []   # [(名字, 函数)]，名字 = 注册名 = 打包文件名
```

- `__init_subclass__`：按 harnesses 同款复制（子类显式定义则覆盖，否则继承基类列表），防就地修改污染父类。
- `_build_registry`：`for name, fn in self.guards: reg.guard(name, fn)`。
- `pack()`：导出 `guards/{name}.py`（与 scripts 同机制：`inspect.getsource(fn)` + `from __future__ import annotations` 头）。**约束：guard 函数必须自包含**（pack 单文件导出，不能引用同文件模块级辅助函数——example 中显式写成自包含函数）。
- `ModuleLoader`：新增 `_load_guards(p)`（exec 加载 `guards/*.py`，取 `ns[f.stem]`），动态类加 `"guards": guards`。guard 名**不进入** provides/requires（它们是边引用，不是可消费资源），不参与重复名检测。

## 模块一：fact_review_loop（FactReviewLoop(SubModule)）

**定位**：开发场景产物——通用可复用组件，独立打包发布；也是使用场景产物——可直接写 spec 运行。

**spec 契约**：`input: {original_text: str, draft_text: str}`；`output: {text: str, attempt: int, clean: bool, issues_remaining: list}`

**图**：

```
[Seed] --> Merge --> Review --|has_issues|--> Fix --> Merge
          Review --|clean|--> Exit
          Merge.join: OR
```

| 节点 | 类型 | 引用 | 输入 | 说明 |
|------|------|------|------|------|
| Seed | harness | `seed_draft` | `{draft: "{spec.draft_text}"}` | 原样转发种子稿（script 读不到 spec；即使 LLM 转发失真，后续审阅会抓出并修复——loop 自愈） |
| Merge | script | `merge` | `{seed: Seed, fixed: Fix}` | 修复稿优先于种子稿；`view.state["attempt"] += 1` 计轮次；输出 `{"draft": str, "attempt": int}` |
| Review | harness | `fact_review` | `{draft: Merge, original: "{spec.original_text}"}` | 原始 vs 当前稿逐句对比；输出 `{"issues": [...], "clean": bool}` |
| Fix | harness | `fix_issues` | `{draft: Merge, issues: Review}` | 按 issues 逐条修复；输出 `{"text": str}` |
| Exit | script | `collect_result` | `{review: Review, merge: Merge}` | 输出 `{"text": 当前稿, "attempt": N, "clean": review.clean, "issues_remaining": issues 若 clean=false 否则 []}` |

**guards**（自包含、可打包，绑定本模块固定节点名 Review/Merge）：

```python
def has_issues(view):  # issues 非空 且 未达上限（3 轮）
    issues = view.Review.value.get("issues", [])
    attempt = view.Merge.value.get("attempt", 0)
    return bool(issues) and attempt < 3

def clean(view):       # 与 has_issues 严格互补（XOR 分支不得双走）
    # 必须内联 has_issues 逻辑：pack 逐文件单函数导出，跨函数引用在加载后
    # 失效（clean.py 单独 exec 时 has_issues 未定义）；两处判断同步修改
    issues = view.Review.value.get("issues", [])
    attempt = view.Merge.value.get("attempt", 0)
    return not (bool(issues) and attempt < 3)
```

- **上限 3 轮为函数内联常量**——pack 单文件导出（`inspect.getsource(fn)`）只含函数体，同文件模块级常量也不会被导出；跨文件 import 同样失效。guard/script 读不到 spec，故上限不进契约。
- 达上限仍有 issues 时：clean 边触发 → Exit 把遗留 issues 收进 `issues_remaining`，不静默丢弃。
- 兜底：`Module.run(max_ticks=...)` 防非终止。

**harnesses**：

| harness | prompt 要点 | 输出 |
|---------|-----------|------|
| `seed_draft` | 原样输出，禁止任何修改 | `{"text": str}` |
| `fact_review` | 原始 vs 当前稿逐句对比：信息缺漏（omission）/ 幻觉新增（hallucination）/ 事实改动（alteration）；每条附原文与当前稿引文；`clean` 仅当零问题 | `{"issues": [{type, detail, quote_original, quote_draft}], "clean": bool}` |
| `fix_issues` | 按 issues 逐条修复：补遗漏、删幻觉、还原事实；不引入新事实；不改动未被点名内容 | `{"text": str}` |

## 模块二：academic_writer（普通 Module 过程式组装，Loop1/Loop2 为 submodule 节点）

**定位**：使用场景产物——end user 只写 spec（raw_text）运行完整流水线。

**形态（2026-08-10 修正）**：`academic_writer` 是**顶层完整工作流**（整机），不是可复用处理单元
（零件）——用普通 `Module` + **双模板**（框架模板通道：`TemplateLoader` 注册 +
`Module(template_name=...)` 选择，翻译器为 script 类型、确定性返回流程），
**不定义 `AcademicWriter(SubModule)` 类**。只有 `fact_review_loop` 是
SubModule（可打包、可被任意模块引用）；academic_writer 消费它，自身不做类式声明/打包/被引用。
修正前版本基于错误认知把两个模块都做成 SubModule（零件/整机不分）。

**两种使用方式（2026-08-10 实现时确认，用户需求"一个 module 多种使用方式"）**：
- **`academic_writer`（默认）**——loop 以 submodule 节点复用（黑盒嵌入，只暴露终点输出）
- **`academic_writer_detailed`（详细模式）**——事实审阅 loop **内联展开到主图**
  （Seed1→Merge1→Review1→Fix1 循环 + Exit1；Loop2 同构），全部节点进审计记录，
  逐 tick 可审阅修复过程；`run_writer(spec, mode="detailed")` 切换

**submodule 节点组合**：tasklist 内 Loop1/Loop2 直接写 `{type: "submodule", submodule:
"fact_review_loop", inputs: {...}}`；`modules` 解析表经 `Module(modules=...)` 传入（无全局注册表）
——两阶段复用同一 `fact_review_loop` **处理单元**（黑盒嵌入运行，不进审计/快照/回滚，只暴露
终点输出）。节点级 LLM 设置（model/temperature/think/api_params）传播到子模块内部所有 harness；
`outputs` 字段可从终点输出挑选/重命名（缺省全量）。

**spec 契约**：`input: {raw_text: str, target_field: str?, max_words: int?}`；`output: {final_text: str, modification_notes: str}`

**图**：

```
[A]Organize --> Loop1 --> Polish --> Loop2 --> Finalize --> Report
```

（Loop1 / Loop2 各自是 `fact_review_loop` 的完整 loop 子图：Seed → Merge → Review --|has_issues|--> Fix → Merge，Review --|clean|--> Exit，Merge.join: OR——节点与 guard 均在子模块内部，父图只见黑盒。）

| 节点 | 类型 | 引用 | 输入 | 说明 |
|------|------|------|------|------|
| Organize | harness | `organize` | `{raw_text: "{spec.raw_text}"}` + 可选字段 | 中英混杂 → 逻辑通顺英文，保留全部信息，不增删事实 |
| Loop1 / Loop2 | submodule | `fact_review_loop`（**同一处理单元，两个节点**） | `{original_text: "{spec.raw_text}", draft_text: Organize\|Polish}` | 事实审阅 loop；节点输出 = 子流程终点输出全量（text/attempt/clean/issues_remaining），可选 `outputs` 挑选/重命名 |
| Polish | harness | `polish` | `{draft: Loop1}` + 可选字段 | 学术英语化（正式、精确、学术句式），事实不变 |
| Finalize | harness | `finalize` | `{original: "{spec.raw_text}", draft: Loop2}` | 原始+润色整合终稿，可微调语言，不得改事实；输出终稿 + 语言调整说明 |
| Report | script | `build_report` | `{finalize: Finalize, loop1: Loop1, loop2: Loop2}` | 聚合生成 markdown 修改说明（确定性、可审计，见下） |

**guards**：父模块无需声明——loop 的 guards（has_issues/clean，上限 3 内联）内聚在 `fact_review_loop` 内部（自包含、可打包，见模块一）；这是 submodule 一等节点相比"按阶段重声明节点与 guard"的核心收益。

**modification_notes（markdown，由 build_report 确定性聚合）**：
- 整理阶段：原始 vs 整理稿（无 LLM 说明，结构重组由 organize 完成）
- 阶段 1 审阅：发现 issues 数、修复轮数（Loop1.attempt）、最终 verdict（clean 或"达上限，遗留 N 项"，Loop1.issues_remaining）
- 阶段 2 审阅：同上（Loop2.attempt / Loop2.issues_remaining）
- 整合阶段：finalize 的语言调整说明（LLM 输出）
- 遗留问题逐条列出（若达上限未清）
- 每轮修复明细不进 notes——子模块嵌入模式内部过程不进审计（零落盘），聚合字段（attempt / issues_remaining）已覆盖审阅所需

**可选字段**（target_field / max_words）：spec 缺省时 prompt 占位符渲染为空/占位文案（实现时验证渲染行为，缺省不得报错）。

## 设计决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | loop 形态 | **独立 submodule + submodule 一等节点组合**（2026-08-10 修正，用户确认）；图级组合能力维持不做（用户定位 submodule 为黑盒处理单元，非图结构复用） |
| 2 | 阶段 4 审阅 | 对称 loop（用户确认）：润色后同样带"审阅 → 修复 → 回审" |
| 3 | 轮次上限 | guard 函数内联常量 3（guard/script 读不到 spec；pack 单文件导出约束）；达上限强制走 clean 边退出，遗留 issues 进 `issues_remaining`/notes |
| 4 | guard 互补性 | `clean = not has_issues` 严格互补——两 guarded 出边同时为 True 会让 XOR 分支双走（Fix 与下游同时触发） |
| 5 | 循环合并节点 | Merge（OR-join：一次性种子 + loop 回边）——引擎文档的标准"loop 成员 + 一次性种子"模式；不用 LLM 条件判断（无隐式行为） |
| 6 | 种子入图 | 转发 harness 节点（script 读不到 spec 的框架约束）；失真由 loop 自愈 |
| 7 | 修改说明 | script 确定性聚合（可审计）而非 LLM 生成；每轮明细不进 notes——子模块嵌入模式内部过程不进审计（零落盘），聚合字段（attempt / issues_remaining）覆盖审阅所需 |
| 8 | guard 归属 | guards 内聚在 fact_review_loop 内部（绑定 Review/Merge 固定节点名）；父模块经 submodule 节点引用，无需按阶段重声明（submodule 一等节点核心收益） |
| 9 | academic_writer 外壳 | **普通 Module（过程式组装），不是 SubModule**（2026-08-10 修正，用户确认）：SubModule = 可复用/可打包的处理单元（零件），academic_writer = 顶层工作流（整机）；修正前把两者都定义为 SubModule 系错误认知 |
| 10 | 文本节点输出格式 | **长文本 harness（seed_draft / fix_issues / organize / polish）用 `OutputFormat(type="text")`，仅结构化节点（fact_review 的 issues/clean、finalize 的 text+notes）用 json_object**（2026-08-10 实现时确认）：deepseek `json_object` 模式偶发返回空白/非 JSON（实测复现：seed 转发全文时 `Failure(llm)` 阻断子模块），长文本 JSON 包裹放大该风险；text 类型无格式要求、失败率趋零，script 侧已有 str 防御 |
| 11 | 详细模式 tasklist 模板 | **同 module 双模板（框架模板通道）：`academic_writer`（submodule 黑盒）与 `academic_writer_detailed`（loop 内联展开，全程可审计）**（2026-08-10 实现时确认，用户需求"一个 module 多种使用方式"）：翻译器为 script 类型、确定性返回流程（`translate` 返回值即 tasklist 形式）；内联 loop 的 merge/collect_result/build_report 经闭包工厂（`_make_merge` 等）`reg.body()` 绑定节点名——view[field_name] 在 tickflow 语义下恒为 Missing（graph_builder 注释），故不能用 field 名解耦脚本；闭包逻辑与 fact_review_loop 的 @script 版本同步维护（pack 单文件导出约束后者须自包含） |

## 错误处理

| 场景 | 行为 |
|------|------|
| 审阅永远报 issues | 第 3 轮后 clean 强制触发退出；遗留 issues 显式列出 |
| guard 双走（理论不可能，防御） | 互补实现保证唯一 |
| LLM 输出缺字段（issues/clean/text） | script 侧 `dict.get` 缺省 + 类型防御；guard 对非 dict 输出返回 False（安全侧） |
| LLM 输出非 JSON | 长文本节点用 text 类型消除（D10）；结构化节点（fact_review/finalize）偶发失败 → `Failure(llm)` 阻断下游，重跑运行 |
| 非终止 | `Module.run(max_ticks=...)` 兜底（demo 传 100） |
| spec 缺可选字段 | 渲染为空，不报错 |

## 测试计划

**框架修复**：已随框架实现落地（test_validator.py / test_submodule.py / test_submodule_node.py，见 2026-08-10-submodule-node 计划），本 example 不再涉及。

**example mock 测试**（无 key 可跑，MagicMock 客户端逐节点返回预设输出）：
1. `test_loop.py`：
   - 正常路径：review 首轮报 issues → fix 触发 → 二轮 clean → Exit 输出 `text/attempt=2/clean=True/issues_remaining=[]`
   - 达上限路径：review 恒报 issues → attempt=3 后退出 → `issues_remaining` 非空
2. `test_writer.py`：
   - 正常路径：两阶段均一次 clean → 最终输出 final_text + modification_notes 两变量；`fact_review_loop` 被调用两次（两个 submodule 节点）
   - 修复路径：阶段 1 loop 先 issues 后 clean → 子模块内部 Fix 触发且循环收敛
   - 达上限路径：阶段 2 loop 恒 issues → notes 含遗留问题（Loop2.issues_remaining）

**demo（真实 LLM，deepseek）**：`demo_loop.py` / `demo_writer.py` 跑 `sample_raw_text.txt`（中英混杂示例草稿），打印各节点输出与最终两变量。

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `example/__init__.py` | 空包 |
| `example/fact_review_loop.py` | FactReviewLoop(SubModule) + 自包含 guards + 内联上限 |
| `example/academic_writer.py` | 普通 Module 双模板（模板通道）：`academic_tasklist` / `detailed_tasklist`（Loop1/Loop2 submodule 节点 / loop 内联展开）+ 翻译器 + 闭包工厂 + `run_writer(spec, mode=...)` |
| `example/demo_loop.py` | loop 模块真实运行入口 |
| `example/demo_writer.py` | 完整流水线真实运行入口 |
| `example/sample_raw_text.txt` | 示例灵感草稿（中英混杂、重复） |
| `example/test_loop.py` / `example/test_writer.py` | mock 验证 |
| `example/README.md` | 两级用户使用说明（开发场景：类定义/pack；使用场景：spec 运行） |
| `docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md` | 本设计 |

框架文件变更（translator.py / submodule.py / loader.py / module_harness/tests）已随框架实现落地，不在此列。

## 使用示例

```python
import asyncio

# 使用场景：end user 只写 spec 运行完整流水线（普通 Module，过程式组装）
from example.academic_writer import academic_tasklist, run_writer
from module_harness.module import Module

async def main():
    # 方式一：run_writer 入口（内部构造 Module，llm_client 缺省从 env 创建）
    firings = await run_writer({"raw_text": "……中英混杂草稿……"})
    report = firings[-1].output          # {"final_text": ..., "modification_notes": ...}

    # 方式二：直接过程式组装（等价，可自行控制参数）
    mod = Module(
        spec={"raw_text": "……中英混杂草稿……"},
        tasklist=academic_tasklist,
        llm_client=llm_client,           # create_llm_client(LLMConfig.from_env())
        modules={"fact_review_loop": FactReviewLoop},   # submodule 节点解析表
        review_harness=None,             # 固定 tasklist，发布前已验证
    )
    firings = await mod.run()

asyncio.run(main())

# 开发场景：loop 模块独立复用/打包（SubModule 双重身份：独立运行 / 可被引用）
from example.fact_review_loop import FactReviewLoop
firings = await FactReviewLoop().run({"original_text": "...", "draft_text": "..."})
FactReviewLoop().pack("dist/fact_review_loop")     # 发布

# 开发场景：新模块以 submodule 节点引用复用（SubModule 类式或 Module 过程式同效）
class PaperOptimizer(SubModule):
    name = "paper_optimizer"
    modules = {"fact_review_loop": FactReviewLoop}
    spec_schema = SpecSchema(input={"paper": "str"}, ...)
    tasklist = Tasklist(tasks={
        "Loop": TaskDefinition(
            type="submodule", submodule="fact_review_loop",
            inputs={"original_text": "{spec.paper}", "draft_text": "..."},
        ),
        ...
    }, flow="...")
```
