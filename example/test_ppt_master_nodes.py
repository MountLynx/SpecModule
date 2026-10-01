"""LLM 节点工厂：成功收据 / 失败收据（AND 不饿死）/ 修复轮上限。"""

from __future__ import annotations

import json

import pytest

from example.ppt_master import workspace
from example.ppt_master.llm_nodes import (
    make_page_node,
    make_plan_node,
    make_repair_node,
)


class OkClient:
    """prompt 含 'roster_ids'（规划收据 schema 提示，_PLAN_CORE 原样透传）
    返回计划收据 JSON，否则返回合格 SVG。

    注：规划 prompt 与页 prompt 的真实可判别标记是 roster_ids——它只出现在
    规划 prompt（收据 schema 提示）中，页 prompt（executor 包）不含。
    """

    def __init__(self, svg):
        self.svg = svg

    async def complete(self, **kw):
        from llm.client import LLMResponse

        prompt = kw.get("prompt", "")
        if "roster_ids" in prompt:
            receipt = {
                "status": "ok", "roster_ids": ["p01", "p02"],
                "design_spec_md": "# spec\n§IX\n## p01 封面\nAudience move: 建立信任\n## p02 背景\nAudience move: 给出动机\n",
                "spec_lock_md": "# lock\npalette: #000000\n",
                "image_rows": [], "icon_pool": [],
                "notes_enabled": True,
            }
            return LLMResponse(content=json.dumps(receipt))
        return LLMResponse(content=self.svg)


class BoomClient(OkClient):
    """LLM 挂——按 llm.client 错误契约抛 LLMError（harness body 据此映射
    infrastructure Failure → call_harness 抛 HarnessCallError；
    裸 RuntimeError 会炸穿节点，不是真实的客户端失败形态）。"""

    async def complete(self, **kw):
        from llm.client import LLMError

        raise LLMError("network down")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    root = workspace.init_workspace(tmp_path / "deck")
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": [{"id": "p01", "title": "封面", "role": "cover"},
                   {"id": "p02", "title": "背景", "role": "content"}],
    })
    return root


class _View:
    """假视图：script body 消费 .node 属性、.field(k) 方法与 .state
    （in-node 透传后 call_harness 经它归属事件与审计状态）。"""

    def __init__(self, node, fields):
        self._f = fields
        self.node = node
        self.state = {}

    def field(self, k):
        return self._f[k]


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_plan_node_writes_files_and_receipt(env):
    node = make_plan_node(OkClient("<svg/>"))
    view = _View("Plan", {"source": {"digest": "论文内容"}})
    out = _run(node(view))
    assert out["status"] == "ok"
    assert (env / "design_spec.md").exists() and (env / "spec_lock.md").exists()
    assert out["roster_ids"] == ["p01", "p02"]


