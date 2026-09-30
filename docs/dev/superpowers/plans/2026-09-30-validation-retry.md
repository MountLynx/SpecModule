# 校验失败带反馈重试（validate_retries）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `HarnessConfig` 加 `validate_retries` 字段——输出校验失败后带校验错误反馈重问（instructor 模式），预算内循环，耗尽仍返回最后一次 `Failure(type="llm")` 不中止 run；缺省 0 行为与现状逐字节一致。

**Architecture:** 重试加在 `Harness.build_body` 的校验段（节点路径与 `call_harness` 路径汇聚于同一执行配方，两条路同时受益）。每次尝试发完整事件链；节点 state 另记 `_validation_attempts` / `_validation_retry_errors`；`_usage` 累计、`_llm_raw` 取最后一次。task 级经 `graph_builder._register_harness` 的 override 链覆盖。LLMError（传输层）不重试、不消耗预算（SDK `max_retries` 已管，fail-fast → infrastructure ABORT 语义不变）。

**Tech Stack:** Python 3.13，`@dataclass` 配置模型，pytest + `unittest.mock`（`AsyncMock`），tickflow `Failure(type="llm"|"infrastructure")` 语义。测试命令：`python -m pytest module_harness/tests/ -q`（无 pytest 配置，直接 pytest）。

**规格来源:** `docs/dev/superpowers/specs/2026-09-30-validation-retry-design.md`（设计已确认，本文是其任务化）。

**上游核对记录（2026-10-01，对照 `C:\Users\xingy\Desktop\开发\Graph` @ 35b1dbb，已装 tickflow-py 0.3.0）：**
0.2.0 → 0.3.0 库面变化仅姊妹 spec 的 FAILED 终态（`runner.py`/`async_runner.py` 空 tick 有 pending 判 FAILED，不再误报 done）+ `views.py` 一处 docstring；**NodeView 构造签名、Failure(type=...) 语义、Registry/Bind/InputPolicy 均未动**。对本 plan 的影响：
- Task 1–5 实现与测试**零改动**（测试直调 body 或仅建图不跑引擎，不触及 runner 终态；views.py 仅 docstring 变化）。
- 运行级语义注释：0.3.0 下校验失败饿死下游会以 run **FAILED 终态**浮出（旧版误报 done）——这强化了本特性动机，但不改变任何任务代码；"Failure(type="llm") 下游跳过、不中止" 的节点级语义不变。
- 基线已验证：全量 `python -m pytest module_harness/tests/ -q` 在 0.3.0 下 **744 passed**（执行者以此为准，无需重装依赖）。
- 执行分支决定：本仓库为 trunk-based（历史全部直提 main），plan 各任务的 commit 步骤即提在 main——不开 worktree。

**对 spec 的两处落地勘误**（实现按真实文档结构走）：
- spec §6 说同步 `docs/guides/config-guide.md` 的 "HarnessConfig / tasklist task 键清单"——该文件实际**没有**字段清单（它只讲 .env/config.json/rules.txt/LLMConfig），字段表真实居所在 `docs/references/spec-harness-syntax.md`（TaskDefinition 字段表 + HarnessConfig 章节）。本计划改后者 + config-guide 的 "harness 覆盖" 一行。
- spec §6 的 `docs/api.md` 实际是 `docs/references/api.md`，且它**不列** HarnessConfig 字段契约（按消费通道组织）——spec 自己的措辞是"如列则同步"，故不改。

**明确的非目标**（执行者不要"顺手"做）：
- 不重试 `LLMError`；不中止 run（耗尽维持 `Failure(type="llm")`）。
- 不做降温/换模型重试；image 模式不进重试循环（配置层拒绝）。
- `model/translator.py` 里 `Translator._call_harness_translator` 的 prompt_core 覆盖分支（translator.py:295-304）会丢弃 mode/image 等字段——这是**既有**缺口，本 spec 不修它（同样不补 validate_retries，保持该分支现状）。
- `HarnessCallResult` / `call_harness` **零代码逻辑改动**——usage/raw 经节点 state（`_usage`/`_llm_raw`）自然继承累计/最后一次语义，只改 docstring。

## File Structure

