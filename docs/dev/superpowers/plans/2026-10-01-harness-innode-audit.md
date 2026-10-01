# call_harness 节点内透传（ppt_master LLM 全链进审计）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `call_harness` 增加 `view=` 节点内形态，ppt_master 六个 LLM 节点的事件归属与 LLM 状态链（`_prompt`/`_llm_raw`/`_usage`/`_llm_calls`）按真实节点进审计（stream.log / NodeState→records / 查询层）。

**Architecture:** 不改图结构与收据语义——`call_harness` 在节点内形态下用真实 `view.node` 发事件、本调用状态隔离（新 dict 承接 body 写入）完成后合并回 `view.state`；llm_nodes 六工厂分单调用（加一个参数）与多调用（`_llm_calls` 累积）两档改造。

**Tech Stack:** Python 3.13 / pytest + unittest.mock / tickflow 0.3.0（NodeView、_NodeStateView）/ 无新依赖。

**Spec:** `docs/dev/superpowers/specs/2026-10-01-harness-innode-audit-design.md`

**测试命令**（仓库根目录）：

```bash
python -m pytest module_harness/tests/test_call.py -q          # Task 1
python -m pytest example/test_ppt_master_nodes.py -q          # Task 2/3
python -m pytest example/test_ppt_master_e2e.py -q            # Task 4
python -m pytest module_harness/tests/ -q                      # 全量回归
python -m pytest example/ -q                                   # 全量回归
```

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `module_harness/core/call.py` | 修改 | `call_harness` 加 `view=` 参数：node 归属 + 本调用状态隔离 + 合并回 |
| `module_harness/tests/test_call.py` | 修改 | 透传形态单测（事件归属/状态落点/失败诊断不串音/无状态回落） |
| `example/ppt_master/llm_nodes.py` | 修改 | 六工厂加 `view=view`；`_record_llm_call` 累积助手（repair/image）；模块 docstring 补审计说明 |
| `example/test_ppt_master_nodes.py` | 修改 | `_View` 加 `state`；页节点状态落点测试；repair 累积测试；image 累积断言 |
| `example/test_ppt_master_e2e.py` | 修改 | 全链 firings 的 `mutable_state` 断言；事件归属端到端测试 |
| `docs/dev/progress/module-roadmap.md` | 修改 | call_harness 条目补节点内形态一笔 |

不新建文件。不改 tickflow、不改图结构/翻译器/门、不改 prompts_config。

---

### Task 1: `call_harness` view 透传（module_harness 层）

**Files:**

- Modify: `module_harness/tests/test_call.py`（文件末尾追加测试类）
- Modify: `module_harness/core/call.py:53-102`（`call_harness` 签名与函数体）

- [ ] **Step 1: 写失败测试** — 在 `module_harness/tests/test_call.py` 末尾追加（文件顶部 import 区补充一行 `NodeView` 导入：`from tickflow.views import NodeView`）：

