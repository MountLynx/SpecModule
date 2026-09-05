# Academic Writer Example Module 实现计划

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新建 `example/` 目录落地两个可运行 module——`fact_review_loop`（通用事实审阅循环 submodule）与 `academic_writer`（灵感式写作 → 学术英语完整流水线，含两阶段事实审阅 loop）。

**Architecture:** 框架侧（submodule 一等节点、guards、pack/loader）已实现且测试全绿（342 passed），本计划纯新增 `example/` 包，零框架改动。**仅 `fact_review_loop` 是 `SubModule`**（可打包复用的黑盒处理单元：Seed→Merge→Review→Fix 条件 loop，2 个互补 guard）；**`academic_writer` 是普通 `Module` 过程式组装**（2026-08-10 外壳修正：顶层工作流不定义 SubModule 类，`Module(spec, academic_tasklist, modules={"fact_review_loop": FactReviewLoop})`，tasklist 内 Loop1/Loop2 以 `{type: "submodule"}` 引用同一处理单元——同一个 `fact_review` harness，两个节点实例）。所有 LLM 文本节点用 `json_object` 输出；script/guard 侧 `dict.get` 缺省 + 类型防御（设计文档错误处理表）。

**Tech Stack:** Python 3.13, asyncio, pytest + unittest.mock（AsyncMock/MagicMock）, tickflow 引擎（零修改）。

**前置阅读：** 设计文档 `docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md`（状态：已确认，待实现）；框架实现 `module_harness/submodule.py`（`SubModule.guards/modules` 类属性、`run(persist=False)`、`pack()` 导出 guards//submodules/）。

**关键框架事实（已核实）：**
- DSL：guard 边 `--|guard|-->`、OR-join 声明 `Merge.join: OR`（test_checkpoint.py:404 同款）
- harness 输入：`inputs={"draft": "Merge"}` → prompt `{draft}` 占位符运行时渲染 producer 输出（`str()`）；`inputs={"original": "{spec.xxx}"}` 构建期解析（缺失字段 → None → 渲染 "None"，prompt 需写明"None 表示未提供"）
- submodule 节点：`inputs` 作为子模块 spec 传入；常量引用构建期解析，节点引用（如 `"Organize"`）运行时取 `view[producer].value`（子模块 spec_schema 中对应字段需声明为 `"any"` 以接受 dict 输出）
- guard 打包约束：注册名 = 函数名；函数必须模块级、自包含（pack 用 `inspect.getsource` 单文件导出，上限常量必须内联在函数体）
- script 打包约束：`@script(name)` 装饰器函数名 = 注册名；body 内禁止模块级常量引用（跨文件 import 在 pack 后失效）
- guard/script 读不到 spec：`{spec.xxx}` 只对 harness 生效
- 测试注入：`mock_llm.complete = AsyncMock(side_effect=fn)`，`fn(**kwargs)` 按 prompt 关键词分发 `LLMResponse(content=..., usage={}, finish_reason="end_turn")`

---

## 文件结构

| 文件 | 职责 | 变更 |
|------|------|------|
| `example/__init__.py` | 空包标记 | 新建（T1） |
| `example/test_loop.py` | fact_review_loop mock 测试（正常/修复/达上限/防御/pack roundtrip） | 新建（T1） |
| `example/fact_review_loop.py` | FactReviewLoop(SubModule)：3 harnesses + 2 guards + 2 scripts + tasklist | 新建（T1） |
| `example/test_writer.py` | academic_writer mock 测试（正常/修复/达上限/spec 校验） | 新建（T2） |
| `example/academic_writer.py` | 普通 Module 过程式组装：模块级 `academic_tasklist`（Loop1/Loop2 submodule 节点）+ `run_writer()` 入口（内部构造 `Module(spec, tasklist, modules=...)`） | 新建（T2） |
| `example/sample_raw_text.txt` | 示例灵感草稿（中英混杂、重复、碎片化） | 新建（T3） |
| `example/demo_loop.py` | loop 真实运行入口（`--mock` 免 key 冒烟） | 新建（T3） |
| `example/demo_writer.py` | 完整流水线真实运行入口（`--mock` 免 key 冒烟） | 新建（T3） |
| `example/README.md` | 两级用户使用说明（开发场景/使用场景） | 新建（T4） |
| `docs/dev/progress/module-roadmap.md` | 「本次 example 计划」标记完成 | 修改（T5） |
| `docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md` | 状态 待实现 → 已实现 | 修改（T5） |

测试命令：`python -m pytest example/ -q`（本计划任务通用）；全量回归 `python -m pytest module_harness/tests/ -q`（约 5 分钟，T5 执行）。

---

## Task 1: FactReviewLoop（example 包 + test_loop.py TDD）

**Files:**
- Create: `example/__init__.py`
- Create: `example/test_loop.py`
- Create: `example/fact_review_loop.py`
- Test: `example/test_loop.py`

- [ ] **Step 1: 写失败测试**（`example/test_loop.py` + 空包 `example/__init__.py`）

`example/__init__.py`：

```python
# example 包：落地实践线 module（fact_review_loop / academic_writer）。
```

`example/test_loop.py`：