| 文件 | 动作 | 职责 |
|------|------|------|
| `module_harness/core/config.py` | 修改 | `HarnessConfig.validate_retries: int = 0` 字段 + `__post_init__` 校验（负数拒绝、image 互斥）+ `from_task_definition` 读键 |
| `module_harness/model/spec.py` | 修改 | `TaskDefinition.validate_retries: int \| None = None` 覆盖字段 + `from_dict` 读键 |
| `module_harness/model/translator.py` | 修改 | `TasklistValidator._check_task` 校验显式值须 int ≥ 0（bool 不算 int） |
| `module_harness/core/harness.py` | 修改 | `build_body` 文本路径重试循环；`_VALIDATION_FEEDBACK` 常量；`_merge_usage` 累计助手 |
| `module_harness/core/call.py` | 修改 | 仅 docstring：usage 累计 / raw 最后一次 |
| `module_harness/orchestrate/graph_builder.py` | 修改 | `_register_harness` override 链加 `validate_retries` |
| `module_harness/tests/test_config.py` | 修改 | 配置层测试 |
| `module_harness/tests/test_spec.py` | 修改 | TaskDefinition 字段测试 |
| `module_harness/tests/test_validator.py` | 修改 | TasklistValidator 校验测试 |
| `module_harness/tests/test_harness.py` | 修改 | 重试循环行为测试 |
| `module_harness/tests/test_call.py` | 修改 | call 层契约测试 |
| `module_harness/tests/test_graph_builder.py` | 修改 | task 级覆盖传播测试 |
| `docs/references/spec-harness-syntax.md` | 修改 | TaskDefinition 表 + HarnessConfig 章节补字段 |
| `docs/guides/config-guide.md` | 修改 | "harness 覆盖" 行补字段名 |

任务顺序即依赖顺序：Task 1（配置字段）→ Task 2（tasklist 模型+校验）→ Task 3（重试循环，依赖 1）→ Task 4（call 层契约，依赖 3）→ Task 5（task 级覆盖，依赖 1+2）→ Task 6（文档+全量回归）。

---

### Task 1: HarnessConfig.validate_retries 字段

**Files:**
- Modify: `module_harness/core/config.py`
- Test: `module_harness/tests/test_config.py`

- [ ] **Step 1: 写失败测试** — 在 `module_harness/tests/test_config.py` 末尾追加（`TestImageModeConfig` 类之后）：

```python
class TestValidateRetriesConfig:
    """validate_retries：缺省 0、负数拒绝、image 互斥、序列化与 from_task_definition 兼容。"""

    def test_default_zero(self):
        assert HarnessConfig(prompt_core="x").validate_retries == 0

    def test_negative_rejected(self):
        with pytest.raises(ValueError, match="validate_retries"):
            HarnessConfig(prompt_core="x", validate_retries=-1)

    def test_image_mode_rejects_positive(self):
        with pytest.raises(ValueError, match="image"):
            HarnessConfig(prompt_core="x", mode="image", validate_retries=1)

    def test_image_mode_zero_ok(self):
        cfg = HarnessConfig(prompt_core="画:{t}", mode="image", validate_retries=0)
        assert cfg.validate_retries == 0

    def test_roundtrip(self):
        cfg = HarnessConfig(prompt_core="x", validate_retries=2)
        assert HarnessConfig.from_dict(cfg.to_dict()) == cfg

    def test_from_task_definition_reads_field(self):
        cfg = HarnessConfig.from_task_definition(
            {"prompt_core": "x", "validate_retries": 3}
        )
        assert cfg.validate_retries == 3

    def test_from_task_definition_absent_means_zero(self):
        cfg = HarnessConfig.from_task_definition({"prompt_core": "x"})
        assert cfg.validate_retries == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_config.py::TestValidateRetriesConfig -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'validate_retries'`

- [ ] **Step 3: 实现** — `module_harness/core/config.py` 三处改动：

(a) 在 `notdo` 字段之后（`# ── LLM 参数` 注释之前）加字段：

```python
    validate_retries: int = 0
    """输出校验失败时的额外重试次数（带校验错误反馈重问）。

    0（缺省）= 不重试，行为与无此字段时逐字节一致。
    N > 0 = 校验失败后最多再问 N 次，每次 prompt 追加校验错误反馈段；
    预算耗尽返回最后一次的 Failure(type="llm")。
    仅作用于输出校验失败；LLMError（传输层）不重试、不消耗预算。
    """
```

(b) `__post_init__` 末尾（`image_dir` 检查之后）追加两段：

```python
        if self.validate_retries < 0:
            raise ValueError(f"validate_retries 须 >= 0，得到 {self.validate_retries!r}")
        if self.validate_retries > 0 and self.mode == "image":
            raise ValueError("mode='image' 无文本输出格式可校验，validate_retries 须为 0")
```

(c) `from_task_definition`：docstring 键清单加一行（`- image_dir ...` 之后）：

```
        - validate_retries → 校验失败重试预算（int ≥ 0，缺省 0）
```

并在 `return cls(...)` 里 `image_dir=task.get("image_dir", "images"),` 之后加：

```python
            validate_retries=task.get("validate_retries", 0),
```

