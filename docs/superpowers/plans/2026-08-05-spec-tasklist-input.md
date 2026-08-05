# spec + 自定义 tasklist 输入通道 与 一致性审核 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `Module` 增加 `spec + tasklist`（完全自定义）输入通道，跳过翻译；tasklist 经结构校验后由独立审核 harness（`spec_tasklist_review`）做 LLM 一致性审核，不通过抛 `ConsistencyError` 阻塞执行。

**Architecture:** 新增 `module_harness/consistency.py`（`ConsistencyReport` / `ConsistencyError` / `REVIEW_HARNESS_CONFIG` / `register_review_harness` / `ConsistencyReviewer`），审核直接调 harness body（不走 tickflow，与 `Translator` 同模式）。`Module.__init__` 中 `template_name` 与 `tasklist` 互斥，`_build_runner_async` 按通道分支。`events.py` 新增 `ConsistencyReviewed` 事件。

**Tech Stack:** Python 3.13, pytest, asyncio, unittest.mock（`MagicMock`/`AsyncMock`）。

**设计文档:** `docs/superpowers/specs/2026-08-05-spec-tasklist-input-design.md`

**基线:** `python -m pytest module_harness/tests/ -q -m "not smoke"` → 125 passed（smoke 为真实 LLM 测试，不参与）。

---

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `module_harness/consistency.py` | 新建 | 审核数据模型、内置审核配置、审核器 |
| `module_harness/events.py` | 修改 | 新增 `ConsistencyReviewed` 事件 |
| `module_harness/module.py` | 修改 | `tasklist` 通道分支 + `review_harness` 参数 |
| `module_harness/__init__.py` | 修改 | 导出新符号 |
| `module_harness/tests/test_consistency.py` | 新建 | 审核层单元测试 |
| `module_harness/tests/test_module.py` | 修改 | tasklist 通道测试 + fixture 注册审核 harness |
| `docs/progress/module-roadmap.md` | 修改 | #1 #4 标记完成 |

`translator.py` / `graph_builder.py` / `tickflow` / `llm` **零修改**。

---

### Task 1: consistency.py — 审核数据模型 + 内置配置 + ConsistencyReviewer

**Files:**
- Create: `module_harness/consistency.py`
- Test: `module_harness/tests/test_consistency.py`

- [ ] **Step 1: 写失败测试** — 创建 `module_harness/tests/test_consistency.py`：

```python
# module_harness/tests/test_consistency.py
"""一致性审核：ConsistencyReport / REVIEW_HARNESS_CONFIG / ConsistencyReviewer。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMError, LLMResponse
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.consistency import (
    ConsistencyError,
    ConsistencyReport,
    ConsistencyReviewer,
    REVIEW_HARNESS_CONFIG,
    register_review_harness,
)
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _spec() -> Spec:
    return Spec({"source_text": "Hello", "target": "中文"})


def _tasklist() -> Tasklist:
    return Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate", inputs={"text": "source_text"},
            ),
        },
        flow="[A]",
    )


class TestConsistencyModels:
    def test_report_fields(self):
        report = ConsistencyReport(consistent=False, suggestions="缺少覆盖", raw="{}")
        assert report.consistent is False
        assert report.suggestions == "缺少覆盖"

    def test_error_carries_report(self):
        report = ConsistencyReport(consistent=False, suggestions="flow 有死路", raw="{}")
        err = ConsistencyError(report)
        assert err.report is report
        assert "flow 有死路" in str(err)


class TestRegisterReviewHarness:
    def test_registers_builtin(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_review_harness(reg)
        assert reg.is_harness("spec_tasklist_review")
        cfg = reg.harness_config("spec_tasklist_review")
        assert cfg.output_format is not None
        assert cfg.output_format.type == "json_object"

    def test_custom_name(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_review_harness(reg, name="my_review")
        assert reg.is_harness("my_review")
        assert not reg.is_harness("spec_tasklist_review")


class TestConsistencyReviewer:
    @pytest.fixture
    def reg(self, mock_llm):
        r = HarnessRegistry(llm_client=mock_llm)
        register_review_harness(r)
        r.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
        return r

    @pytest.mark.asyncio
    async def test_review_pass(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        report = await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        assert report.consistent is True
        assert report.suggestions == ""

    @pytest.mark.asyncio
    async def test_review_fail_returns_report(self, mock_llm, reg):
        """consistent=false 是合法审核结果：reviewer 返回 report，由 Module 决定阻塞。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": false, "suggestions": "缺少目标覆盖"}',
            usage={}, finish_reason="end_turn",
        )
        report = await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        assert report.consistent is False
        assert "缺少目标覆盖" in report.suggestions

    @pytest.mark.asyncio
    async def test_review_non_json_raises(self, mock_llm, reg):
        """内置审核 harness 带 json_object 约束：非 JSON 在 OutputValidator 层转 Failure。"""
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all", usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="审核 harness 返回 Failure"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_str_path_non_json_raises(self, mock_llm):
        """text 输出格式的审核 harness：str 结果走 json.loads 路径。"""
        reg = HarnessRegistry(llm_client=mock_llm)
        reg.harness("review_text", HarnessConfig(
            prompt_core="审核：{spec} / {tasklist}",
            output_format=OutputFormat(type="text"),
        ))
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all", usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="不是合法 JSON"):
            await ConsistencyReviewer(
                reg, harness_name="review_text"
            ).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_missing_suggestions_raises(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true}', usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="suggestions"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_wrong_consistent_type_raises(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": "yes", "suggestions": "x"}',
            usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="consistent"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_harness_not_registered(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        with pytest.raises(ValueError, match="spec_tasklist_review"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_llm_error_blocks(self, mock_llm, reg):
        mock_llm.complete.side_effect = LLMError("API 不可用")
        with pytest.raises(ValueError):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_prompt_injects_spec_and_tasklist(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert "Hello" in prompt          # spec 数据注入
        assert "source_text" in prompt    # tasklist 字段注入
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_consistency.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'module_harness.consistency'`

