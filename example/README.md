# example — 实践线模块

两个可运行的示例模块：**fact_review_loop**（SubModule，通用事实审阅循环——
可复用/可打包的处理单元）与 **academic_writer**（普通 Module，双模板——
顶层完整工作流，消费 fact_review_loop 的 submodule 节点）。

## academic_writer：灵感写作 → 学术英语

将中英混杂、混乱重复的灵感式写作文段，逐步整合优化为符合学术英语写作要求的文段。

**一个 module 两种使用方式**（框架原生多模板设置：TemplateLoader 注册多个
TasklistTemplate + `Module(template_name=...)` 选择，翻译器为 script 类型、
确定性返回流程）：

| | 默认（`mode="submodule"`） | 详细模式（`mode="detailed"`） |
|---|---|---|
| 模板名 | `academic_writer` | `academic_writer_detailed` |
| 事实审阅 loop | **submodule 节点**（复用 fact_review_loop 黑盒嵌入） | **内联展开到主图**（Seed1→Merge1→Review1→Fix1 循环 + Exit1；Loop2 同构） |
| 审计 | 只暴露终点输出（attempt/issues_remaining 聚合字段） | **全部节点进审计记录**，逐 tick 可审阅修复过程 |
| 图 | `[A]Organize → Loop1 → Polish → Loop2 → Finalize → Report` | 17 条边 + 4 个互补 guard + `Merge1/2.join: OR` |

两模式流程语义一致：

- **Organize**：去重复、合并碎片、理顺语序、中文表达译英（保留全部信息）
- **Loop1**：原始文段 vs 整理稿事实审阅（信息缺漏 / LLM 幻觉新增 / 事实改动）
  有问题 → 修复 harness → 回审（条件 loop，最多 3 轮）
- **Polish**：学术英语化润色（只改语言，不改事实）
- **Loop2**：原始文段 vs 润色稿事实审阅（与 Loop1 同一个 fact_review harness，
  但为独立节点）
- **Finalize**：原始 + 润色整合终稿（可对语言再调整），输出语言调整说明
- **Report**：确定性聚合 modification_notes（markdown，可审计）

### 使用场景（end user）— 只写 spec 运行

```python
import asyncio
from example.academic_writer import run_writer

async def main():
    firings = await run_writer({
        "raw_text": "中英混杂草稿……",          # 必填
        "target_field": "software engineering",  # 可选：目标领域
        "max_words": 300,                        # 可选：字数上限
    })  # 默认 mode="submodule"；传 mode="detailed" 切换详细模式
    out = firings[-1].output
    print(out["final_text"])            # 最终文段
    print(out["modification_notes"])    # 修改说明（markdown）

asyncio.run(main())
```

或直接跑 demo：

```bash
python -m example.demo_writer                # 真实 LLM（配置 .env / 环境变量）
python -m example.demo_writer --mock         # 免 key 冒烟
python -m example.demo_writer --detailed     # 详细模式（loop 内联，可审计）
python -m example.demo_writer --mock --detailed
```

需要细粒度审阅（每轮修复过程、逐 tick 快照回滚）时用详细模式；只要最终结果时
用默认模式（子模块嵌入，零落盘、开销小）。

### 开发场景（developer）— 复用 / 打包 / 引用

fact_review_loop 是独立可打包的处理单元，任何「文本需对照原文核验」的工作流
可以 submodule 节点引用复用：

```python
from example.fact_review_loop import FactReviewLoop

# 独立运行
out = await FactReviewLoop().run({
    "original_text": "原文……",
    "draft_text": "待审稿……",   # 或 {"text": "..."}（父节点 dict 输出直传）
})[-1].output
# => {"text": ..., "attempt": N, "clean": bool, "issues_remaining": [...]}

# 打包发布（guards/ 随包导出，加载无运行时依赖）
FactReviewLoop().pack("dist/fact_review_loop")

# 在自己的模块中声明引用 + tasklist 直接写 submodule 节点
# （SubModule 类式或 Module 过程式同效）
from module_harness.module import Module
from example.academic_writer import academic_tasklist  # 或自定义 tasklist

mod = Module(
    spec={"raw_text": "……"},
    tasklist=academic_tasklist,
    llm_client=llm_client,                     # create_llm_client(LLMConfig.from_env())
    modules={"fact_review_loop": FactReviewLoop},
    review_harness=None,                       # 固定 tasklist，发布前已验证
)
firings = await mod.run()
```

节点级 LLM 设置（model / temperature / think / api_params）可写在 tasklist 的
submodule 节点上，传播到子模块内部全部 harness。

## 设计说明

- 形态分工：仅 fact_review_loop 是 SubModule（可复用/可打包的处理单元）；
  academic_writer 是普通 Module（顶层工作流），双模板经框架模板通道
  （TemplateLoader + `Module(template_name=...)`）切换，翻译器为 script 类型
  （确定性，`translate` 返回值即流程形式）
- 详细模式内联 loop 的 merge / collect_result / build_report 由闭包工厂
  （`_make_merge` 等）经 `reg.body()` 绑定各自节点名——逻辑与
  fact_review_loop 的 @script 版本保持同步（pack 单文件导出约束后者须自包含）
- 子模块嵌入模式运行（不进审计/快照/回滚，零落盘），只暴露终点输出；
  内部修复明细不进 notes——聚合字段（attempt / issues_remaining）覆盖审阅所需
- 轮次上限 3 为 guard 内联常量（guard 读不到 spec；pack 单文件导出约束）
- 达上限仍有问题时 clean 边强制退出，遗留 issues 逐条收进
  `issues_remaining` / 修改说明，不静默丢弃

## 测试

```bash
python -m pytest example/ -q        # mock 测试（无需 API key）
```

设计文档：`docs/superpowers/specs/2026-08-10-academic-writer-design.md`
