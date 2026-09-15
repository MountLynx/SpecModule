"""workspace：目录契约建立 + 信封读写（pid 隔离）。"""

from __future__ import annotations

from example.ppt_master import workspace


def test_init_workspace_creates_contract(tmp_path):
    root = workspace.init_workspace(tmp_path / "deck")
    for d in ("sources", "analysis", "images", "icons", "svg_output",
              "svg_final", "notes", "validation", "exports"):
        assert (root / d).is_dir(), d
    assert (root / "validation" / "workflow.log").exists()
    # 幂等：重复调用不抛
    workspace.init_workspace(root)


def test_envelope_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    workspace.write_envelope({"output_dir": str(tmp_path), "roster": []})
    assert workspace.read_envelope()["output_dir"] == str(tmp_path)


def test_gate_report_path(tmp_path):
    assert workspace.gate_report_path(tmp_path, "early").name == "svg_quality_early_report.json"
    assert workspace.gate_report_path(tmp_path, "final").name == "svg_quality_report.json"


def test_append_workflow_log(tmp_path):
    workspace.init_workspace(tmp_path)
    workspace.append_workflow_log(tmp_path, "手工恢复一次")
    log = (tmp_path / "validation" / "workflow.log").read_text(encoding="utf-8")
    assert "手工恢复一次" in log