```python
"""fact_review_loop 模块 mock 测试（无 key 可跑，MagicMock 逐节点预设输出）。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.spec import SpecValidationError

from example.fact_review_loop import FactReviewLoop


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage={}, finish_reason="end_turn")


def _make_side_effect(review_bodies: list[str], fix_text: str = "draft fixed"):
    """按 prompt 关键词分发：seed 原样转发（text 类型，返回纯文本）/
    review 按给定序列返回 JSON / fix 返回修复稿纯文本。"""
    review_calls = {"n": 0}

    async def side_effect(**kwargs):
        prompt = kwargs.get("prompt", "")
        if "原样转发" in prompt:
            return _resp("draft v1")
        if "修复者" in prompt:
            return _resp(fix_text)
        # 审阅：按 review_bodies 序列依次返回
        body = review_bodies[min(review_calls["n"], len(review_bodies) - 1)]
        review_calls["n"] += 1
        return _resp(body)

    return side_effect


class TestFactReviewLoop:
    @pytest.mark.asyncio
    async def test_clean_first_round(self, mock_llm):
        """首轮审阅即 clean：Seed → Review → Exit，无 Fix 触发。"""
        mock_llm.complete.side_effect = _make_side_effect(
            ['{"issues": [], "clean": true}'])
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out == {
            "text": "draft v1", "attempt": 1, "clean": True, "issues_remaining": [],
        }

    @pytest.mark.asyncio
    async def test_fix_then_clean(self, mock_llm):
        """首轮报 issues → Fix 触发 → 二轮 clean → 输出 attempt=2。"""
        mock_llm.complete.side_effect = _make_side_effect(
            [
                '{"issues": [{"type": "omission", "detail": "缺结论", '
                '"quote_original": "…", "quote_draft": "…"}], "clean": false}',
                '{"issues": [], "clean": true}',
            ],
            fix_text='{"text": "draft v2 fixed"}',
        )
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out == {
            "text": "draft v2 fixed", "attempt": 2, "clean": True, "issues_remaining": [],
        }

    @pytest.mark.asyncio
    async def test_max_attempts_exit_with_issues(self, mock_llm):
        """审阅恒报 issues → 第 3 轮后 clean 边强制退出，遗留 issues 进 issues_remaining。"""
        issues_body = (
            '{"issues": [{"type": "hallucination", "detail": "杜撰数据", '
            '"quote_original": "无", "quote_draft": "85%"}], "clean": false}'
        )
        mock_llm.complete.side_effect = _make_side_effect([issues_body])
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=50,
        ))[-1].output
        assert out["clean"] is False
        assert out["attempt"] == 3
        assert out["issues_remaining"] == [{
            "type": "hallucination", "detail": "杜撰数据",
            "quote_original": "无", "quote_draft": "85%",
        }]

    @pytest.mark.asyncio
    async def test_review_missing_fields_safe(self, mock_llm):
        """审阅输出缺 issues/clean 字段：不循环、不崩溃（防御侧返回 False）。"""
        mock_llm.complete.side_effect = _make_side_effect(['{"foo": 1}'])
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out["attempt"] == 1
        assert out["issues_remaining"] == []
        assert out["clean"] is False

    @pytest.mark.asyncio
    async def test_missing_spec_field_raises(self, mock_llm):
        """缺 original_text → SpecValidationError（spec 契约校验）。"""
        with pytest.raises(SpecValidationError):
            await FactReviewLoop(llm_client=mock_llm).run(
                {"draft_text": "draft v0"}, persist=False, max_ticks=20)

    def test_pack_exports_guards(self, tmp_path):
        """pack 导出 guards/*.py（注册名 = 函数名 = 文件名）。"""
        dist = FactReviewLoop().pack(tmp_path / "dist")
        assert (dist / "guards" / "has_issues.py").is_file()
        assert (dist / "guards" / "clean.py").is_file()
        ns: dict = {}
        exec(compile(
            (dist / "guards" / "has_issues.py").read_text(encoding="utf-8"),
            "has_issues.py", "exec"), ns)
        assert callable(ns["has_issues"])

    @pytest.mark.asyncio
    async def test_pack_load_roundtrip_runs(self, tmp_path, mock_llm):
        """pack → load roundtrip 后 guard 可解析、loop 照常运行。"""
        from module_harness.loader import ModuleLoader

        dist = FactReviewLoop().pack(tmp_path / "dist")
        loaded = ModuleLoader(llm_client=mock_llm).load(dist)
        assert {name for name, _ in loaded.guards} == {"has_issues", "clean"}
        mock_llm.complete.side_effect = _make_side_effect(
            ['{"issues": [], "clean": true}'])
        out = (await loaded.run(
            {"original_text": "orig", "draft_text": "d"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out["text"] == "draft v1"
        assert out["clean"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_loop.py -q`
Expected: 全部 FAIL（`ModuleNotFoundError: No module named 'example.fact_review_loop'`）

- [ ] **Step 3: 实现**（`example/fact_review_loop.py`）