`to_dict` / `from_dict` 经 `dataclasses.asdict` / `cls(**data)` 自动兼容，零改动。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_config.py -q`
Expected: PASS（全部，含既有类）

- [ ] **Step 5: Commit**

```bash
git add module_harness/core/config.py module_harness/tests/test_config.py
git commit -m "feat(core): HarnessConfig.validate_retries 字段——缺省 0 行为不变，负数与 image 模式构建期拒绝"
```

---

### Task 2: TaskDefinition 字段 + TasklistValidator 校验

**Files:**
- Modify: `module_harness/model/spec.py`（TaskDefinition + from_dict）
- Modify: `module_harness/model/translator.py`（TasklistValidator._check_task）
- Modify: `module_harness/core/config.py`（`__post_init__` 加 isinstance 加固——Task 1 质量审查发现：dataclass 直接构造 / `from_dict` / `from_task_definition` 不经过 tasklist 校验器，bool/float/None 会漏进预算循环）
- Test: `module_harness/tests/test_spec.py`、`module_harness/tests/test_validator.py`、`module_harness/tests/test_config.py`

- [ ] **Step 1: 写失败测试** — `module_harness/tests/test_spec.py` 的 `TestTaskDefinition` 类末尾（`test_from_dict_script` 之后）加：

```python
    def test_validate_retries_default_none(self):
        td = TaskDefinition(type="harness", harness="translate")
        assert td.validate_retries is None

    def test_from_dict_reads_validate_retries(self):
        td = TaskDefinition.from_dict(
            {"type": "harness", "harness": "translate", "validate_retries": 2}
        )
        assert td.validate_retries == 2
```

`module_harness/tests/test_validator.py` 的 `TestTasklistValidator` 类末尾（`test_submodule_declared_in_modules_passes` 之后）加：

```python
    def test_validate_retries_negative_rejected(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="translate",
                                       validate_retries=-1)},
            flow="[A]",
        )
        reg = _make_registry(harnesses={"translate"})
        errors = TasklistValidator.validate(tl, reg)
        assert any("validate_retries" in e for e in errors)

    def test_validate_retries_non_int_rejected(self):
        """非 int（含 bool——框架 bool 与 int 严格区分）显式值报错。"""
        for bad in ("2", True, 1.5):
            tl = Tasklist(
                tasks={"A": TaskDefinition(type="harness", harness="translate",
                                           validate_retries=bad)},
                flow="[A]",
            )
            reg = _make_registry(harnesses={"translate"})
            errors = TasklistValidator.validate(tl, reg)
            assert any("validate_retries" in e for e in errors), f"未拒绝 {bad!r}"

    def test_validate_retries_valid_passes(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="translate",
                                       validate_retries=2)},
            flow="[A]",
        )
        reg = _make_registry(harnesses={"translate"})
        assert TasklistValidator.validate(tl, reg) == []