```python
class TestCallHarnessInView:
    """节点内透传形态：事件与状态归属真实节点；诊断链本调用隔离（不串音）。"""

    @pytest.mark.asyncio
    async def test_events_and_state_land_on_real_node(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="你好", usage={"input_tokens": 2}, finish_reason="end_turn",
        )
        bus = EventBus()
        seen = []
        bus.subscribe(PromptRendered, lambda e: seen.append(e))
        state: dict = {}
        view = NodeView(node="P01", state=state)
        result = await call_harness(
            HarnessConfig(prompt_core="翻译：{text}", output_format=OutputFormat(type="text")),
            {"text": "hello"},
            llm_client=mock_llm,
            event_bus=bus,
            view=view,
        )
        assert result.value == "你好"
        assert seen and all(e.node == "P01" for e in seen)   # 事件归属真实节点
        assert state["_prompt"] == "翻译：hello"             # 合并回真实状态
        assert state["_llm_raw"] == "你好"
        assert state["_usage"] == {"input_tokens": 2}

    @pytest.mark.asyncio
    async def test_view_without_state_falls_back_to_local(self, mock_llm):
        """view.state None（引擎外合成视图）→ 回落局部 dict，不炸不外泄。"""
        mock_llm.complete.return_value = LLMResponse(
            content="hi", usage={}, finish_reason="end_turn",
        )
        view = NodeView(node="P01", state=None)
        result = await call_harness(
            HarnessConfig(prompt_core="P：{x}"),
            {"x": "1"},
            llm_client=mock_llm,
            view=view,
        )
        assert result.value == "hi"
        assert result.raw == "hi"   # 诊断链仍可从局部 dict 读回

    @pytest.mark.asyncio
    async def test_error_chain_is_this_call_only(self, mock_llm):
        """多调用节点共享状态：失败诊断不得读到上一调用残留的 raw/usage；
        合并只写 body 实际写过的键，上一调用残留原样保留。"""
        mock_llm.complete = AsyncMock(side_effect=LLMError("API 不可用"))
        state: dict = {"_llm_raw": "上一调用的输出", "_usage": {"input_tokens": 9}}
        view = NodeView(node="Repair", state=state)
        with pytest.raises(HarnessCallError) as ei:
            await call_harness(
                HarnessConfig(prompt_core="修 {p}"),
                {"p": "p02"},
                llm_client=mock_llm,
                view=view,
            )
        err = ei.value
        assert err.raw is None                          # 本调用无输出
        assert err.usage is None                        # 不得串上一调用的用量
        assert err.prompt == "修 p02"
        assert state["_llm_raw"] == "上一调用的输出"     # 残留不被清除
        assert state["_usage"] == {"input_tokens": 9}
        assert state["_llm_error"] == "API 不可用"       # 本调用的错误进了真实状态
        assert state["_prompt"] == "修 p02"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest module_harness/tests/test_call.py::TestCallHarnessInView -q
```

预期：3 个测试全部 ERROR/FAIL，`TypeError: call_harness() got an unexpected keyword argument 'view'`。

- [ ] **Step 3: 实现** — `module_harness/core/call.py` 两处修改。

（a）签名加参数：

```python
async def call_harness(
    config: HarnessConfig,
    values: dict[str, Any],
    *,
    llm_client: Any,
    promptmode: str | None = None,
    prompt_extra: str | None = None,
    event_bus: EventBus | None = None,
    view: NodeView | None = None,
) -> HarnessCallResult:
```

（b）docstring：在 ``event_bus`` 说明段之后、失败说明段之前插入：

```
    ``view``：节点内形态——传 script 节点自己的视图（图执行 body 拿到的
    view）。事件 ``node`` 归属 ``view.node``；body 写入 state 的
    ``_prompt``/``_llm_raw``/``_usage`` 等键在调用完成后合并回
    ``view.state``（进 NodeState.mutable_state 审计链）。state 本调用
    隔离：诊断读回（含 HarnessCallError）严格是本调用的值。不传（缺省）：
    独立调用形态，合成 ``__call__`` 视图 + 一次性局部 state，行为同
    历史版本。
```

（c）函数体：把 `state: dict[str, Any] = {}` 起到视图构造的整块

```python
    bus = event_bus or EventBus.null()
    body = Harness(config, llm_client, bus).build_body(
        promptmode=promptmode,
        prompt_extra=prompt_extra,
    )
    state: dict[str, Any] = {}
    # bind 时代：values 即具名 bind 字段（field → 值），
    # 视图按引擎对具名 bind body 的供数形态构造（v.named 直达占位符）
    view = NodeView(
        node="__call__",
        fields=tuple((key, key) for key in values),
        values=tuple(values.values()),
        state=state,
        resolved={key: Resolved(value=val, k=None) for key, val in values.items()},
    )
    result = await body(view)

    prompt = state.get("_prompt")
    raw = state.get("_llm_raw")
    usage = state.get("_usage")
```

替换为：