```python
# example/fact_review_loop.py
"""FactReviewLoop — 通用事实审阅循环 submodule。

spec 契约：input {original_text: str, draft_text: str|dict}；
output {text, attempt, clean, issues_remaining}。

图（`Merge.join: OR` = 一次性种子 + loop 回边的标准 loop 成员）::

    [Seed] --> Merge --> Review --|has_issues|--> Fix --> Merge
                          |                                  ^
                          |--|clean|--> Exit                 |
                          +----------------------------------+

- Seed    harness 转发种子稿（script 读不到 spec；LLM 转发失真由 loop 自愈）
- Merge   script 合并：修复稿优先于种子稿，计数轮次
- Review  harness 事实审阅：原始 vs 当前稿逐句对比（缺漏/幻觉/改动）
- Fix     harness 按 issues 逐条修复
- Exit    script 收集终点输出

任一「文本需对照原文核验」的工作流可引用本模块为 submodule 节点复用。
"""

from __future__ import annotations

from typing import Any

from module_harness.config import HarnessConfig
from module_harness.outputfmt import OutputFormat
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SubModule, script


def has_issues(view: Any) -> bool:
    """issues 非空且未达上限（3 轮）→ 走修复边。

    上限为函数内联常量（guard 读不到 spec；pack 单文件导出只含函数体，
    不能引用模块级常量）。达上限仍有 issues 时 clean 边触发退出，
    遗留 issues 由 Exit 收进 issues_remaining，不静默丢弃。
    """
    review = view["Review"].value
    issues = review.get("issues", []) if isinstance(review, dict) else []
    merge = view["Merge"].value
    attempt = merge.get("attempt", 0) if isinstance(merge, dict) else 0
    return bool(issues) and attempt < 3


def clean(view: Any) -> bool:
    """与 has_issues 严格互补（XOR 分支不得双走）。

    注意：必须内联 has_issues 逻辑（pack 逐文件单函数导出，跨函数引用在
    加载后失效）——两处判断必须保持同步修改。
    """
    review = view["Review"].value
    issues = review.get("issues", []) if isinstance(review, dict) else []
    merge = view["Merge"].value
    attempt = merge.get("attempt", 0) if isinstance(merge, dict) else 0
    return not (bool(issues) and attempt < 3)


SEED_DRAFT_CONFIG = HarnessConfig(
    name="seed_draft",
    prompt_core=(
        "你是文档处理管线中的转发节点。原样转发以下「待审稿」内容，禁止任何修改、"
        "删减、补充、翻译或重新组织。\n"
        "若「待审稿」是 JSON 对象（含 text 字段），只转发其中的 text 字段内容；"
        "若为纯文本，直接原样输出。\n"
        "直接输出文本内容本身，不要用 JSON 包裹，不要添加任何解释、前后缀或标记。\n\n"
        "待审稿：{draft}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["修改内容", "删减内容", "补充内容", "翻译", "添加解释"],
)

FACT_REVIEW_CONFIG = HarnessConfig(
    name="fact_review",
    prompt_core=(
        "你是学术写作管线中的事实审阅者。将「原始文段」与「当前稿」逐句对比，"
        "只报告以下三类事实问题：\n"
        "1. omission 信息缺漏：原始文段有、当前稿缺失的信息；\n"
        "2. hallucination 幻觉新增：当前稿有、原始文段没有的信息（杜撰）；\n"
        "3. alteration 事实改动：同一信息被改写成与原意不符。\n\n"
        "每条问题含：type（omission/hallucination/alteration）、detail（说明）、"
        "quote_original（原文引文）、quote_draft（当前稿引文）。\n"
        "没有任何事实问题时 issues 为空数组、clean 为 true。\n\n"
        "原始文段：{original}\n\n"
        "当前稿（JSON 对象，取其 draft 字段）：{draft}"
    ),
    output_format=OutputFormat(type="json_object"),
    notdo=["报告语言风格问题", "报告结构问题", "报告用词问题"],
)

FIX_ISSUES_CONFIG = HarnessConfig(
    name="fix_issues",
    prompt_core=(
        "你是学术写作管线中的修复者。按「问题列表」逐条修复「当前稿」：\n"
        "- omission：补回缺失信息；\n"
        "- hallucination：删除杜撰内容；\n"
        "- alteration：还原为原始文段事实。\n\n"
        "约束：只改动被点名内容，不引入原始文段中没有的新事实，"
        "不重写未被点名内容的措辞。\n"
        "直接输出修复后的完整文本内容本身，不要用 JSON 包裹，"
        "不要添加任何解释、前后缀或标记。\n\n"
        "当前稿（JSON 对象，取其 draft 字段）：{draft}\n\n"
        "问题列表（JSON 对象，取其 issues 字段）：{issues}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["新增原文没有的事实", "修改未被点名内容", "改动事实", "添加解释"],
)


class FactReviewLoop(SubModule):
    """通用事实审阅循环：原始 vs 当前稿 → 发现问题 → 修复 → 回审。"""

    name = "fact_review_loop"
    version = "0.1.0"
    description = (
        "给定原始文段与待审文段，循环执行「事实审阅 → 问题修复 → 回审」，"
        "直到无事实问题或达最大轮数（3）。"
    )
    spec_schema = SpecSchema(
        input={"original_text": "str", "draft_text": "any"},
        output={
            "text": "str",
            "attempt": "int",
            "clean": "bool",
            "issues_remaining": "list",
        },
    )
    harnesses = [SEED_DRAFT_CONFIG, FACT_REVIEW_CONFIG, FIX_ISSUES_CONFIG]
    guards = [("has_issues", has_issues), ("clean", clean)]
    tasklist = Tasklist(
        tasks={
            "Seed": TaskDefinition(
                type="harness",
                harness="seed_draft",
                inputs={"draft": "{spec.draft_text}"},
            ),
            "Merge": TaskDefinition(
                type="script",
                script="merge",
                inputs={"seed": "Seed", "fixed": "Fix"},
            ),
            "Review": TaskDefinition(
                type="harness",
                harness="fact_review",
                inputs={"draft": "Merge", "original": "{spec.original_text}"},
            ),
            "Fix": TaskDefinition(
                type="harness",
                harness="fix_issues",
                inputs={"draft": "Merge", "issues": "Review"},
            ),
            "Exit": TaskDefinition(
                type="script",
                script="collect_result",
                inputs={"review": "Review", "merge": "Merge"},
            ),
        },
        flow=(
            "[Seed] --> Merge\n"
            "Merge --> Review\n"
            "Review --|has_issues|--> Fix\n"
            "Fix --> Merge\n"
            "Review --|clean|--> Exit\n"
            "Merge.join: OR"
        ),
    )

    @script("merge")
    def merge(view: Any) -> dict[str, Any]:
        """合并输入：修复稿优先于种子稿（Fix 首次未触发时用 Seed）；计数轮次。"""
        try:
            fixed = view["Fix"].value
        except (KeyError, AttributeError):
            fixed = None
        if isinstance(fixed, dict) and fixed.get("text"):
            draft = fixed["text"]
        elif isinstance(fixed, str) and fixed:
            draft = fixed
        else:
            seed = view["Seed"].value
            draft = seed if isinstance(seed, str) else (
                seed.get("text", "") if isinstance(seed, dict) else ""
            )
        n = view.state.get("attempt", 0) + 1
        view.state["attempt"] = n
        return {"draft": draft, "attempt": n}

    @script("collect_result")
    def collect_result(view: Any) -> dict[str, Any]:
        """收集终点输出：当前稿 + 轮次 + verdict + 遗留 issues（达上限未清时）。"""
        review = view["Review"].value
        merge = view["Merge"].value
        issues = review.get("issues", []) if isinstance(review, dict) else []
        clean_flag = review.get("clean", False) if isinstance(review, dict) else False
        return {
            "text": merge.get("draft", "") if isinstance(merge, dict) else "",
            "attempt": merge.get("attempt", 0) if isinstance(merge, dict) else 0,
            "clean": bool(clean_flag),
            "issues_remaining": [] if clean_flag else issues,
        }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_loop.py -q`