- [ ] **Step 3: 实现 `module_harness/consistency.py`**（完整文件）：

```python
# module_harness/consistency.py
"""一致性审核 — spec + tasklist 语义一致性检查。

独立于翻译通道：审核不经过模板，直接调用注册的审核 harness body（不走 tickflow）。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from tickflow import Failure
from tickflow.views import DictView, Resolved

from .config import HarnessConfig
from .outputfmt import OutputFormat
from .registry import HarnessRegistry
from .spec import Spec, Tasklist


@dataclass
class ConsistencyReport:
    """一致性审核结果。"""

    consistent: bool
    suggestions: str
    raw: str  # LLM 原始输出（审计链）


class ConsistencyError(ValueError):
    """一致性审核未通过。携带完整 report，str() 输出问题描述。"""

    def __init__(self, report: ConsistencyReport) -> None:
        self.report = report
        super().__init__(f"一致性审核未通过: {report.suggestions}")


REVIEW_HARNESS_CONFIG = HarnessConfig(
    prompt_core=(
        "你是一致性审核器。判断给定 tasklist 是否能实现 spec 的目标。\n"
        "审核要点：\n"
        "1. spec 的每个目标/需求是否被 tasklist 中的任务覆盖\n"
        "2. task 中引用的字段（{spec.xxx}、inputs）在 spec 中是否存在\n"
        "3. flow 是否可达、是否有死路或未定义节点\n"
        "spec: {spec}\n"
        "tasklist: {tasklist}\n"
        '输出 JSON：{"consistent": true/false, "suggestions": "..."}'
    ),
    output_format=OutputFormat(type="json_object"),
    temperature=0.1,
)


def register_review_harness(
    reg: HarnessRegistry, name: str = "spec_tasklist_review"
) -> None:
    """注册内置一致性审核 harness（默认名 spec_tasklist_review）。"""
    reg.harness(name, REVIEW_HARNESS_CONFIG)


class ConsistencyReviewer:
    """调用审核 harness body，返回 ConsistencyReport。"""

    def __init__(
        self, registry: HarnessRegistry, harness_name: str = "spec_tasklist_review"
    ) -> None:
        self.reg = registry
        self.harness_name = harness_name

    async def review(self, spec: Spec, tasklist: Tasklist) -> ConsistencyReport:
        """执行一致性审核。审核失败（LLM 错误/输出不合法）抛 ValueError。"""
        if self.reg.harness_config(self.harness_name) is None:
            raise ValueError(
                f"审核 harness '{self.harness_name}' 未注册。"
                f"请先调用 register_review_harness(reg) 注册内置审核器，"
                f"或自行 reg.harness('{self.harness_name}', ...)。"
            )
        body = self.reg.get_body(self.harness_name)

        tasklist_dict = {
            "Tasks": {
                key: dataclasses.asdict(task)
                for key, task in tasklist.tasks.items()
            },
            "Flow": tasklist.flow,
        }
        view = DictView(
            {
                "spec": Resolved(value=spec.to_dict(), k=None),
                "tasklist": Resolved(
                    value=json.dumps(tasklist_dict, ensure_ascii=False), k=None
                ),
            },
            node="__review__",
        )
        result = await body(view)

        if isinstance(result, Failure):
            raise ValueError(f"审核 harness 返回 Failure: {result.error}")

        if isinstance(result, str):
            try:
                data = json.loads(result)
            except json.JSONDecodeError as e:
                raise ValueError(f"审核输出不是合法 JSON: {e}") from e
        elif isinstance(result, dict):
            data = result
        else:
            raise ValueError(f"审核输出类型异常: {type(result).__name__}")

        consistent = data.get("consistent")
        suggestions = data.get("suggestions")  # 缺字段 → None → 下方 isinstance 校验抛错
        if not isinstance(consistent, bool):
            raise ValueError(f"审核输出缺少合法的 'consistent' 布尔字段: {data!r}")
        if not isinstance(suggestions, str):
            raise ValueError(f"审核输出 'suggestions' 必须是字符串: {data!r}")

        raw = result if isinstance(result, str) else json.dumps(data, ensure_ascii=False)
        return ConsistencyReport(
            consistent=consistent, suggestions=suggestions, raw=raw
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_consistency.py -q`
Expected: PASS — 13 passed

