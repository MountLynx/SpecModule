"""确定性节点：ingest/plan_validate/门裁决/readiness/report；run_tool 透传。

gate 报告真实形状（Task 1 产物 + vendor 源码 checker.py:_provenance_categories
钉死）：categories.blocking.issues 路径存在，issue 条目是
``{"file": "page_p01.svg", "message": ...}``——无 ``page`` 键，门裁决需归一化。
门路由 = checker exit 0 **且** 报告在盘且无 blocking（checker 在落盘前可
sys.exit，崩溃/缺报告的门一律不放行）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_ingest_missing_source_is_contract_note(tmp_path, monkeypatch):
    """源缺失不炸图：摘记忆契约驱动注记，status 仍 ok。"""
    from example.ppt_master.tools_nodes import ingest
    _env(tmp_path, monkeypatch, sources=[str(tmp_path / "ghost.md")])
    out = ingest(_View("Ingest", {}))
    assert out["status"] == "ok" and out["copied"] == []
    assert "（源缺失，契约驱动）" in out["digest"]


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
    # 漂移消息两侧都可见（收据页册 vs spec 页册）
    assert "收据页册 ['p01']" in out.error and "['p01', 'p02']" in out.error


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


def test_plan_validate_missing_spec_files_fail_loud(tmp_path, monkeypatch):
    """spec 文件缺失 → 聚合进 infrastructure Failure，不炸裸 FileNotFoundError。"""
    from example.ppt_master.tools_nodes import plan_validate
    from tickflow import Failure
    root = _env(tmp_path, monkeypatch)   # 不写 design_spec / spec_lock
    out = plan_validate(_View("PlanValidate", {"plan": {"roster_ids": ["p01"]}}))
    assert isinstance(out, Failure) and out.type == "infrastructure"
    assert "design_spec.md 缺失" in out.error and "spec_lock.md 缺失" in out.error


def test_gate_verdict_parses_blocking(tmp_path, monkeypatch):
    """真实报告形状：issue 无 page 键（file="page_p01.svg"）→ 归一化出 page。"""
    from example.ppt_master.tools_nodes import make_gate_verdict
    root = _env(tmp_path, monkeypatch)
    report = {"categories": {"blocking": {"count": 2, "issues": [
        {"file": "page_p01.svg", "message": "文本溢出"},
        {"file": "Page_P02.SVG", "message": "字型漂移"},
    ]}}}
    workspace.gate_report_path(root, "final").write_text(
        json.dumps(report), encoding="utf-8")
    verdict = make_gate_verdict("final")
    out = verdict(_View("FinalVerdict", {"gate": {"returncode": 1}}))
    assert out["ok"] is False and out["issues"][0]["page"] == "p01"
    # 派生 page 统一小写（页 id 约定小写，repair 按 id 查页册）
    assert out["issues"][1]["page"] == "p02"
    # 原始字段保留（repair prompt 上下文要 file/message）
    assert out["issues"][0]["message"] == "文本溢出"


def test_gate_verdict_requires_exit_zero(tmp_path, monkeypatch):
    """verdict ok = checker exit 0 且无 blocking——rc≠0 时报告干净也不是 ok。"""
    from example.ppt_master.tools_nodes import make_gate_verdict
    root = _env(tmp_path, monkeypatch)
    verdict = make_gate_verdict("final")
    # 报告缺失 + rc≠0（checker 落盘前崩溃）→ ok False，收据记 rc
    out = verdict(_View("FinalVerdict", {"gate": {"returncode": 7}}))
    assert out == {"stage": "final", "returncode": 7, "ok": False, "issues": []}
    # rc=0 + 报告有 blocking → ok False
    report = {"categories": {"blocking": {"count": 1, "issues": [
        {"file": "page_p01.svg", "message": "文本溢出"}]}}}
    workspace.gate_report_path(root, "final").write_text(
        json.dumps(report), encoding="utf-8")
    out = verdict(_View("FinalVerdict", {"gate": {"returncode": 0}}))
    assert out["ok"] is False and out["issues"][0]["page"] == "p01"
    # rc=0 + 干净报告 → ok True
    workspace.gate_report_path(root, "final").write_text(
        json.dumps({"categories": {"blocking": {"count": 0, "issues": []}}}),
        encoding="utf-8")
    out = verdict(_View("FinalVerdict", {"gate": {"returncode": 0}}))
    assert out == {"stage": "final", "returncode": 0, "ok": True, "issues": []}


def test_guard_requires_exit_zero_and_report(tmp_path, monkeypatch):
    """守卫无证据不放行：崩溃（rc≠0）或报告未落盘 ≠ 干净。

    gate 键 = 守卫边源任务（门 command 节点）的输出收据。
    """
    from example.ppt_master.tools_nodes import make_guard
    root = _env(tmp_path, monkeypatch)
    # (a) 报告缺失 + rc≠0 → want_clean 不放行
    assert make_guard("final", True)(_View("Guard", {"gate": {"returncode": 1}})) is False
    # rc=0 但报告未落盘 → 同样不放行（checker 声称成功却无证据）
    assert make_guard("final", True)(_View("Guard", {"gate": {"returncode": 0}})) is False
    # (b) rc=0 + blocking 报告：want_clean False；反向 guard True
    report = {"categories": {"blocking": {"count": 1, "issues": [
        {"scope": "template", "message": "模板错误"}]}}}
    workspace.gate_report_path(root, "final").write_text(
        json.dumps(report), encoding="utf-8")
    assert make_guard("final", True)(_View("Guard", {"gate": {"returncode": 0}})) is False
    assert make_guard("final", False)(_View("Guard", {"gate": {"returncode": 0}})) is True
    # (c) rc=0 + 干净报告 → 放行
    workspace.gate_report_path(root, "final").write_text(
        json.dumps({"categories": {"blocking": {"count": 0, "issues": []}}}),
        encoding="utf-8")
    assert make_guard("final", True)(_View("Guard", {"gate": {"returncode": 0}})) is True


def test_calibrate_receipt_carries_stderr(tmp_path, monkeypatch):
    """校准软降级（无产物 → empty + "{}"）是有意的；stderr 全文进收据供审计。"""
    import example.ppt_master.tools_nodes as tn
    root = _env(tmp_path, monkeypatch)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="measured 3 roles")

    monkeypatch.setattr(tn.subprocess, "run", fake_run)
    empty = tn.calibrate(_View("Calibrate", {}))
    assert empty["status"] == "empty" and empty["calibration"] == "{}"
    assert empty["stderr"] == "measured 3 roles" and empty["returncode"] == 0
    # 产物在盘 → ok + 全文
    cal = root / "validation" / "text_calibration.json"
    cal.write_text('{"body": 20}', encoding="utf-8")
    ok = tn.calibrate(_View("Calibrate", {}))
    assert ok["status"] == "ok" and ok["calibration"] == '{"body": 20}'


def test_icon_sync_noop_and_failure_types(tmp_path, monkeypatch):
    """空池 no-op（不起子进程）；rc≠0 → Failure(llm)；成功 → synced 计数。"""
    import example.ppt_master.tools_nodes as tn
    from tickflow import Failure
    _env(tmp_path, monkeypatch)
    calls: list[list] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(tn.subprocess, "run", fake_run)
    # 空池 → no-op，subprocess 从未起
    assert tn.icon_sync_node(_View("IconSync", {"plan": {}})) == {"status": "ok", "synced": 0}
    assert tn.icon_sync_node(_View("IconSync", {"plan": {"icon_pool": []}})) == {"status": "ok", "synced": 0}
    assert calls == []
    # 成功 → synced = 池大小
    assert tn.icon_sync_node(_View("IconSync", {"plan": {"icon_pool": ["a", "b"]}})) == {"status": "ok", "synced": 2}
    assert len(calls) == 1
    # rc≠0 → Failure(type="llm")（修复轮可重试，不炸图）
    monkeypatch.setattr(
        tn.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, stdout="", stderr="boom"))
    out = tn.icon_sync_node(_View("IconSync", {"plan": {"icon_pool": ["a"]}}))
    assert isinstance(out, Failure) and out.type == "llm" and "boom" in out.error


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
    # 无透传参数（nargs="*" 缺省 []）→ cmd 只有 root
    run_tool.main(["--envelope", str(envelope), "--tool", "svg_quality_checker.py"])
    assert captured["cmd"] == [sys.executable, str(tool), str(root)]


def test_run_tool_bad_envelope_errors_cleanly(tmp_path, capsys):
    """信封缺失/损坏 → parser.error（用法级报错退出 2），不甩裸 traceback。"""
    from example.ppt_master.tools import run_tool
    with pytest.raises(SystemExit) as ei:
        run_tool.main(["--envelope", str(tmp_path / "nope.json"), "--tool", "x.py"])
    assert ei.value.code == 2
    assert "信封缺失或损坏" in capsys.readouterr().err
    corrupted = tmp_path / "bad.json"
    corrupted.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        run_tool.main(["--envelope", str(corrupted), "--tool", "x.py"])
    assert ei.value.code == 2
