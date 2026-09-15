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
    """假视图：script body 只消费 .node 属性与 .field(k) 方法。"""

    def __init__(self, node, fields):
        self._f = fields
        self.node = node

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


def test_repair_node_round_limit(env):
    repair = make_repair_node(OkClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [{"page": "p01", "message": "溢出"}]}
    view = _View("FinalRepair", {"verdict": verdict, "calibration": "{}"})
    rounds = env / "validation" / "repair_rounds.json"
    rounds.write_text(json.dumps({"final": 2}), encoding="utf-8")
    from tickflow import Failure

    out = _run(repair(view))
    assert isinstance(out, Failure) and out.type == "infrastructure"