- [ ] **Step 5: Commit**

```bash
git add module_harness/consistency.py module_harness/tests/test_consistency.py
git commit -m "feat: add consistency review layer (report/error/reviewer/builtin harness)"
```

---

### Task 2: ConsistencyReviewed 事件 + 公共导出

**Files:**
- Modify: `module_harness/events.py`（`HarnessFailed` 之后插入）
- Modify: `module_harness/__init__.py`
- Test: `module_harness/tests/test_consistency.py`（追加）

- [ ] **Step 1: 追加失败测试** — 在 `test_consistency.py` 末尾追加：

```python
class TestConsistencyReviewedEvent:
    def test_event_emit_and_subscribe(self):
        from module_harness.events import ConsistencyReviewed, EventBus
        bus = EventBus()
        seen = []
        bus.subscribe(ConsistencyReviewed, lambda e: seen.append(e))
        bus.emit(ConsistencyReviewed(
            timestamp=1.0, node="__review__", tick=0,
            consistent=False, suggestions="x", raw="{}",
        ))
        assert len(seen) == 1
        assert seen[0].consistent is False
        assert seen[0].node == "__review__"

    def test_public_exports(self):
        import module_harness
        assert module_harness.ConsistencyReviewed is not None
        assert module_harness.ConsistencyError is not None
        assert module_harness.ConsistencyReport is not None
        assert module_harness.ConsistencyReviewer is not None
        assert module_harness.register_review_harness is not None
        assert module_harness.REVIEW_HARNESS_CONFIG is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_consistency.py -q`
Expected: FAIL — `ImportError: cannot import name 'ConsistencyReviewed'`

- [ ] **Step 3: 实现** — `module_harness/events.py` 在 `HarnessFailed` 类之后插入：

```python
@dataclass
class ConsistencyReviewed(HarnessEvent):
    """一致性审核事件（spec + 自定义 tasklist 通道）。"""
    consistent: bool
    suggestions: str
    raw: str
```

`module_harness/__init__.py` 三处修改：

```python
# 1. events 导入块追加（HarnessFailed 之后）：
    HarnessFailed,
    ConsistencyReviewed,

# 2. 新增导入块（.translator 导入之前）：
from .consistency import (
    ConsistencyError,
    ConsistencyReport,
    ConsistencyReviewer,
    REVIEW_HARNESS_CONFIG,
    register_review_harness,
)

# 3. __all__ 追加：
    "ConsistencyReviewed",
    "ConsistencyError",
    "ConsistencyReport",
    "ConsistencyReviewer",
    "REVIEW_HARNESS_CONFIG",
    "register_review_harness",
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_consistency.py -q`
Expected: PASS — 15 passed

- [ ] **Step 5: Commit**

```bash
git add module_harness/events.py module_harness/__init__.py module_harness/tests/test_consistency.py
git commit -m "feat: add ConsistencyReviewed event + public exports"
```

---

### Task 3: Module tasklist 通道

**Files:**
- Modify: `module_harness/module.py`
- Modify: `module_harness/tests/test_module.py`

- [ ] **Step 1: 追加失败测试** — `test_module.py` 顶部导入追加：

```python
from module_harness.consistency import ConsistencyError, register_review_harness
from module_harness.events import ConsistencyReviewed
from module_harness.spec import TaskDefinition, Tasklist
```

`setup_registry` fixture 末尾（`return reg, bus, loader` 之前）追加：

```python
    # 审核 harness
    register_review_harness(reg)
```

文件末尾追加：