```

`module_harness/tests/test_config.py` 的 `TestValidateRetriesConfig` 类末尾加（dataclass 层 isinstance 加固——堵住不经过 tasklist 校验器的编程 API 边界；显式 null 同样报错，框架不猜）：

```python
    def test_bool_rejected(self):
        with pytest.raises(ValueError, match="validate_retries"):
            HarnessConfig(prompt_core="x", validate_retries=True)

    def test_float_rejected(self):
        with pytest.raises(ValueError, match="validate_retries"):
            HarnessConfig(prompt_core="x", validate_retries=1.5)

    def test_explicit_none_rejected(self):
        with pytest.raises(ValueError, match="validate_retries"):
            HarnessConfig(prompt_core="x", validate_retries=None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_spec.py::TestTaskDefinition -q module_harness/tests/test_validator.py::TestTasklistValidator -q`
Expected: FAIL — test_spec 两条 `TypeError: __init__() got an unexpected keyword argument 'validate_retries'`；test_validator 前两条同样 TypeError

- [ ] **Step 3: 实现** — 两文件改动：

(a) `module_harness/model/spec.py`：`TaskDefinition` 的 `image_dir` 行之后、`inputs` 行之前加字段：

```python
    validate_retries: int | None = None  # 校验失败重试预算覆盖（None = 沿用注册 config）
```

`from_dict` 里 `image_dir=d.get("image_dir"),` 之后加：

```python
            validate_retries=d.get("validate_retries"),
```

（`Tasklist.to_dict` 走 `dataclasses.asdict`，字段自动带上，零改动。）

(b) `module_harness/model/translator.py`：`TasklistValidator._check_task` 末尾 `return errors` 之前加（对所有 task 类型生效——spec 只要求"显式值须为 int >= 0"，不按 type 收窄）：

```python
        if task.validate_retries is not None:
            if isinstance(task.validate_retries, bool) or not isinstance(task.validate_retries, int):
                errors.append(
                    f"Task '{key}': validate_retries 应为 int，"
                    f"得到 {type(task.validate_retries).__name__}"
                )
            elif task.validate_retries < 0:
                errors.append(
                    f"Task '{key}': validate_retries 须 >= 0，得到 {task.validate_retries}"
                )
```

(c) `module_harness/core/config.py`：`__post_init__` 的负数检查**之前**加 isinstance 加固（先类型后数值——None/str/float 不得落到 `< 0` 比较里炸出 opaque TypeError；bool 显式排除，`True == 1` 不得伪装成预算 1；float 会把 `budget -= 1` 循环拖出 1.5→0.5→-0.5 的三次尝试）：

```python
        if isinstance(self.validate_retries, bool) or not isinstance(self.validate_retries, int):
            raise ValueError(
                f"validate_retries 须为 int，得到 {type(self.validate_retries).__name__}"
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_spec.py module_harness/tests/test_validator.py module_harness/tests/test_config.py -q`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add module_harness/model/spec.py module_harness/model/translator.py module_harness/core/config.py module_harness/tests/test_spec.py module_harness/tests/test_validator.py module_harness/tests/test_config.py
git commit -m "feat(model): TaskDefinition.validate_retries 可选覆盖字段——TasklistValidator 与 dataclass 双层校验 int>=0（bool 不算 int）"
```

---

### Task 3: build_body 校验失败带反馈重试循环

**Files:**
- Modify: `module_harness/core/harness.py`
- Test: `module_harness/tests/test_harness.py`

背景（给零上下文执行者）：`body` 是 `build_body` 返回的 async callable；现状是一次 `llm.complete` → 校验失败即返回 `Failure(type="llm")`（harness.py:176-232）。重试循环包住"调 LLM + 校验"段；`PromptRendered` 首次在循环外发射（harness.py:149-152），重试时在循环内补发带反馈的 prompt 事件。`_run_image`（图像模式）不进循环。

- [ ] **Step 1: 写失败测试** — `module_harness/tests/test_harness.py` 末尾追加（`TestHarnessImageMode` 类之后）。文件顶部已 import `LLMError`/`NodeView`/`Failure`/`EventBus`/`LlmCallStarted` 等，无需新增顶部 import（`LLMResponse` 沿用文件内局部 import 风格）：

```python
class TestValidationRetry:
    """validate_retries：校验失败带反馈重问（预算内循环；LLMError 不消耗预算）。"""

    BAD = "not json at all, completely invalid {{{"

    @staticmethod
    def _cfg(**overrides) -> HarnessConfig:
        kwargs: dict = {
            "prompt_core": "P",
            "output_format": OutputFormat(type="json_object"),
        }
        kwargs.update(overrides)
        return HarnessConfig(**kwargs)

    @staticmethod
    def _resp(content: str, usage: dict | None = None):
        from llm.client import LLMResponse
        return LLMResponse(
            content=content,
            usage=usage if usage is not None else {"input_tokens": 1, "output_tokens": 1},
            finish_reason="end_turn",
        )

    @staticmethod
    def _state_view():
        """挂了 state dict 的视图（模拟引擎供数的 mutable_state 审计链）。"""
        state: dict = {}
        view = NodeView(node="test_node", fields=(), values=(), state=state)
        return view, state

    @pytest.mark.asyncio
    async def test_first_fail_second_pass(self, mock_llm):
        """首败次过：2 次 complete、第二次 prompt 含反馈（带首错文本）、参数原样保留、usage 累计。"""
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD, usage={"input_tokens": 1, "output_tokens": 2}),
            self._resp('{"ok": 1}', usage={"input_tokens": 3, "output_tokens": 4}),
        ])
        h = Harness(self._cfg(validate_retries=1, temperature=0.3), mock_llm, EventBus())
        view, state = self._state_view()
        result = await h.build_body()(view)

        assert result == {"ok": 1}
        assert mock_llm.complete.await_count == 2
        calls = mock_llm.complete.await_args_list
        assert "上一次输出未通过校验" not in calls[0].kwargs["prompt"]
        assert "上一次输出未通过校验" in calls[1].kwargs["prompt"]
        assert "输出格式校验失败" in calls[1].kwargs["prompt"]  # 反馈携带首错 error
        # LLM 参数重试时原样保留（不降温不换模型）
        assert calls[1].kwargs["temperature"] == calls[0].kwargs["temperature"] == 0.3
        # usage 累计、raw 取最后一次
        assert state["_usage"] == {"input_tokens": 4, "output_tokens": 6}
        assert state["_llm_raw"] == '{"ok": 1}'

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_last_failure(self, mock_llm):
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD), self._resp(self.BAD),
        ])
        h = Harness(self._cfg(validate_retries=1), mock_llm, EventBus())
        result = await h.build_body()(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert mock_llm.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_default_zero_no_retry_no_new_state_keys(self, mock_llm):
        """缺省 0：1 次调用即返 Failure；不新增状态键（与无此字段时逐字节一致）。"""
        mock_llm.complete = AsyncMock(side_effect=[self._resp(self.BAD)])
        h = Harness(self._cfg(), mock_llm, EventBus())
        view, state = self._state_view()
        result = await h.build_body()(view)

        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert mock_llm.complete.await_count == 1
        assert "_validation_attempts" not in state
        assert "_validation_retry_errors" not in state

    @pytest.mark.asyncio
    async def test_llm_error_does_not_consume_budget(self, mock_llm):
        """第 1 次坏输出、第 2 次 LLMError：infrastructure Failure，complete 恰 2 次。"""
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD), LLMError("连接耗尽"),
        ])
        h = Harness(self._cfg(validate_retries=2), mock_llm, EventBus())
        result = await h.build_body()(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert mock_llm.complete.await_count == 2  # LLMError 即返，不续问

    @pytest.mark.asyncio
    async def test_events_and_state_on_retry(self, mock_llm):
        """事件序列含两次 LlmCallStarted；state 记 _validation_attempts / _validation_retry_errors。"""
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD), self._resp('{"ok": 1}'),
        ])
        started = []
        bus = EventBus()
        bus.subscribe(LlmCallStarted, lambda e: started.append(e))
        h = Harness(self._cfg(validate_retries=1), mock_llm, bus)
        view, state = self._state_view()
        result = await h.build_body()(view)

        assert result == {"ok": 1}
        assert len(started) == 2
        assert state["_validation_attempts"] == 2
        assert len(state["_validation_retry_errors"]) == 1
        assert "输出格式校验失败" in state["_validation_retry_errors"][0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_harness.py::TestValidationRetry -q`
Expected: FAIL — `test_first_fail_second_pass` 断言 `result == {"ok": 1}` 得到 `Failure`（无重试发生，complete 只被调 1 次）；其余各条同理失败。`test_default_zero_no_retry_no_new_state_keys` 与 `test_llm_error_does_not_consume_budget` 此刻可能已经绿（它们锁定的是现状行为）——这是预期内的"特征测试"，随类一起留作回归锚。

- [ ] **Step 3: 实现** — `module_harness/core/harness.py` 三处改动：

(a) import 块之后（`class Harness` 之前）加框架契约文案常量：

```python
# 校验失败反馈段（框架契约文案）：重试时追加到原渲染 prompt 末尾重问
_VALIDATION_FEEDBACK = (
    "\n\n上一次输出未通过校验：{error}\n"
    "请修正该问题后重新输出完整结果，不要复述错误内容，不要解释修改过程。"
)
```

(b) 文件末尾 `_was_extracted` 旁加用量累计助手：

```python
def _merge_usage(acc: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    """累计多次尝试的 token 用量：数值键求和，单侧缺键取另一侧。"""
    merged = dict(acc)
    for key, val in new.items():
        prev = merged.get(key)
        if isinstance(prev, int) and isinstance(val, int):
            merged[key] = prev + val
        else:
            merged[key] = val
    return merged
```

(c) 替换 `body` 内从 `# 2. 调用 LLM` 起、到 `return response.content` 止的整段（现 harness.py:154-232）为：

```python
            # 2. 调用 LLM（image 单次；text 带校验重试循环）
            if config.mode == "image":
                bus.emit(LlmCallStarted(
                    timestamp=time.monotonic(), node=node, tick=0,
                    model=config.model or "default",
                    prompt_chars=len(rendered),
                ))
                return await _run_image(view, rendered, state)

            budget = config.validate_retries
            retrying = budget > 0
            attempt_prompt = rendered
            usage_acc: dict[str, int] = {}
            attempts = 0
            retry_errors: list[str] = []

            def on_token(chunk: str) -> None:
                bus.emit(LlmToken(
                    timestamp=time.monotonic(), node=node, tick=0,
                    chunk=chunk,
                ))

            def on_thinking(chunk: str) -> None:
                bus.emit(LlmThinking(
                    timestamp=time.monotonic(), node=node, tick=0,
                    chunk=chunk,
                ))

            while True:
                attempts += 1
                bus.emit(LlmCallStarted(
                    timestamp=time.monotonic(), node=node, tick=0,
                    model=config.model or "default",
                    prompt_chars=len(attempt_prompt),
                ))

                try:
                    from llm.client import LLMError

                    # notdo 由 LLM client 内部通过 _build_system() 拼入 system prompt
                    response = await llm.complete(
                        prompt=attempt_prompt,
                        model=config.model,
                        temperature=config.temperature,
                        think=config.think,
                        output_format=dataclasses.asdict(config.output_format) if config.output_format else None,
                        notdo=config.notdo if config.notdo else None,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        api_params=config.api_params if config.api_params else None,
                    )
                except LLMError as e:
                    # 传输层失败不重试（SDK max_retries 已管）、不消耗校验重试预算
                    if state is not None:
                        state["_llm_error"] = str(e)
                    bus.emit(HarnessFailed(
                        timestamp=time.monotonic(), node=node, tick=0,
                        reason=str(e),
                        failure_type="infrastructure",
                    ))
                    return Failure(str(e), type="infrastructure")

                # LLM 原始响应 + usage 写入节点状态（审计链：NodeState.mutable_state）；
                # 重试开启时 usage 累计、_validation_attempts 记实际调用次数
                if state is not None:
                    state["_llm_raw"] = response.content
                    usage_acc = _merge_usage(usage_acc, response.usage)
                    state["_usage"] = dict(usage_acc)
                    if retrying:
                        state["_validation_attempts"] = attempts

                # 3. 校验输出
                bus.emit(LlmCallCompleted(
                    timestamp=time.monotonic(), node=node, tick=0,
                    content_chars=len(response.content),
                    usage=response.usage,
                    finish_reason=response.finish_reason,
                ))

                if validator is None:
                    return response.content

                result = validator.validate(response.content)
                if not isinstance(result, Failure):
                    bus.emit(OutputValidated(
                        timestamp=time.monotonic(), node=node, tick=0,
                        passed=True,
                        extracted=_was_extracted(response.content, result),
                        error=None,
                    ))
                    return result

                bus.emit(OutputValidated(
                    timestamp=time.monotonic(), node=node, tick=0,
                    passed=False,
                    extracted=False,
                    error=result.error,
                ))

                if budget <= 0:
                    return result  # 预算耗尽：最后一次的 Failure(type="llm")

                # 带反馈重问：prompt 追加校验错误反馈段，LLM 参数原样保留
                budget -= 1
                retry_errors.append(result.error)
                if state is not None:
                    state["_validation_retry_errors"] = list(retry_errors)
                attempt_prompt = rendered + _VALIDATION_FEEDBACK.format(error=result.error)
                if state is not None:
                    state["_prompt"] = attempt_prompt
                bus.emit(PromptRendered(
                    timestamp=time.monotonic(), node=node, tick=0,
                    rendered=attempt_prompt,
                ))

        return body
```

语义对照（实现者自查）：
- 缺省 0 路径与旧代码逐字节同构——同序事件（LlmCallStarted → complete → state 写 → LlmCallCompleted → OutputValidated → return）；`dict(usage_acc)` 单次尝试内容等于 `dict(response.usage)`；`_validation_*` 键只在 `retrying` 时写。
- LLMError 在循环内 return，不消耗 budget；image 分支保留原有的循环外单次 LlmCallStarted（`_run_image` 内部发 LlmCallCompleted/ImageSaved，测试 `TestHarnessImageMode::test_events_and_state` 锁定该序列）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_harness.py -q`
Expected: PASS（全部，含既有 `TestHarnessBuildBody` / `TestHarnessImageMode`）

- [ ] **Step 5: Commit**

```bash
git add module_harness/core/harness.py module_harness/tests/test_harness.py
git commit -m "feat(core): build_body 校验失败带反馈重试循环——预算内重问、usage 累计、LLMError 不耗预算、缺省 0 逐字节不变"
```

---

### Task 4: call 层契约锁定（docstring + 测试）

**Files:**
- Modify: `module_harness/core/call.py`（仅 docstring——逻辑零改动，语义经 Task 3 的 state 写入自然继承）
- Test: `module_harness/tests/test_call.py`

- [ ] **Step 1: 写契约测试** — `module_harness/tests/test_call.py` 末尾追加（`TestPublicExports` 类之前或之后均可）：

```python
class TestCallHarnessRetry:
    """validate_retries 在 call 层的呈现：耗尽 → HarnessCallError；usage 累计；raw 取最后一次。"""

    @pytest.mark.asyncio
    async def test_budget_exhausted_error_carries_last_attempt(self, mock_llm):
        mock_llm.complete = AsyncMock(side_effect=[
            LLMResponse(content="bad one", usage={"input_tokens": 2, "output_tokens": 1},
                        finish_reason="end_turn"),
            LLMResponse(content="bad two", usage={"input_tokens": 3, "output_tokens": 4},
                        finish_reason="end_turn"),
        ])
        with pytest.raises(HarnessCallError) as ei:
            await call_harness(
                HarnessConfig(
                    prompt_core="P",
                    output_format=OutputFormat(type="json_object"),
                    validate_retries=1,
                ),
                {},
                llm_client=mock_llm,
            )
        err = ei.value
        assert err.failure.type == "llm"
        assert err.raw == "bad two"          # 最后一次尝试的原始输出
        assert err.usage == {"input_tokens": 5, "output_tokens": 5}  # 各次之和
        assert "上一次输出未通过校验" in err.prompt   # prompt 为最后一次实际发送
        assert mock_llm.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_success_returns_value_with_summed_usage(self, mock_llm):
        mock_llm.complete = AsyncMock(side_effect=[
            LLMResponse(content="bad", usage={"input_tokens": 1}, finish_reason="end_turn"),
            LLMResponse(content='{"ok": 1}', usage={"input_tokens": 2}, finish_reason="end_turn"),
        ])
        result = await call_harness(
            HarnessConfig(
                prompt_core="P",
                output_format=OutputFormat(type="json_object"),
                validate_retries=1,
            ),
            {},
            llm_client=mock_llm,
        )
        assert result.value == {"ok": 1}
        assert result.raw == '{"ok": 1}'
        assert result.usage == {"input_tokens": 3}
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_call.py::TestCallHarnessRetry -q`
Expected: PASS —— 行为已由 Task 3 提供（usage/raw/prompt 读自节点 state），本任务是把契约锁进测试。若意外 FAIL，说明 Task 3 的 state 写入有误，回 Task 3 修，不要在 call.py 加逻辑。

- [ ] **Step 3: 写 docstring** — `module_harness/core/call.py` 两处：

(a) `HarnessCallResult` docstring 改为：

```python
    """独立调用结果：校验后输出 + LLM 原始输出 + token 用量。

    图像模式不产生文本 raw（无 _llm_raw），raw 为 None。
    validate_retries > 0 时：usage 为各次尝试之和；raw 为最后一次尝试的原始输出。
    """
```

(b) `call_harness` docstring 中"失败（LLM 错误 / 输出校验不通过）抛 HarnessCallError，携带 failure 与 渲染 prompt / 原始输出 / usage 诊断链。"一句改写为：

```python
    失败（LLM 错误 / 输出校验不通过）抛 HarnessCallError，携带 failure 与
    渲染 prompt / 原始输出 / usage 诊断链。``validate_retries > 0`` 时校验
    失败在 body 内带反馈重问：``prompt`` 为最后一次尝试的实际 prompt（含
    反馈段）、``raw`` 为最后一次原始输出、``usage`` 为各次尝试之和。task
    层没有"下游跳过"概念，Failure 一律翻译为异常；promptmode 缺 key →
    KeyError 原样冒出。
```

- [ ] **Step 4: 跑全文件确认无回归**

Run: `python -m pytest module_harness/tests/test_call.py -q`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add module_harness/core/call.py module_harness/tests/test_call.py
git commit -m "docs(call): HarnessCallResult 重试语义写明——usage 累计、prompt/raw 为最后一次尝试（契约测试锁定）"
```

---

### Task 5: task 级覆盖 — graph_builder override 链

**Files:**
- Modify: `module_harness/orchestrate/graph_builder.py:188-214`（`_register_harness` 的 HarnessConfig 构造）
- Modify: `module_harness/model/submodule.py:141-155`（`_apply_harness_overrides` 拷贝构造——Task 1 质量审查发现：该链携带 mode/image_size/image_dir 却漏 validate_retries，SubModule `run(harness_overrides=...)` 重建 config 时预算静默归零，与 graph_builder 要修的"漏掉即静默回默认"同类）
- Test: `module_harness/tests/test_graph_builder.py`、`module_harness/tests/test_submodule.py`

- [ ] **Step 1: 写失败测试** — `module_harness/tests/test_graph_builder.py` 末尾追加（`TestImageModePropagation` 之后）：

```python
class TestValidateRetriesPropagation:
    """Task 级 validate_retries 覆盖传播到隔离注册的 HarnessConfig。"""

    def test_task_overrides_budget(self, mock_llm, reg):
        reg.harness("jt", HarnessConfig(prompt_core="P", validate_retries=1))
        tl = Tasklist(
            tasks={"J": TaskDefinition(type="harness", harness="jt", validate_retries=5)},
            flow="[J]",
        )
        _, out_reg = TasklistTranslator(reg, module_id="m1").build(tl)
        assert out_reg.harness_config("m1:J").validate_retries == 5

    def test_task_without_field_keeps_base(self, mock_llm, reg):
        reg.harness("jb", HarnessConfig(prompt_core="P", validate_retries=2))
        tl = Tasklist(
            tasks={"J": TaskDefinition(type="harness", harness="jb")},
            flow="[J]",
        )
        _, out_reg = TasklistTranslator(reg, module_id="m1").build(tl)
        assert out_reg.harness_config("m1:J").validate_retries == 2

    def test_inherited_budget_conflicts_with_image_mode_at_build(self, mock_llm, reg):
        """注册 config 带预算 + task 覆盖 mode="image"：合成 config 构建期 ValueError。"""
        reg.harness("jc", HarnessConfig(prompt_core="P", validate_retries=3))
        tl = Tasklist(
            tasks={"J": TaskDefinition(type="harness", harness="jc", mode="image")},
            flow="[J]",
        )
        with pytest.raises(ValueError, match="image"):
            TasklistTranslator(reg, module_id="m1").build(tl)
```

`module_harness/tests/test_submodule.py` 的 `test_harness_overrides_keep_image_mode` 之后加（同款守护，同文件既有 import 即可）：

```python
    def test_harness_overrides_keep_validate_retries(self):
        """LLM 覆盖重建 config 时不得丢校验重试预算——漏掉会静默归零。"""
        hc = HarnessConfig(name="jt", prompt_core="P", validate_retries=3)
        out = SubModule._apply_harness_overrides(hc, {"model": "m2"})
        assert out.validate_retries == 3
        assert out.model == "m2"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_graph_builder.py::TestValidateRetriesPropagation -q`
Expected: FAIL — `test_task_overrides_budget` 得 1 ≠ 5（override 链还没带 validate_retries）；`test_inherited_budget_conflicts_with_image_mode_at_build` 不抛（合成 config 的 validate_retries 缺省 0，无冲突）

- [ ] **Step 3: 实现** — 两文件改动：

(a) `module_harness/orchestrate/graph_builder.py` 的 `_register_harness` 中 `cfg = HarnessConfig(...)` 构造里，`image_dir=(...)` 项之后加：

```python
            # 校验重试预算：task 级覆盖，缺省沿用注册 config——漏掉会让
            # tasklist 想调的预算静默回注册值
            validate_retries=(
                task.validate_retries
                if task.validate_retries is not None
                else existing.validate_retries
            ),
```

(b) `module_harness/model/submodule.py` 的 `_apply_harness_overrides` 返回的 `HarnessConfig(...)` 构造里，`image_dir=hc.image_dir,` 之后加（与"调用形态三字段原样携带"同类语义——覆盖词汇表不含 validate_retries，只保证基配置预算不被重建丢掉）：

```python
            # 校验重试预算原样携带——漏掉会在覆盖时静默归零
            validate_retries=hc.validate_retries,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_graph_builder.py module_harness/tests/test_submodule.py -q`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add module_harness/orchestrate/graph_builder.py module_harness/model/submodule.py module_harness/tests/test_graph_builder.py module_harness/tests/test_submodule.py
git commit -m "feat(orchestrate): validate_retries 传播链补全——task 级覆盖进 _register_harness，SubModule 覆盖重建不丢预算"
```

---

### Task 6: 文档同步 + 全量回归

**Files:**
- Modify: `docs/references/spec-harness-syntax.md`
- Modify: `docs/guides/config-guide.md`

（`docs/references/api.md` 不列 HarnessConfig 字段契约，按 spec "如列则同步" 的措辞——不改。）

- [ ] **Step 1: spec-harness-syntax.md** — 两处：

(a) "### TaskDefinition 字段" 表格 `image_dir` 行之后加一行：

```markdown
| `validate_retries` | `int \| None` | 输出校验失败带反馈重试次数覆盖；`None`（缺省；显式 null 等同缺省）= 沿用注册 config。显式值须 ≥ 0（TasklistValidator 校验）；`mode="image"` 的 harness 须为 0（合成 config 构建期 `ValueError`） |
```

(b) "## harness 语法（HarnessConfig）" 代码示例 `api_params` 行之后加：

```python
    # 输出校验失败带反馈重试（0 = 不重试；image 模式须为 0）
    validate_retries=1,
```

并在 "### output_format" 小节（其表格与"校验失败时自动尝试修复提取"段之后、"### 注册方式" 之前）插入新小节：

```markdown
### 校验重试（validate_retries）

`HarnessConfig.validate_retries`（缺省 0）：输出校验失败时的额外重试次数——每次重试 prompt 追加校验错误反馈段，LLM 参数（model/temperature/think/notdo/api_params）原样保留。预算耗尽返回最后一次的 `Failure(type="llm")`（下游跳过语义不变，不中止 run）。仅作用于输出校验失败；`LLMError`（传输层）不重试（SDK `max_retries` 已管）、不消耗预算。审计：每次尝试发完整事件链，节点 state 记 `_validation_attempts`（实际调用次数）与 `_validation_retry_errors`（历次校验错误），`_usage` 累计、`_llm_raw` 为最后一次输出；`call_harness` 诊断链同语义。
```

- [ ] **Step 2: config-guide.md** — "### 各消费位置" 表的 harness 覆盖行改为：

```markdown
| **harness 覆盖** | `HarnessConfig(model/temperature/think/api_params/validate_retries)` | 单节点覆盖 LLM 默认参数与校验重试预算；`api_params` 按 SDK 官方格式透传，优先级最高 |
```

- [ ] **Step 3: 全量回归**

Run: `python -m pytest module_harness/tests/ -q`
Expected: 全绿（缺省 0 保证既有消费方零行为变化——academic_writer / ppt_master / 一致性审核 / 翻译通道不受影响）

- [ ] **Step 4: Commit**

```bash
git add docs/references/spec-harness-syntax.md docs/guides/config-guide.md
git commit -m "docs(spec): validate_retries 字段文档同步——TaskDefinition/HarnessConfig 字段表与校验重试小节"
```

---

## 自查记录（Self-Review）

- **Spec 覆盖**：§1.1 字段+校验（Task 1）✓；§1.2 TaskDefinition+validator（Task 2）✓；§2 重试循环+反馈段+参数保留（Task 3）✓；§3 事件链/state/usage 累计/raw 最后一次（Task 3+4）✓；§4 task 级覆盖（Task 5）✓；§5 测试清单逐条对号——首败次过/耗尽/缺省 0/LLMError 不耗预算/image 互斥/双 LlmCallStarted+_validation_attempts（Task 3）、call 耗尽+usage 累计+raw（Task 4）、override 传递+非法值报错（Task 2+5）、全量回归（Task 6）✓；§6 文档（Task 6，按两处落地勘误执行）✓。
- **占位符扫描**：无 TBD/TODO/"适当处理"；所有代码步骤含完整代码。
- **类型一致性**：`_VALIDATION_FEEDBACK` / `_merge_usage` / `_validation_attempts` / `_validation_retry_errors` 各任务引用同名；`validate_retries` 字段名跨 config/spec/graph_builder 一致；`HarnessCallResult` 字段（value/raw/usage）与测试一致。