Expected: 7 passed（clean / fix-then-clean / max-attempts / missing-fields-safe / missing-spec / pack-guards / roundtrip-runs）

- [ ] **Step 5: 提交**

```bash
git add example/__init__.py example/test_loop.py example/fact_review_loop.py
git commit -m "feat: example/fact_review_loop — 通用事实审阅循环 submodule（审阅→修复→回审 loop，guard 上限 3 轮内联，pack 自包含）"
```

---

## Task 2: academic_writer 过程式组装（test_writer.py TDD）

**Files:**
- Create: `example/test_writer.py`
- Create: `example/academic_writer.py`
- Test: `example/test_writer.py`

> 形态注记（2026-08-10 修正）：academic_writer 是**普通 Module**（过程式组装，
> 模块级 `academic_tasklist` + `run_writer()` 入口），**不是 SubModule 类**；
> 仅 fact_review_loop 是 SubModule。过程式形态下 script 需先注册进 registry
> （`reg.script("build_report")(build_report)`）；Module 不做 spec_schema 校验
> （校验只在 SubModule.run 发生），故不设缺字段报错测试，改为结构测试
> （tasklist 含两个 submodule 节点）。

- [ ] **Step 1: 写失败测试**（`example/test_writer.py`）

```python
"""academic_writer 模块 mock 测试（无 key 可跑，MagicMock 逐节点预设输出）。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse

from example.academic_writer import academic_tasklist, run_writer


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage={}, finish_reason="end_turn")


RAW = (
    "灵感草稿：我们 propose 一个方法，方法很好。它 can 提高 accuracy，"
    "accuracy 提升明显，非常 impressive。"
)


class TestAcademicWriter:
    def test_tasklist_has_two_submodule_nodes(self):
        """Loop1/Loop2 两个 submodule 节点引用同一 fact_review_loop（结构校验）。"""
        subs = [t for t in academic_tasklist.tasks.values() if t.type == "submodule"]
        assert len(subs) == 2
        assert all(t.submodule == "fact_review_loop" for t in subs)

    def _run_with(self, mock_llm, review_plan: dict[str, list[str]]):
        """review_plan：{"loop1": [审阅响应...], "loop2": [审阅响应...]}
        按阶段分发审阅；其余节点按关键词返回预设输出。"""
        review_calls = {"loop1": 0, "loop2": 0}

        def _next_review(stage: str):
            bodies = review_plan[stage]
            body = bodies[min(review_calls[stage], len(bodies) - 1)]
            review_calls[stage] += 1
            return _resp(body)

        async def side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            # 分发键必须是各 prompt_core 独有引导短语——不能选用户文本
            # 可能出现的词（如"灵感草稿"），因为 {original} 占位符会把
            # raw_text 渲染进 Review/Finalize 的 prompt。
            # 文本节点（organize/seed/fix/polish）为 text 类型 → 返回纯文本；
            # 仅 finalize/review 为 json_object → 返回 JSON。
            if "整理成逻辑通顺的英文文段" in prompt:
                return _resp("organized draft")
            if "原样转发" in prompt:
                # 子模块 Seed 的 draft 是父节点输出 —— 原样转发
                return _resp("child draft")
            if "修复者" in prompt:
                return _resp("child draft fixed")
            if "学术英语写作规范" in prompt:
                return _resp("polished draft")
            if "整合输出最终版本" in prompt:
                return _resp('{"text": "final version", "notes": "将被动语态改为主动语态"}')
            # 审阅：按调用顺序区分 loop1 / loop2
            if "逐句对比" in prompt:
                if review_calls["loop1"] < len(review_plan["loop1"]):
                    return _next_review("loop1")
                return _next_review("loop2")
            return _resp('{"issues": [], "clean": true}')

        mock_llm.complete.side_effect = side_effect

    @pytest.mark.asyncio
    async def test_normal_path_two_variables(self, mock_llm):
        """两阶段均一次 clean → 输出 final_text + modification_notes 两变量；
        fact_review harness 被两个 submodule 节点各调用一次（同一 harness，两个节点）。"""
        self._run_with(mock_llm, review_plan={
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        out = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=50,
        ))[-1].output
        assert set(out) == {"final_text", "modification_notes"}
        assert out["final_text"] == "final version"
        notes = out["modification_notes"]
        assert "阶段 1 审阅" in notes and "阶段 2 审阅" in notes
        assert "通过（无事实问题）" in notes
        assert "被动语态改为主动语态" in notes  # finalize 的语言调整说明
        review_prompts = [
            c.kwargs.get("prompt", "") for c in mock_llm.complete.await_args_list
            if "逐句对比" in c.kwargs.get("prompt", "")
        ]
        assert len(review_prompts) == 2  # 同一 harness、两个节点各触发一次

    @pytest.mark.asyncio
    async def test_loop1_fix_path(self, mock_llm):
        """阶段 1 loop 先 issues 后 clean → notes 记 2 轮、结论通过。"""
        self._run_with(mock_llm, review_plan={
            "loop1": [
                '{"issues": [{"type": "omission", "detail": "缺实验细节", '
                '"quote_original": "…", "quote_draft": "…"}], "clean": false}',
                '{"issues": [], "clean": true}',
            ],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        out = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=50,
        ))[-1].output
        notes = out["modification_notes"]
        assert "事实审阅轮数：2" in notes
        assert "通过（无事实问题）" in notes

    @pytest.mark.asyncio
    async def test_loop2_max_attempts_notes_remaining(self, mock_llm):
        """阶段 2 loop 恒 issues → 达上限退出，notes 含遗留问题明细。"""
        self._run_with(mock_llm, review_plan={
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": [
                '{"issues": [{"type": "hallucination", "detail": "杜撰引用文献", '
                '"quote_original": "无", "quote_draft": "[1]"}], "clean": false}',
            ],
        })
        out = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=80,
        ))[-1].output
        notes = out["modification_notes"]
        assert "达上限未清" in notes
        assert "杜撰引用文献" in notes  # 遗留问题逐条列出
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_writer.py -q`
Expected: 全部 FAIL（`ModuleNotFoundError: No module named 'example.academic_writer'`）