```python
class TestModuleTasklistChannel:
    def _tasklist(self):
        return Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "source_text"},
                ),
                "B": TaskDefinition(
                    type="script", script="format_output", inputs={"data": "A"},
                ),
            },
            flow="[A] --> B",
        )

    def test_build_runner_with_tasklist(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_mod",
            registry=reg,
        )
        runner = mod.build_runner()
        assert runner is not None
        assert mod.review_result is not None
        assert mod.review_result.consistent is True

    @pytest.mark.asyncio
    async def test_run_with_tasklist(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        call_count = [0]

        async def fake_complete(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次调用 = 审核
                return LLMResponse(
                    content='{"consistent": true, "suggestions": ""}',
                    usage={}, finish_reason="end_turn",
                )
            # 后续 = 执行 harness
            return LLMResponse(
                content='{"translation": "你好"}',
                usage={}, finish_reason="end_turn",
            )

        mock_llm.complete = AsyncMock(side_effect=fake_complete)
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_run",
            registry=reg,
        )
        firings = await mod.run(max_ticks=10)
        assert len(firings) >= 2
        assert any(f.node == "B" for f in firings)

    def test_tasklist_inconsistent_raises(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": false, "suggestions": "flow 无法到达终点"}',
            usage={}, finish_reason="end_turn",
        )
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_incon",
            registry=reg,
        )
        with pytest.raises(ConsistencyError) as ei:
            mod.build_runner()
        assert mod.review_result is not None
        assert "flow" in ei.value.report.suggestions

    def test_review_harness_none_skips_review(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好"}',
            usage={}, finish_reason="end_turn",
        )
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_norev",
            registry=reg,
            review_harness=None,
        )
        runner = mod.build_runner()
        assert runner is not None
        assert mod.review_result is None

    def test_review_event_emitted(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        seen = []
        bus.subscribe(ConsistencyReviewed, lambda e: seen.append(e))
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_evt",
            registry=reg,
        )
        mod.build_runner()
        assert len(seen) == 1
        assert seen[0].consistent is True

    def test_template_and_tasklist_mutually_exclusive(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        with pytest.raises(ValueError, match="只能传一个"):
            Module(
                spec={},
                template_name="translate",
                tasklist=self._tasklist(),
                llm_client=mock_llm,
                template_loader=loader,
            )
        with pytest.raises(ValueError, match="只能传一个"):
            Module(spec={}, llm_client=mock_llm)

    def test_tasklist_unknown_harness_rejected(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        bad = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="nope")},
            flow="[A]",
        )
        mod = Module(
            spec={},
            tasklist=bad,
            llm_client=mock_llm,
            registry=reg,
        )
        with pytest.raises(ValueError, match="校验失败"):
            mod.build_runner()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest module_harness/tests/test_module.py -q`
Expected: FAIL — `TypeError: Module.__init__() got an unexpected keyword argument 'tasklist'`（或 review 相关失败）

- [ ] **Step 3: 实现 `module_harness/module.py`**（整文件替换）：