```python
    bus = event_bus or EventBus.null()
    body = Harness(config, llm_client, bus).build_body(
        promptmode=promptmode,
        prompt_extra=prompt_extra,
    )
    # 节点内形态（view 传入）：事件与审计状态归属真实节点；state 本调用
    # 隔离（body 只写不读，隔离保证诊断读回严格是本调用的值，多调用节点
    # 不串上一调用残留），完成后合并回 view.state（NodeState.mutable_state
    # 审计链）。独立调用形态：合成 __call__ 视图 + 一次性局部 state。
    node_name = view.node if view is not None else "__call__"
    sink: dict[str, Any] = {}
    # bind 时代：values 即具名 bind 字段（field → 值），
    # 视图按引擎对具名 bind body 的供数形态构造（v.named 直达占位符）
    body_view = NodeView(
        node=node_name,
        fields=tuple((key, key) for key in values),
        values=tuple(values.values()),
        state=sink,
        resolved={key: Resolved(value=val, k=None) for key, val in values.items()},
    )
    result = await body(body_view)

    if view is not None and view.state is not None:
        for key, val in sink.items():
            view.state[key] = val

    prompt = sink.get("_prompt")
    raw = sink.get("_llm_raw")
    usage = sink.get("_usage")
```

（其后 `if isinstance(result, Failure):` 起不变。）`NodeView`/`Resolved` 导入已存在（call.py 头部），无需新增。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest module_harness/tests/test_call.py -q
```

预期：全部 PASS（含既有 `TestCallHarnessEvents.test_events_collected_when_bus_passed` 的 `node == "__call__"` 独立形态回归钉子）。

- [ ] **Step 5: 提交**

```bash
git add module_harness/core/call.py module_harness/tests/test_call.py
git commit -m "feat(call): call_harness 节点内形态 view=——事件归属真实节点、LLM 状态链合并回 NodeState（本调用隔离不串音）"
```

---

### Task 2: 单调用四工厂透传（example 层）

**Files:**

- Modify: `example/test_ppt_master_nodes.py:67-75`（`_View`）+ 文件末尾追加测试
- Modify: `example/ppt_master/llm_nodes.py`（`make_plan_node` / `make_research_node` / `make_page_node` / `make_notes_node` 各一处 call_harness 调用）

- [ ] **Step 1: 写失败测试** — `example/test_ppt_master_nodes.py` 两处修改。

（a）`_View` 加 `state`（graph 视图本就带状态；in-node 透传经它归属事件与审计）：

```python
class _View:
    """假视图：script body 消费 .node 属性、.field(k) 方法与 .state
    （in-node 透传后 call_harness 经它归属事件与审计状态）。"""

    def __init__(self, node, fields):
        self._f = fields
        self.node = node
        self.state = {}

    def field(self, k):
        return self._f[k]
```

（b）文件末尾追加测试：

```python
def test_page_node_llm_chain_lands_in_node_state(env):
    """in-node 透传：LLM 全链审计键（_prompt/_llm_raw/_usage）落在节点状态；
    失败路径 _llm_error 入状态、无残留 raw（不串上一调用）。"""
    (env / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1280 720'></svg>"
    view = _View("P01", {"plan": {"status": "ok"}, "calibration": "{}"})
    out = _run(make_page_node(OkClient(svg))(view))
    assert out["status"] == "ok"
    assert view.state["_llm_raw"] == svg
    assert isinstance(view.state["_usage"], dict)
    assert view.state["_prompt"]

    bad_view = _View("P01", {"plan": {"status": "ok"}, "calibration": "{}"})
    out2 = _run(make_page_node(BoomClient(""))(bad_view))
    assert out2["status"] == "failed"
    assert bad_view.state["_llm_error"] == "network down"
    assert "_llm_raw" not in bad_view.state
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest example/test_ppt_master_nodes.py::test_page_node_llm_chain_lands_in_node_state -q
```

预期：FAIL，`KeyError: '_llm_raw'`（现状 call_harness 不接 view，写入一次性 dict）。

- [ ] **Step 3: 实现** — `llm_nodes.py` 四个工厂的 `call_harness(...)` 调用各加一行 `view=view,`（按各函数内锚点行定位）：

`make_plan_node`（锚点 `prompt_extra=pc.plan_prompt_pack(),`）：

```python
                llm_client=llm_client,
                prompt_extra=pc.plan_prompt_pack(),
                event_bus=event_bus,
                view=view,
            )
```

`make_research_node`（锚点 `pc.research_config(),`）：

```python
                llm_client=llm_client,
                event_bus=event_bus,
                view=view,
            )
```

`make_page_node`（锚点 `prompt_extra=pc.page_prompt_pack(),`）：

```python
                    llm_client=llm_client,
                    prompt_extra=pc.page_prompt_pack(),
                    event_bus=event_bus,
                    view=view,
                )
