"""确定性节点：ingest/plan_validate/门裁决/readiness/report；run_tool 透传。

gate 报告真实形状（Task 1 产物 + vendor 源码 checker.py:_provenance_categories
钉死）：categories.blocking.issues 路径存在，issue 条目是
``{"file": "page_p01.svg", "message": ...}``——无 ``page`` 键，门裁决需归一化。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from example.ppt_master import workspace


class _View:
    def __init__(self, node, fields): self._f = fields; self.node = node
    def field(self, k): return self._f[k]


def _env(tmp_path, monkeypatch, roster=None, sources=None):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    root = workspace.init_workspace(tmp_path / "deck")
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": roster or [{"id": "p01", "title": "封面", "role": "cover"}],
        "sources": sources or [],
    })
    return root


def test_ingest_copies_md_and_digests(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import ingest
    src = tmp_path / "paper.md"
    src.write_text("# 论文\n" + "内容" * 100, encoding="utf-8")
    _env(tmp_path, monkeypatch, sources=[str(src)])
    out = ingest(_View("Ingest", {}))   # sync 直呼
    assert out["status"] == "ok" and out["digest"]
    assert (tmp_path / "deck" / "sources" / "paper.md").exists()


def test_plan_validate_catches_roster_drift(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import plan_validate
    root = _env(tmp_path, monkeypatch, roster=[
        {"id": "p01", "title": "封面", "role": "cover"},
        {"id": "p02", "title": "背景", "role": "content"},
    ])
    # design_spec 页册漂移：缺 p02
    (root / "design_spec.md").write_text(
        "# spec\n## p01 封面\nAudience move: 建立信任\n", encoding="utf-8")
    (root / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    from tickflow import Failure
    out = plan_validate(_View("PlanValidate", {"plan": {"roster_ids": ["p01"]}}))
    assert isinstance(out, Failure) and out.type == "infrastructure"


def test_plan_validate_passes(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import plan_validate
    root = _env(tmp_path, monkeypatch, roster=[
        {"id": "p01", "title": "封面", "role": "cover"},
        {"id": "p02", "title": "背景", "role": "content"},
    ])
    (root / "design_spec.md").write_text(
        "# spec\n## p01 封面\nAudience move: 建立信任\n"
        "## p02 背景\nAudience move: 给出动机\n", encoding="utf-8")
    # 两个锁锚点都必须在（palette + typography）
    (root / "spec_lock.md").write_text(
        "# lock\npalette: #000000\ntypography: sans\n", encoding="utf-8")
    out = plan_validate(_View("PlanValidate", {"plan": {"roster_ids": ["p01", "p02"]}}))
    assert out == {"status": "ok"}


def test_gate_verdict_parses_blocking(tmp_path, monkeypatch):
    """真实报告形状：issue 无 page 键（file="page_p01.svg"）→ 归一化出 page。"""
    from example.ppt_master.tools_nodes import make_gate_verdict
    root = _env(tmp_path, monkeypatch)
    report = {"categories": {"blocking": {"count": 1, "issues": [
        {"file": "page_p01.svg", "message": "文本溢出"}]}}}
    workspace.gate_report_path(root, "final").write_text(
        json.dumps(report), encoding="utf-8")
    verdict = make_gate_verdict("final")
    out = verdict(_View("FinalVerdict", {"gate": {"returncode": 1}}))
    assert out["ok"] is False and out["issues"][0]["page"] == "p01"
    # 原始字段保留（repair prompt 上下文要 file/message）
    assert out["issues"][0]["message"] == "文本溢出"


def test_gate_verdict_without_report_is_clean(tmp_path, monkeypatch):
    """报告缺失 = 未跑门（无 blocking 证据）→ ok，不猜。"""
    from example.ppt_master.tools_nodes import make_gate_verdict
    root = _env(tmp_path, monkeypatch)
    out = make_gate_verdict("early")(_View("EarlyVerdict", {"gate": {"returncode": 0}}))
    assert out == {"stage": "early", "returncode": 0, "ok": True, "issues": []}
    assert not workspace.gate_report_path(root, "early").exists()


def test_guard_counts_blocking_only(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import make_guard
    root = _env(tmp_path, monkeypatch)
    guard = make_guard("final", want_clean=True)
    assert guard(_View("Guard", {})) is True   # 无报告 → 无 blocking 证据
    report = {"categories": {"blocking": {"count": 1, "issues": [
        {"scope": "template", "message": "模板错误"}]}}}
    workspace.gate_report_path(root, "final").write_text(
        json.dumps(report), encoding="utf-8")
    assert guard(_View("Guard", {})) is False
    assert make_guard("final", want_clean=False)(_View("Guard", {})) is True


def test_image_readiness_gate(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import image_readiness
    from tickflow import Failure
    root = _env(tmp_path, monkeypatch)
    # field("rows") 绑定 ImageAcquire 输出收据（translator: inputs.rows = ImageAcquire）
    manual_missing = {"rows": {"status": "ok", "rows": [
        {"id": "p02", "name": "背景图", "status": "Needs-Manual"},   # 缺 file
        {"id": "p03", "name": "示意", "status": "Needs-Manual",
         "file": "chart.png"},                                        # 文件不存在
        {"id": "p01", "name": "封面", "status": "terminal"},
    ]}}
    out = image_readiness(_View("ImageReady", manual_missing))
    assert isinstance(out, Failure) and out.type == "infrastructure"
    # 列名优先级 = name → file → id（p03 有 name，故列"示意"而非 chart.png）
    assert "背景图" in out.error and "示意" in out.error
    # Needs-Manual 行已有真实文件 → ready
    (root / "images" / "chart.png").write_bytes(b"png")
    manual_ok = {"rows": {"status": "ok", "rows": [
        {"id": "p03", "name": "示意", "status": "Needs-Manual", "file": "chart.png"}]}}
    assert image_readiness(_View("ImageReady", manual_ok)) == {"status": "ready"}


def test_report_ok_and_error(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import report
    root = _env(tmp_path, monkeypatch)
    fields = {"export": {"returncode": 0}, "finalize": {"returncode": 0}}
    assert report(_View("Report", fields))["status"] == "error"   # 无 pptx 产物
    (root / "exports" / "deck.pptx").write_bytes(b"pptx")
    ok = report(_View("Report", fields))
    assert ok["status"] == "ok" and ok["pptx"] == [str(root / "exports" / "deck.pptx")]
    bad = report(_View("Report", {**fields, "export": {"returncode": 1}}))
    assert bad["status"] == "error"


def test_run_tool_passes_envelope_dir_and_tool_args(tmp_path, monkeypatch):
    """-- 分隔后的工具参数原样透传；cmd 首个位置参数 = 信封 output_dir。"""
    from example.ppt_master.tools import run_tool
    root = tmp_path / "deck"
    root.mkdir()
    envelope = tmp_path / "env.json"
    envelope.write_text(json.dumps({"output_dir": str(root)}), encoding="utf-8")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 7)

    monkeypatch.setattr(run_tool.subprocess, "run", fake_run)
    rc = run_tool.main(["--envelope", str(envelope),
                        "--tool", "svg_quality_checker.py",
                        "--", "--stage", "final", "--json"])
    assert rc == 7   # 退出码原样透传
    tool = (Path(run_tool.__file__).resolve().parent.parent
            / "vendor" / "ppt_master" / "scripts" / "svg_quality_checker.py")
    assert captured["cmd"] == [sys.executable, str(tool), str(root),
                               "--stage", "final", "--json"]