- [ ] **Step 3: 实现**（`example/academic_writer.py`）

```python
# example/academic_writer.py
"""academic_writer — 灵感式写作 → 学术英语完整流水线（普通 Module 过程式组装）。

仅 fact_review_loop 是 SubModule（可复用处理单元）；本文件是**顶层工作流**（整机）：
模块级 `academic_tasklist`（Loop1/Loop2 为 submodule 节点）+ `run_writer()` 入口
（内部构造 `Module(spec, tasklist, modules={"fact_review_loop": FactReviewLoop})`）。

spec 契约：input {raw_text: str}（可选未声明字段 target_field / max_words，
传入即生效，缺省时 prompt 占位符渲染 None 并提示忽略）；
output {final_text: str, modification_notes: str}。

图::

    [Organize] --> Loop1 --> Polish --> Loop2 --> Finalize --> Report

Loop1/Loop2 复用同一 fact_review_loop 处理单元（黑盒嵌入运行，不进审计/
快照/回滚，只暴露终点输出）——同一 fact_review harness、两个节点实例。
"""

from __future__ import annotations

from typing import Any

from llm import LLMConfig, create_llm_client

from module_harness.config import HarnessConfig
from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.outputfmt import OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist

from .fact_review_loop import FactReviewLoop

ORGANIZE_CONFIG = HarnessConfig(
    name="organize",
    prompt_core=(
        "你是学术写作助手。把以下「灵感草稿」整理成逻辑通顺的英文文段："
        "去除重复、合并碎片、理顺语序、把中文表达翻译为英文。\n"
        "必须保留草稿中的全部信息，不得新增任何草稿中没有的事实或观点，"
        "不得遗漏任何要点。\n"
        "目标领域（值为 None 表示未提供，忽略此要求）：{target_field}\n"
        "字数上限（值为 None 表示未提供，不设限）：{max_words}\n"
        "直接输出整理后的英文文段本身，不要用 JSON 包裹，"
        "不要添加任何解释、前后缀或标记。\n\n"
        "灵感草稿：{raw_text}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["新增事实", "遗漏要点", "改变原意", "添加解释"],
)

POLISH_CONFIG = HarnessConfig(
    name="polish",
    prompt_core=(
        "你是学术英语写作专家。把以下「整理稿」润色为符合学术英语写作规范的"
        "文段：正式、精确、句式多样、逻辑衔接自然。\n"
        "约束：只改变语言表达，不得改变任何事实、不得增删信息点。\n"
        "直接输出润色后的文段本身，不要用 JSON 包裹，"
        "不要添加任何解释、前后缀或标记。\n\n"
        "整理稿（JSON 对象，取其 text 字段）：{draft}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["改变事实", "新增信息", "删减信息", "添加解释"],
)

FINALIZE_CONFIG = HarnessConfig(
    name="finalize",
    prompt_core=(
        "你是学术写作助手。基于「原始文段」与「润色后文段」整合输出最终版本：\n"
        "- 以润色后文段为主体；\n"
        "- 对照原始文段核验：遗漏的信息点补回，多余/杜撰的内容删除；\n"
        "- 可对语言做最后一次微调，但不得改变事实。\n\n"
        "输出两个字段：\n"
        "- text：最终文段；\n"
        "- notes：本次整合阶段的语言调整说明（简述改了哪些语言表达，为何）。\n\n"
        "原始文段：{original}\n\n"
        "润色后文段（JSON 对象，取其 text 字段）：{draft}"
    ),
    output_format=OutputFormat(type="json_object"),
    notdo=["改变事实", "杜撰信息"],
)


def build_report(view: Any) -> dict[str, Any]:
    """确定性聚合修改说明（markdown）——script 聚合可审计，不用 LLM 生成。"""
    f_out = view["Finalize"].value
    l1_out = view["Loop1"].value
    l2_out = view["Loop2"].value
    finalize = f_out if isinstance(f_out, dict) else {}
    loop1 = l1_out if isinstance(l1_out, dict) else {}
    loop2 = l2_out if isinstance(l2_out, dict) else {}

    def stage(name: str, loop: dict[str, Any]) -> str:
        attempt = loop.get("attempt", 0)
        verdict = "通过（无事实问题）" if loop.get("clean") else "达上限未清"
        remaining = loop.get("issues_remaining", [])
        lines = [f"### {name}", f"- 事实审阅轮数：{attempt}", f"- 结论：{verdict}"]
        if remaining:
            lines.append("- 遗留问题：")
            for i, issue in enumerate(remaining, 1):
                detail = issue.get("detail", issue) if isinstance(issue, dict) else issue
                lines.append(f"  {i}. {detail}")
        return "\n".join(lines)

    notes = "\n\n".join([
        "# 修改说明",
        "## 处理流程",
        "1. 整理：中英混杂灵感草稿 → 逻辑通顺英文文段（保留全部信息）",
        "2. 阶段 1 事实审阅：原始文段 vs 整理稿（循环修复至无事实问题或达上限）",
        "3. 学术润色：整理稿 → 学术英语文段（只改语言，不改事实）",
        "4. 阶段 2 事实审阅：原始文段 vs 润色稿（同上）",
        "5. 整合：原始 + 润色 → 最终版本（语言微调）",
        stage("阶段 1 审阅（整理稿）", loop1),
        stage("阶段 2 审阅（润色稿）", loop2),
        "## 整合阶段语言调整",
        str(finalize.get("notes", "")).strip() or "（无说明）",
    ])
    return {
        "final_text": str(finalize.get("text", "")),
        "modification_notes": notes,
    }


academic_tasklist = Tasklist(
    tasks={
        "Organize": TaskDefinition(
            type="harness",
            harness="organize",
            inputs={
                "raw_text": "{spec.raw_text}",
                "target_field": "{spec.target_field}",
                "max_words": "{spec.max_words}",
            },
        ),
        "Loop1": TaskDefinition(
            type="submodule",
            submodule="fact_review_loop",
            inputs={
                "original_text": "{spec.raw_text}",
                "draft_text": "Organize",
            },
        ),
        "Polish": TaskDefinition(
            type="harness",
            harness="polish",
            inputs={"draft": "Loop1"},
        ),
        "Loop2": TaskDefinition(
            type="submodule",
            submodule="fact_review_loop",
            inputs={
                "original_text": "{spec.raw_text}",
                "draft_text": "Polish",
            },
        ),
        "Finalize": TaskDefinition(
            type="harness",
            harness="finalize",
            inputs={
                "original": "{spec.raw_text}",
                "draft": "Loop2",
            },
        ),
        "Report": TaskDefinition(
            type="script",
            script="build_report",
            inputs={"finalize": "Finalize", "loop1": "Loop1", "loop2": "Loop2"},
        ),
    },
    flow=(
        "[Organize] --> Loop1\n"
        "Loop1 --> Polish\n"
        "Polish --> Loop2\n"
        "Loop2 --> Finalize\n"
        "Finalize --> Report"
    ),
)


def _build_registry(llm_client: Any) -> HarnessRegistry:
    """注册本流水线的 harness 与 script（过程式形态需显式构造 registry）。"""
    reg = HarnessRegistry(llm_client=llm_client, event_bus=EventBus.null())
    for hc in (ORGANIZE_CONFIG, POLISH_CONFIG, FINALIZE_CONFIG):
        reg.harness(hc.name, hc)
    reg.script("build_report")(build_report)
    return reg


def run_writer(
    spec: dict[str, Any],
    *,
    llm_client: Any = None,
    max_ticks: int = 100,
    persist: bool = True,
):
    """构造并运行 academic_writer（普通 Module 过程式组装），返回 firings 列表。

    - llm_client 缺省从 env 创建（LLMConfig.from_env）
    - persist=False：零落盘快速模式（测试/演示用）
    - review_harness=None：固定 tasklist，发布前已验证，跳过一致性审核
    """
    if llm_client is None:
        llm_client = create_llm_client(LLMConfig.from_env())
    mod = Module(
        spec=spec,
        tasklist=academic_tasklist,
        llm_client=llm_client,
        registry=_build_registry(llm_client),
        modules={"fact_review_loop": FactReviewLoop},
        review_harness=None,
        persist=persist,
        status_file=persist,
    )
    return mod.run(max_ticks=max_ticks)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_writer.py -q`
