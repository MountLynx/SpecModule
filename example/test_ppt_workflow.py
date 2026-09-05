# example/test_ppt_workflow.py
"""模板制作工作流测试（5.3）：合规草稿入库（manifest 新条目、源文件未改动）；
缺占位符草稿被拒（逐项清单、reference/ 与 manifest 零变更）；mock 冒烟不阻断。

reference/ 经 monkeypatch normalize.REFERENCE_DIR 指向临时目录——不入库
真实模块资产。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from example.ppt_writer import normalize
from example.ppt_writer.module import run_template_review
from llm.mock import MockLLMClient


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _draft(tmp_path: Path, layout_index: int, name: str = "draft.pptx") -> Path:
    from pptx import Presentation
    pres = Presentation()
    pres.save(str(tmp_path / name))
    return tmp_path / name


def _run(spec: dict, tmp_path: Path, monkeypatch) -> list:
    """在隔离 reference/ 下跑制作工作流（mock LLM）。"""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    monkeypatch.setattr(normalize, "REFERENCE_DIR", ref_dir)
    return asyncio.run(run_template_review(
        spec, llm_client=MockLLMClient(), persist=False,
    ))


def _node_output(firings, name: str) -> dict:
    return next(f.output for f in firings if f.node == name)


def test_compliant_draft_registered(tmp_path: Path, monkeypatch):
    draft = _draft(tmp_path, 1)  # Title and Content：content kind 合规
    src_hash = _sha256(draft)
    firings = _run({
        "draft_pptx": str(draft),
        "template_name": "content",
        "layout": 1,
        "kind": "content",
    }, tmp_path, monkeypatch)

    register = _node_output(firings, "Register")
    assert register["action"] == "registered", register
    assert register["template_name"] == "content"
    assert register["manifest_entry"] == {"file": "content.pptx", "layout": 1, "kind": "content"}
    # 形态诊断随 Register 透出（草稿无自带页 → 无提醒，字段存在即可）
    assert "warnings" in register and isinstance(register["warnings"], list)

    # manifest 出现新条目 + 文件入库
    manifest = normalize.load_manifest(tmp_path / "reference")
    assert manifest["content"] == {"file": "content.pptx", "layout": 1, "kind": "content"}
    assert (tmp_path / "reference" / "content.pptx").exists()
    # 源草稿未被修改（复制而非移动）
    assert _sha256(draft) == src_hash
    assert _sha256(tmp_path / "reference" / "content.pptx") == src_hash

    # mock 意见节点不阻断（占位意见文本）
    opinion = _node_output(firings, "Opinion")
    assert "mock output" in str(opinion)


def test_noncompliant_draft_rejected_without_writes(tmp_path: Path, monkeypatch):
    draft = _draft(tmp_path, 5)  # Title Only：content kind 缺文本占位符
    ref_dir = tmp_path / "reference"
    firings = _run({
        "draft_pptx": str(draft),
        "template_name": "content",
        "layout": 5,
        "kind": "content",
    }, tmp_path, monkeypatch)

    reject = _node_output(firings, "Reject")
    assert reject["action"] == "rejected", reject
    assert any("缺少文本占位符" in i for i in reject["issues"])
    assert isinstance(reject["warnings"], list)
    # 零写入：reference/ 无新文件（仅 manifest.json 可能不存在或为空），manifest 无新条目
    assert list(ref_dir.glob("*.pptx")) == []
    assert normalize.load_manifest(ref_dir) == {}
    # Register 节点未执行
    assert not any(f.node == "Register" for f in firings)


def test_noncompliant_picture_draft_lists_missing_image(tmp_path: Path, monkeypatch):
    draft = _draft(tmp_path, 1)  # Title and Content：picture kind 缺图片占位符
    firings = _run({
        "draft_pptx": str(draft),
        "template_name": "cover",
        "layout": 1,
        "kind": "picture",
    }, tmp_path, monkeypatch)
    reject = _node_output(firings, "Reject")
    assert any("缺少图片占位符" in i for i in reject["issues"])
    assert (tmp_path / "reference" / "cover.pptx").exists() is False


def test_review_spec_validation_errors():
    """翻译器入口校验：缺字段/坏 kind/layout 报含字段路径的错误。"""
    from example.ppt_writer.module import tl_template_review
    from tickflow.views import NodeView

    def run_spec(spec):
        # 合成视图按具名 bind 供数（与 translator._translator_view 同形态）
        view = NodeView(
            node="t",
            fields=(("spec", "spec"),),
            values=(spec,),
        )
        return tl_template_review(view)

    import pytest
    with pytest.raises(ValueError, match="draft_pptx"):
        run_spec({"template_name": "x", "layout": 0, "kind": "content"})
    with pytest.raises(ValueError, match="kind 'nope' 未知"):
        run_spec({"draft_pptx": "a.pptx", "template_name": "x", "layout": 0, "kind": "nope"})
    with pytest.raises(ValueError, match="'layout' 应为整数"):
        run_spec({"draft_pptx": "a.pptx", "template_name": "x", "layout": "0", "kind": "content"})