def test_page_node_receipt_ok_and_failed(env):
    # 页节点前置：spec_lock.md 是 plan 产物（节点无静默兜底，缺文件即异常）
    (env / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1280 720'></svg>"
    page = make_page_node(OkClient(svg))
    view = _View("P01", {"plan": {"status": "ok"}, "calibration": "{}"})
    out = _run(page(view))
    assert out["status"] == "ok"
    # 页 SVG 命名约定（Task 1 四件套实测锁定）：svg_output/page_<页id>.svg
    assert (env / "svg_output" / "page_p01.svg").exists()

    # LLM 挂 → 失败收据（不是 Failure！AND join 不饿死）
    bad = make_page_node(BoomClient(""))
    out2 = _run(bad(view))
    assert out2["status"] == "failed" and "network down" in out2["error"]


class ImageGenClient:
    """generate_image 假客户端（图像模式只走 generate_image，不走 complete）。"""

    def __init__(self):
        self.calls = []

    async def generate_image(self, **kw):
        from llm.client import ImageResult

        self.calls.append(kw)
        return ImageResult(data=b"PNGBYTES", usage={"total_tokens": 7})


def test_image_node_writes_ai_row_to_planned_file(env):
    """AI 行生成后必须落到 plan 行声明的规范路径：页 SVG 按 lock 引用
    images/<file>，harness 落盘却是 __call__-<ns>.png 随机名——无人回接则
    图永远进不了 deck。user 行原样透传（文件由置入保证）。"""
    from example.ppt_master.llm_nodes import make_image_node

    client = ImageGenClient()
    node = make_image_node(client)
    view = _View("ImageAcquire", {"plan": {"image_rows": [
        {"page": "p01", "name": "hero", "file": "images/cover_hero.png",
         "acquire": "ai", "prompt": "granule hero", "status": "pending"},
        {"page": "p04", "name": "fig1", "file": "images/fig1.png",
         "acquire": "user", "status": "ready"},
    ]}})
    out = _run(node(view))
    rows = out["rows"]
    # ai 行：terminal + 规范路径在盘，随机名临时文件不残留
    assert rows[0]["status"] == "terminal"
    assert rows[0]["file"] == str(env / "images" / "cover_hero.png")
    assert (env / "images" / "cover_hero.png").read_bytes() == b"PNGBYTES"
    assert not list((env / "images").glob("__call__-*"))
    # 生图 prompt 原样进调用；user 行透传
    assert client.calls and client.calls[0]["prompt"] == "granule hero"
    assert rows[1] == {"page": "p04", "name": "fig1", "file": "images/fig1.png",
                       "acquire": "user", "status": "ready"}
    # in-node 审计：图像调用入 _llm_calls（raw=None、image_path=规范路径）
    calls = view.state["_llm_calls"]
    assert len(calls) == 1
    assert calls[0]["usage"] == {"total_tokens": 7}
    assert calls[0]["image_path"] == str(env / "images" / "cover_hero.png")
    assert calls[0]["raw"] is None
    assert "granule hero" in calls[0]["prompt"]   # 图像模式的渲染 prompt 也入账


def test_repair_node_round_limit(env):
    repair = make_repair_node(OkClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [{"page": "p01", "message": "溢出"}]}
    view = _View("FinalRepair", {"verdict": verdict, "calibration": "{}"})
    rounds = env / "validation" / "repair_rounds.json"
    rounds.write_text(json.dumps({"final": 2}), encoding="utf-8")
    from tickflow import Failure

    out = _run(repair(view))
    assert isinstance(out, Failure) and out.type == "infrastructure"


def test_repair_node_namespaced_name_resolves_early_stage(env):
    """命名空间前缀不得漂移 stage 判定："ppt_master:EarlyRepair" → early。

    若误判为 final，则 early 计数不动、final 从 1 起步 → 不触发上限，
    节点继续走修复循环返回 ok 收据；正确判定则 early 2+1 超限 → Failure。
    """
    repair = make_repair_node(OkClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [{"page": "p01", "message": "溢出"}]}
    view = _View("ppt_master:EarlyRepair", {"verdict": verdict, "calibration": "{}"})
    rounds = env / "validation" / "repair_rounds.json"
    rounds.write_text(json.dumps({"early": 2}), encoding="utf-8")
    from tickflow import Failure

    out = _run(repair(view))
    assert isinstance(out, Failure) and out.type == "infrastructure"
    assert "early" in out.error and "超过上限" in out.error
    # 计数确实记进 early 桶（而非另开 final 桶）
    assert json.loads(rounds.read_text(encoding="utf-8")) == {"early": 3}


def test_page_node_llm_chain_lands_in_node_state(env):
    """in-node 透传：LLM 全链审计键（_prompt/_llm_raw/_usage）落在节点状态；
    失败路径 _llm_error 入状态、本调用不写 _llm_raw（跨调用不串音由
    test_repair_node_accumulates_llm_calls 以共享视图钉死）。"""
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
    assert calls[0]["prompt"] != calls[1]["prompt"]   # 两页 prompt 确实不同
    assert view.state["_prompt"] == calls[1]["prompt"]


class JunkRepairClient(OkClient):
    """规划收据正常、修复输出非 SVG——钉 repair 非 SVG 分支的审计条目。"""

    async def complete(self, **kw):
        from llm.client import LLMResponse

        if "roster_ids" in kw.get("prompt", ""):
            return await super().complete(**kw)
        return LLMResponse(content="这不是 SVG", usage={"input_tokens": 3})


def test_repair_node_records_non_svg_rejection(env):
    """修复输出非 SVG：调用真实发生——条目带 raw 与 error，页进 failed。"""
    (env / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    repair = make_repair_node(JunkRepairClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [{"page": "p01", "message": "溢出"}]}
    view = _View("FinalRepair", {"verdict": verdict, "calibration": "{}"})
    out = _run(repair(view))
    assert out["status"] == "ok" and len(out["failed"]) == 1
    assert out["failed"][0]["error"] == "修复输出非 SVG"
    calls = view.state["_llm_calls"]
    assert len(calls) == 1
    assert calls[0]["error"] == "修复输出非 SVG"
    assert calls[0]["raw"] == "这不是 SVG"
    assert calls[0]["usage"] == {"input_tokens": 3}


class BoomImageClient:
    """generate_image 抛 LLMError——钉 image 失败分支的审计条目。"""

    async def generate_image(self, **kw):
        from llm.client import LLMError

        raise LLMError("image api down")


def test_image_node_records_failure_entry(env):
    """图像调用失败：条目带 error、usage 为 None（本调用无产出）、行标 Needs-Manual。"""
    from example.ppt_master.llm_nodes import make_image_node

    node = make_image_node(BoomImageClient())
    view = _View("ImageAcquire", {"plan": {"image_rows": [
        {"page": "p01", "name": "hero", "file": "images/cover_hero.png",
         "acquire": "ai", "prompt": "granule hero", "status": "pending"},
    ]}})
    out = _run(node(view))
    assert out["rows"][0]["status"] == "Needs-Manual"
    calls = view.state["_llm_calls"]
    assert len(calls) == 1
    assert calls[0]["error"] == "image api down"
    assert calls[0]["usage"] is None
    assert "image_path" not in calls[0]


def test_repair_rounds_do_not_pollute_previous_record(env):
    """跨 firing 快照保真：引擎 record 浅拷贝状态下，第 2 轮条目不得
    追进第 1 轮已捕获记录共享的 list（rebind 不 append）。"""
    (env / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    repair = make_repair_node(OkClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [{"page": "p01", "message": "溢出"}]}
    view = _View("FinalRepair", {"verdict": verdict, "calibration": "{}"})
    _run(repair(view))                          # 第 1 轮
    round1_snapshot = dict(view.state)          # 引擎 record 的浅拷贝语义
    _run(repair(view))                          # 第 2 轮（同视图续跑）
    assert len(round1_snapshot["_llm_calls"]) == 1    # 上一轮记录未被追溯污染
    assert len(view.state["_llm_calls"]) == 2
    # 顺带钉成功条目形态（质量审 Minor-2）：raw=SVG、无 error 键
    assert round1_snapshot["_llm_calls"][0]["raw"] == "<svg/>"
    assert "error" not in round1_snapshot["_llm_calls"][0]
    assert round1_snapshot["_llm_calls"][0]["prompt"]