Expected: 4 passed（two-submodule-nodes / normal-path / loop1-fix-path / loop2-max-attempts）

- [ ] **Step 5: 提交**

```bash
git add example/test_writer.py example/academic_writer.py
git commit -m "feat: example/academic_writer — 普通 Module 过程式组装（Loop1/Loop2 submodule 节点复用 fact_review_loop，输出 final_text + modification_notes）"
```

---

## Task 3: 示例文本与 demo 入口（真实 LLM + --mock 冒烟）

**Files:**
- Create: `example/sample_raw_text.txt`
- Create: `example/demo_loop.py`
- Create: `example/demo_writer.py`

- [ ] **Step 1: 写示例灵感草稿**（`example/sample_raw_text.txt`）

```text
灵感草稿：关于用大模型做 code review 的一些想法。

我们 propose 一个基于 LLM 的代码评审系统。这个系统，嗯，主要是自动 review pull request。代码评审是很重要的，code review 能发现 bug，能提升代码质量。现在很多团队 code review 都靠人肉，很慢，而且 review 的质量取决于 reviewer 的经验。

我们的方法：用 LLM 先分析 diff，然后生成 comments。comments 按 severity 分类，比如 critical、warning、suggestion。我们做了实验，在 200 个 PR 上测了，效果不错。实验结果 accuracy 达到 85%，比 baseline 高 15 个百分点。baseline 是一个简单的规则系统。85% 的 accuracy 意味着大部分问题都能被发现，准确率 85% 说明方法有效。

我们用了 few-shot 的方法，prompt 里有例子。还有，我们分析了 failure cases，发现主要问题是 context 不够，比如缺少整个 repo 的背景知识。上下文不足会导致漏报。

总结：LLM-based code review 是可行的，我们的系统能减少人工负担。未来工作：引入 repo-level context，做更大规模的实验。更大规模实验。
```