```

`make_notes_node`（锚点 `prompt_extra=pc.notes_prompt_pack(),`）：

```python
                llm_client=llm_client,
                prompt_extra=pc.notes_prompt_pack(),
                event_bus=event_bus,
                view=view,
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest example/test_ppt_master_nodes.py -q
```

预期：全部 PASS（既有收据断言零改动回归）。

- [ ] **Step 5: 提交**

```bash
git add example/ppt_master/llm_nodes.py example/test_ppt_master_nodes.py
git commit -m "feat(ppt_master): 单调用 LLM 节点（plan/research/page/notes）in-node 透传——LLM 全链审计键落节点状态"
```

---

### Task 3: repair/image 多调用 `_llm_calls` 累积

**Files:**

- Modify: `example/test_ppt_master_nodes.py`（repair 累积测试 + image 测试补断言）
- Modify: `example/ppt_master/llm_nodes.py`（`_record_llm_call` 助手 + `make_repair_node` / `make_image_node` 接线）

- [ ] **Step 1: 写失败测试** — `example/test_ppt_master_nodes.py` 两处修改。

（a）文件末尾追加：

```python
def test_repair_node_accumulates_llm_calls(env):
    """一节点多调用：_llm_calls 全量轨迹（含失败尝试），失败条目不串音；
    标准键 last-call-wins（最后一次调用的 _prompt 留在节点状态）。"""
    (env / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    repair = make_repair_node(BoomClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [
        {"page": "p01", "message": "溢出"},
        {"page": "p02", "message": "渐变"},
    ]}
    view = _View("FinalRepair", {"verdict": verdict, "calibration": "{}"})
    out = _run(repair(view))
    assert out["status"] == "ok" and len(out["failed"]) == 2
    calls = view.state["_llm_calls"]
    assert len(calls) == 2
    assert all(c["error"] == "network down" for c in calls)
    assert all(c["raw"] is None for c in calls)
    assert all(c["prompt"] for c in calls)
    assert view.state["_prompt"] == calls[1]["prompt"]
```

（b）`test_image_node_writes_ai_row_to_planned_file` 末尾（`assert rows[1] == ...` 之后）追加断言：

```python
    # in-node 审计：图像调用入 _llm_calls（raw=None、image_path=规范路径）
    calls = view.state["_llm_calls"]
    assert len(calls) == 1
    assert calls[0]["usage"] == {"total_tokens": 7}
    assert calls[0]["image_path"] == str(env / "images" / "cover_hero.png")
    assert calls[0]["raw"] is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest example/test_ppt_master_nodes.py -q
```

预期：两个测试 FAIL，`KeyError: '_llm_calls'`。

- [ ] **Step 3: 实现** — `llm_nodes.py` 三处修改。

（a）在 `_page_svg_path` 函数之后新增模块级助手：

```python
def _record_llm_call(view: Any, *, raw: str | None = None,
                     usage: dict[str, int] | None = None,
                     error: str | None = None, **extra: Any) -> None:
    """多调用节点累积审计：本次 LLM 调用追加进节点状态 ``_llm_calls``。

    单调用节点 harness body 原生写标准键（_prompt/_llm_raw/_usage），无需
    累积；repair/image 一节点多次 LLM 调用，标准键 last-call-wins，全量
    轨迹（含失败尝试）在 ``_llm_calls``。prompt 取 view.state 的 _prompt
    （in-node 透传下合并回的即本次值）；raw/usage 成功取 HarnessCallResult、
    失败取 HarnessCallError 诊断链；error 记失败原因；extra 补调用形态
    字段（如 image_path）。合成视图无状态（state 缺失/None）→ 跳过。
    """
    state = getattr(view, "state", None)
    if state is None:
        return
    calls = state.get("_llm_calls")
    if not isinstance(calls, list):
        calls = []
        state["_llm_calls"] = calls
    entry: dict[str, Any] = {"prompt": state.get("_prompt"), "raw": raw,
                             "usage": usage}
    if error is not None:
        entry["error"] = error
    entry.update(extra)
    calls.append(entry)
```

（b）`make_repair_node`：`call_harness` 加 `view=view,`（锚点 `prompt_extra=pc.repair_prompt_pack(),`）；成功/失败/非 SVG 三条路径各记一笔：

```python
                result = await call_harness(
                    pc.repair_config(),
                    {
                        "page": json.dumps(page, ensure_ascii=False),
                        "lock": (root / "spec_lock.md").read_text(encoding="utf-8"),
                        "calibration": json.dumps(view.field("calibration"), ensure_ascii=False),
                        "issues": json.dumps(page_issues, ensure_ascii=False),
                    },
                    llm_client=llm_client,
                    prompt_extra=pc.repair_prompt_pack(),
                    event_bus=event_bus,
                    view=view,
                )
            except HarnessCallError as e:
                _record_llm_call(view, raw=e.raw, usage=e.usage,
                                 error=str(e.failure.error))
                failed.append({"page": page_id, "error": str(e)})
                continue
            svg = result.value
            if not isinstance(svg, str) or "<svg" not in svg:
                _record_llm_call(view, raw=result.raw, usage=result.usage,
                                 error="修复输出非 SVG")
                failed.append({"page": page_id, "error": "修复输出非 SVG"})
                continue
            _write_text(_page_svg_path(root, page_id), svg)
            _record_llm_call(view, raw=result.raw, usage=result.usage)
            repaired.append(page_id)
```

（c）`make_image_node`：`call_harness` 加 `view=view,`（锚点 `{"image_prompt": row.get("prompt", "")},` 所在调用块）；成功/失败两路径各记一笔（`image_path` 记重命名后的规范路径，非落盘时的随机名）：

```python
                result = await call_harness(
                    pc.image_config(image_dir=str(root / "images")),
                    {"image_prompt": row.get("prompt", "")},
                    llm_client=llm_client,
                    event_bus=event_bus,
                    view=view,
                )
                # 生成物对齐 plan 行声明的规范路径：页 SVG 按 lock 引用
                # images/<file>，harness 落盘却是 __call__-<ns>.png 随机名
                generated = Path(result.value)
                planned = row.get("file")
                if planned:
                    dest = root / planned
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    generated.replace(dest)
                    row = {**row, "status": "terminal", "file": str(dest)}
                else:
                    row = {**row, "status": "terminal", "file": str(generated)}
                _record_llm_call(view, usage=result.usage, image_path=row["file"])
            except HarnessCallError as e:
                _record_llm_call(view, usage=e.usage, error=str(e.failure.error))
                row = {**row, "status": "Needs-Manual", "error": str(e)}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest example/test_ppt_master_nodes.py -q
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add example/ppt_master/llm_nodes.py example/test_ppt_master_nodes.py
git commit -m "feat(ppt_master): repair/image 多调用节点 _llm_calls 累积审计——全量轨迹含失败尝试，失败不串音"
```

---

### Task 4: 全链端到端审计断言（e2e）

**Files:**

- Modify: `example/test_ppt_master_e2e.py`（文件末尾追加两个测试）

- [ ] **Step 1: 写测试** — 文件末尾追加（这两个测试在 Task 1-3 未实施前必然失败，是跨层集成钉子；实施后应直接通过）：

```python
def test_llm_chain_lands_in_node_state(tmp_path, monkeypatch):
    """in-node 透传端到端：Plan/页节点 firings 的 mutable_state 携带 LLM 全链。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    firings = asyncio.run(run_generate(
        _spec(tmp_path / "deck_state"), llm_client=ScriptedMock(), persist=False))
    by_node = {f.node: f for f in firings}
    plan = by_node["Plan"]
    assert "_prompt" in plan.mutable_state and "_llm_raw" in plan.mutable_state
    assert json.loads(plan.mutable_state["_llm_raw"])["status"] == "ok"
    p01 = by_node["P01"]
    assert p01.mutable_state["_llm_raw"] == _SVG
    assert isinstance(p01.mutable_state["_usage"], dict)


def test_llm_events_attribute_to_real_nodes(tmp_path, monkeypatch):
    """in-node 透传端到端：LLM 事件归属真实节点，不再全是 "__call__"。"""
    from example.ppt_master.module import GENERATE_TEMPLATE, _build_registry
    from module_harness.infra.events import EventBus, LlmCallStarted
    from module_harness.model.module import Module
    from module_harness.model.translator import TemplateLoader

    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    bus = EventBus()
    nodes: list[str] = []
    bus.subscribe(LlmCallStarted, lambda e: nodes.append(e.node))
    loader = TemplateLoader()
    loader.register("generate", GENERATE_TEMPLATE)
    mod = Module(
        spec=_spec(tmp_path / "deck_events"),
        template_name="generate",
        template_loader=loader,
        llm_client=ScriptedMock(),
        registry=_build_registry(ScriptedMock(), bus),
        review_harness=None,
        persist=False, status_file=False, stream_log=False,
    )
    asyncio.run(mod.run(max_ticks=400))
    assert nodes, "无 LLM 事件（接线断了）"
    assert "__call__" not in nodes
    assert "Plan" in nodes and "P01" in nodes
```

- [ ] **Step 2: 跑测试确认通过**

```bash
python -m pytest example/test_ppt_master_e2e.py -q
```

预期：全部 PASS（含既有 `test_full_pipeline_mock`）。若 FAIL：firings 无 `_prompt` → 查 Task 2 四工厂是否都加了 `view=view`；事件仍 `__call__` → 查 Task 1 的 `node_name` 分支。

- [ ] **Step 3: 提交**

```bash
git add example/test_ppt_master_e2e.py
git commit -m "test(ppt_master): 全链 e2e 钉 LLM 审计三层贯通——mutable_state 携带链路、事件归属真实节点"
```

---

### Task 5: 文档同步 + 全量回归

**Files:**

- Modify: `example/ppt_master/llm_nodes.py:1-10`（模块 docstring）
- Modify: `docs/dev/progress/module-roadmap.md:93`（call_harness 条目）

- [ ] **Step 1: llm_nodes 模块 docstring 补审计说明** — 在 docstring 第一段（"为什么是 script 不是裸 harness……架构规则 4）。"）之后插入：

```

LLM 链审计（in-node 透传，spec 2026-10-01）：call_harness(view=view) 把
事件归属与 state 写入（_prompt/_llm_raw/_usage）接到真实节点——单调用
节点零额外代码；repair/image 一节点多调用，全量轨迹（含失败尝试）累积在
_llm_calls（_record_llm_call），标准键 last-call-wins。
```

- [ ] **Step 2: roadmap 条目补一笔** — `docs/dev/progress/module-roadmap.md` 中 `- [x] **task 级 API 地板 call_harness**` 行（约 93 行）行尾追加：

```markdown
；**节点内形态 `view=`**（2026-10-01）：事件归属与 LLM 状态链（`_prompt`/`_llm_raw`/`_usage`/`_llm_calls`）进 NodeState 审计，设计见 docs/dev/superpowers/specs/2026-10-01-harness-innode-audit-design.md
```

并把该文件头部 `> 最后更新：2026-09-01（…）` 改为 `> 最后更新：2026-10-01（call_harness 节点内形态：LLM 链进审计）`。

- [ ] **Step 3: 全量回归**

```bash
python -m pytest module_harness/tests/ -q
python -m pytest example/ -q
```

预期：两个套件全部 PASS、0 failed。

- [ ] **Step 4: 提交**

```bash
git add example/ppt_master/llm_nodes.py docs/dev/progress/module-roadmap.md
git commit -m "docs: in-node 透传文档同步——llm_nodes 模块说明与 roadmap call_harness 条目"
```

---

## 完成判定（对照 spec §1 目标 / §8 测试策略）

1. `TestCallHarnessInView` 三测过：事件归属真实节点、状态合并回、失败诊断本调用隔离。
2. `test_page_node_llm_chain_lands_in_node_state` / `test_repair_node_accumulates_llm_calls` / image 断言过：标准键 + `_llm_calls` 落点正确。
3. e2e 两测过：全链 firings 的 `mutable_state` 携带 LLM 链；事件不含 `"__call__"`。
4. 收据结构零改动（既有断言全绿）；独立调用方（consistency.py）零改动（`TestCallHarnessEvents.__call__` 钉子绿）。
5. 两套件全量回归 0 failed。