```python
"""Module 编排器 — spec + template/tasklist → tasklist → runner。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from tickflow.async_runner import AsyncRunner

from .spec import Spec, Tasklist
from .consistency import ConsistencyError, ConsistencyReport, ConsistencyReviewer
from .translator import Translator, TemplateLoader, TasklistValidator
from .graph_builder import TasklistTranslator
from .registry import HarnessRegistry
from .events import EventBus, ConsistencyReviewed


class Module:
    """SpecModule 的核心编排器。

    spec + template → 翻译 → tasklist → tickflow Graph + registry → AsyncRunner
    或 spec + tasklist（自定义）→ 校验 + 一致性审核 → AsyncRunner。
    """

    def __init__(
        self,
        spec: dict[str, Any],
        *,
        template_name: str | None = None,
        tasklist: Tasklist | None = None,
        llm_client: Any,
        event_bus: EventBus | None = None,
        template_loader: TemplateLoader | None = None,
        module_id: str | None = None,
        registry: HarnessRegistry | None = None,
        review_harness: str | None = "spec_tasklist_review",
    ) -> None:
        if (template_name is None) == (tasklist is None):
            raise ValueError("template_name 与 tasklist 必须且只能传一个")
        self.spec = Spec(spec)
        self.template_name = template_name
        self.tasklist = tasklist
        self.review_harness = review_harness
        self.review_result: ConsistencyReport | None = None
        self.module_id = module_id or f"mod_{uuid.uuid4().hex[:8]}"

        if registry is not None:
            self._reg = registry
        else:
            self._reg = HarnessRegistry(
                llm_client=llm_client,
                event_bus=event_bus or EventBus.null(),
            )
        self._loader = template_loader or TemplateLoader()
        self._translator = Translator(self._reg)

    def build_runner(self) -> AsyncRunner:
        """执行翻译 → 构建 graph → 返回 AsyncRunner。

        Note: 这是一个同步方法。在 async 上下文中直接使用
        await module._build_runner_async() 或 await module.run()。
        """
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._build_runner_async())
        finally:
            loop.close()

    async def _build_runner_async(self) -> AsyncRunner:
        """异步版 build_runner。"""
        if self.tasklist is not None:
            tasklist = self.tasklist
            errors = TasklistValidator.validate(tasklist, self._reg)
            if errors:
                raise ValueError(
                    "tasklist 校验失败:\n" + "\n".join(f"  - {e}" for e in errors)
                )
            if self.review_harness is not None:
                report = await ConsistencyReviewer(
                    self._reg, self.review_harness
                ).review(self.spec, tasklist)
                self.review_result = report
                self._reg._event_bus.emit(ConsistencyReviewed(
                    timestamp=time.monotonic(), node="__review__", tick=0,
                    consistent=report.consistent,
                    suggestions=report.suggestions,
                    raw=report.raw,
                ))
                if not report.consistent:
                    raise ConsistencyError(report)
        else:
            template = self._loader.get(self.template_name)
            if template is None:
                raise ValueError(f"模板 '{self.template_name}' 未找到")
            tasklist = await self._translator.translate(self.spec, template)
        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist, spec=self.spec)
        return AsyncRunner(graph, registry=reg, keep_records=True)

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。"""
        runner = await self._build_runner_async()
        return await runner.run_until_idle(max_ticks=max_ticks)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest module_harness/tests/test_module.py module_harness/tests/test_consistency.py -q`
Expected: PASS — 原有 Module 测试 + 新增 7 个 tasklist 通道测试全绿

- [ ] **Step 5: 全量回归**

Run: `python -m pytest module_harness/tests/ -q -m "not smoke"`
Expected: PASS — 149 passed（125 基线 + 15 Task1 含修复 + 2 Task2 + 7 Task3 新增），7 deselected

- [ ] **Step 6: Commit**

```bash
git add module_harness/module.py module_harness/tests/test_module.py
git commit -m "feat: add spec+tasklist input channel with consistency review to Module"
```

---

### Task 4: Roadmap 更新 + 收尾

**Files:**
- Modify: `docs/progress/module-roadmap.md`

- [ ] **Step 1: 更新 roadmap** — `docs/progress/module-roadmap.md` 三处修改：

1. 头部（第 3 行 `> 最后更新`）与速览（第 11 行）：

```markdown
> 最后更新：2026-08-05
```
```markdown
已实现：**14** / 待实现：**5**
```

2. 「已实现 ✅」表格「配置与翻译」小节末尾追加两行：

```markdown
| **spec + 自定义 tasklist 输入** — tasklist 参数直入 graph builder，跳过翻译（与 template_name 互斥） | `Module` | `module.py` |
| **一致性审核** — 独立审核 harness `spec_tasklist_review`，spec+tasklist 语义一致性 LLM 审核，不通过抛 `ConsistencyError` 阻塞 | `ConsistencyReviewer` + `register_review_harness` | `consistency.py`, `events.py` |
```

3. 删除「待实现 🔲」中的 #1（48-59 行）与 #4（87-97 行）两个小节（含其 `###` 标题、说明、实现方向、依赖），并将「实现顺序建议」中第 1、4 行注释更新：

```markdown
│ 1. spec+tasklist 输入   │  ✅ 已完成（含一致性审核 #4）
│ 4. 一致性审核           │  ✅ 随 #1 完成
```

- [ ] **Step 2: 全量回归**

Run: `python -m pytest module_harness/tests/ -q -m "not smoke"`
Expected: PASS — 149 passed

- [ ] **Step 3: Commit**

```bash
git add docs/progress/module-roadmap.md
git commit -m "docs: mark roadmap #1 spec+tasklist input & #4 consistency review done"
```

---

## 自审记录

- **Spec 覆盖**：输入通道（Task 3）、审核 harness 与 Reviewer（Task 1）、事件与导出（Task 2）、错误处理表全部场景（Task 1/3 测试）、roadmap 更新（Task 4）✅
- **占位符**：所有步骤含完整代码与命令 ✅
- **类型一致性**：`ConsistencyReport(consistent, suggestions, raw)` / `ConsistencyReviewer(registry, harness_name)` / `Module(..., tasklist, review_harness)` 在测试与实现中签名一致 ✅