- [ ] **Step 2: 写 demo_loop.py**

```python
# example/demo_loop.py
"""fact_review_loop 真实运行示例。

用法（在仓库根目录）：
    python -m example.demo_loop            # 真实 LLM（.env / 环境变量）
    python -m example.demo_loop --mock     # 假 LLM（无需 key，冒烟演示数据流）
"""

from __future__ import annotations

import sys

from llm.client import LLMResponse

from example.fact_review_loop import FactReviewLoop

ORIGINAL = (
    "The proposed method achieves 85% accuracy on the benchmark, "
    "outperforming the baseline rule-based system by 15 percentage points. "
    "It runs in under 2 seconds per query."
)
DRAFT = (
    "Our method achieves 85% accuracy, which is 15% higher than the baseline. "
    "It is also very fast and can run in less than 5 seconds. "
    "The system is quite impressive."
)


def _mock_client():
    """免 key 假客户端：按 prompt 关键词返回预设输出。"""
    from unittest.mock import AsyncMock, MagicMock

    async def complete(prompt: str | None = None, **kwargs) -> LLMResponse:
        p = prompt or ""
        if "修复者" in p:
            content = (
                "Our method achieves 85% accuracy, outperforming the baseline "
                "rule-based system by 15 percentage points. "
                "It runs in under 2 seconds per query."
            )
        elif "原样转发" in p:
            content = DRAFT
        else:  # 审阅
            content = (
                '{"issues": [{"type": "alteration", "detail": "耗时 5 秒与原文'
                ' 2 秒不符", "quote_original": "under 2 seconds", '
                '"quote_draft": "less than 5 seconds"}], "clean": false}'
            )
        return LLMResponse(content=content, usage={}, finish_reason="end_turn")

    client = MagicMock()
    client.complete = AsyncMock(side_effect=complete)
    return client


async def main() -> None:
    loop = FactReviewLoop(llm_client=_mock_client() if "--mock" in sys.argv else None)
    firings = await loop.run(
        {"original_text": ORIGINAL, "draft_text": DRAFT}, max_ticks=50)
    print("=== fact_review_loop 输出 ===")
    for k, v in firings[-1].output.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
```

- [ ] **Step 3: 写 demo_writer.py**

```python
# example/demo_writer.py
"""academic_writer 完整流水线真实运行示例（读 sample_raw_text.txt）。

用法（在仓库根目录）：
    python -m example.demo_writer            # 真实 LLM（.env / 环境变量）
    python -m example.demo_writer --mock     # 假 LLM（无需 key，冒烟演示数据流）
"""

from __future__ import annotations

import sys
from pathlib import Path

from llm.client import LLMResponse

from example.academic_writer import run_writer

SAMPLE = Path(__file__).parent / "sample_raw_text.txt"


def _mock_client():
    """免 key 假客户端：按 prompt 独有引导短语返回预设输出（演示数据流形状）。

    分发键不能用"灵感草稿"等用户文本可能出现的词——{original} 占位符会把
    raw_text 渲染进 Review/Finalize 的 prompt。
    """
    from unittest.mock import AsyncMock, MagicMock

    async def complete(prompt: str | None = None, **kwargs) -> LLMResponse:
        p = prompt or ""
        if "整合输出最终版本" in p:
            content = '{"text": "This paper proposes an LLM-based code review system that automatically analyzes pull requests.", "notes": "合并重复表述；将口语化表达改为正式学术句式"}'
        elif "学术英语写作规范" in p:
            content = "We propose an LLM-based system for automated code review of pull requests."
        elif "整理成逻辑通顺的英文文段" in p:
            content = "We propose an LLM-based code review system that automatically reviews pull requests."
        elif "原样转发" in p:
            content = "We propose an LLM-based code review system that automatically reviews pull requests."
        elif "修复者" in p:
            content = "We propose an LLM-based code review system that automatically reviews pull requests."
        else:  # 审阅
            content = '{"issues": [], "clean": true}'
        return LLMResponse(content=content, usage={}, finish_reason="end_turn")

    client = MagicMock()
    client.complete = AsyncMock(side_effect=complete)
    return client


async def main() -> None:
    raw_text = SAMPLE.read_text(encoding="utf-8")
    firings = await run_writer(
        {"raw_text": raw_text},
        llm_client=_mock_client() if "--mock" in sys.argv else None,
        max_ticks=80,
    )
    out = firings[-1].output
    print("=== final_text ===")
    print(out["final_text"])
    print()
    print("=== modification_notes ===")
    print(out["modification_notes"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
```

- [ ] **Step 4: 冒烟运行（--mock，无 key）**

Run: `python -m example.demo_loop --mock && python -m example.demo_writer --mock`
Expected: 两个 demo 均打印输出且退出码 0；demo_loop 输出含 `clean: False` 与 `attempt: 3`（mock 审阅恒报 issues → 达上限退出，演示遗留问题通道）

- [ ] **Step 5: 提交**

```bash
git add example/sample_raw_text.txt example/demo_loop.py example/demo_writer.py
git commit -m "feat: example demo 入口（真实 LLM + --mock 免 key 冒烟）+ 示例灵感草稿"
```

---

## Task 4: example/README.md

**Files:**
- Create: `example/README.md`

- [ ] **Step 1: 写 README**（`example/README.md`）

````markdown
# example — 实践线模块

两个可运行的示例模块：**fact_review_loop**（SubModule，通用事实审阅循环——
可复用/可打包的处理单元）与 **academic_writer**（普通 Module 过程式组装——
顶层完整工作流，消费 fact_review_loop 的 submodule 节点）。

## academic_writer：灵感写作 → 学术英语

将中英混杂、混乱重复的灵感式写作文段，逐步整合优化为符合学术英语写作要求的文段。

```
[A]Organize --> Loop1 --> Polish --> Loop2 --> Finalize --> Report
```

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
    })
    out = firings[-1].output
    print(out["final_text"])            # 最终文段
    print(out["modification_notes"])    # 修改说明（markdown）

asyncio.run(main())
```

或直接跑 demo：

```bash
python -m example.demo_writer            # 真实 LLM（配置 .env / 环境变量）
python -m example.demo_writer --mock     # 免 key 冒烟
```

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

# 打包发布（guards/ 与 submodules/ 随包导出，加载无运行时依赖）
FactReviewLoop().pack("dist/fact_review_loop")

# 在自己的 SubModule 中声明引用 + tasklist 直接写 submodule 节点
class PaperOptimizer(SubModule):
    modules = {"fact_review_loop": FactReviewLoop}
    tasklist = Tasklist(tasks={
        "Loop": TaskDefinition(
            type="submodule", submodule="fact_review_loop",
            inputs={"original_text": "{spec.paper}", "draft_text": "..."},
        ),
    }, flow="Loop")
```

节点级 LLM 设置（model / temperature / think / api_params）可写在 tasklist 的
submodule 节点上，传播到子模块内部全部 harness。

## 设计说明

- 形态分工：仅 fact_review_loop 是 SubModule（可复用/可打包的处理单元）；
  academic_writer 是普通 Module（顶层工作流），过程式组装
  `Module(spec, academic_tasklist, modules={"fact_review_loop": FactReviewLoop})`
- 子模块嵌入模式运行（不进审计/快照/回滚，零落盘），只暴露终点输出；
  内部修复明细不进 notes——聚合字段（attempt / issues_remaining）覆盖审阅所需
- 轮次上限 3 为 guard 内联常量（guard 读不到 spec；pack 单文件导出约束）
- 达上限仍有问题时 clean 边强制退出，遗留 issues 逐条收进
  `issues_remaining` / 修改说明，不静默丢弃

## 测试

```bash
python -m pytest example/ -q        # mock 测试（无需 API key）
```

设计文档：`docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md`
````

- [ ] **Step 2: 提交**

```bash
git add example/README.md
git commit -m "docs: example README — 两级用户使用说明（使用场景 spec 运行 / 开发场景复用打包）"
```

---

## Task 5: 文档状态更新 + 全量回归

**Files:**
- Modify: `docs/dev/progress/module-roadmap.md`（「本次 example 计划」段落，约 194-199 行）
- Modify: `docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md`（状态行，第 4 行）

- [ ] **Step 1: roadmap 更新**（`docs/dev/progress/module-roadmap.md`，把「本次 example 计划（记于此处）」两条 bullet 追加完成标注）

在「**本次 example 计划**（记于此处）：」段落后追加：

```markdown
**✅ 已完成（2026-08-10，`example/` 落地）**：`example/fact_review_loop.py`
（FactReviewLoop + 自包含 guards，pack 含 guards/ 导出，roundtrip 测试覆盖）、
`example/academic_writer.py`（普通 Module 过程式组装，Loop1/Loop2 为 submodule 节点引用
fact_review_loop，输出 final_text + modification_notes）、demo 入口
（`--mock` 免 key 冒烟）、示例草稿、两级用户 README、mock 测试（`pytest example/ -q`）。
框架缺口修复已随 436dbcc 前的系列提交落地。设计见
`docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md`（状态：已实现）。
```

- [ ] **Step 2: 设计文档状态更新**（`docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md`）

第 3-4 行：

```markdown
> 日期：2026-08-10 | 状态：已确认，待实现
```

改为：

```markdown
> 日期：2026-08-10 | 状态：已实现（example/，mock 测试 + demo 入口；实现计划见
> docs/dev/superpowers/plans/2026-08-10-academic-writer-example.md）
```

- [ ] **Step 3: 全量回归**

Run: `python -m pytest example/ -q`
Expected: 11 passed

Run: `python -m pytest module_harness/tests/ -q`（约 5 分钟）
Expected: 342 passed, 2 xfailed（与基线一致，零回归）

- [ ] **Step 4: 提交**

```bash
git add docs/dev/progress/module-roadmap.md docs/dev/superpowers/specs/2026-08-10-academic-writer-design.md
git commit -m "docs: roadmap / academic-writer 设计标记 example 落地完成（fact_review_loop + academic_writer）"
```

---

## 验收清单（全部完成后）

- [ ] `python -m pytest example/ -q` 全绿（11 项：loop 7 + writer 4）
- [ ] `python -m pytest module_harness/tests/ -q` 与基线一致（342 passed, 2 xfailed）
- [ ] `python -m example.demo_loop --mock` / `python -m example.demo_writer --mock` 退出码 0 且输出完整
- [ ] fact_review_loop 可 pack → load roundtrip（guards 导出、运行一致）
- [ ] academic_writer 为普通 Module 过程式组装（模块级 academic_tasklist + run_writer()，无 SubModule 类定义）；仅 fact_review_loop 为 SubModule
- [ ] 同一 fact_review harness 经 Loop1/Loop2 两个 submodule 节点各触发一次（测试断言 review prompt 计数 = 2）
- [ ] 达上限路径不静默丢弃：遗留 issues 进 issues_remaining 与 modification_notes
