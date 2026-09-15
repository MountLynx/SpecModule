"""翻译器展开规则：页节点数、批次门、条件段、≤6 无早门、命令串形状。"""

from __future__ import annotations

import pytest

from example.ppt_master import workspace
from example.ppt_master.translator import build_generate_tasklist


def _spec(n: int, **kw) -> dict:
    spec = {
        "project": "t",
        "source": {"kind": "files", "paths": ["a.md"]},
        "roster": [{"id": f"p{i:02d}", "title": f"页{i}"} for i in range(1, n + 1)],
    }
    spec.update(kw)
    return spec


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)


def test_four_pages_no_early_gate():
    tasks, flow = build_generate_tasklist(_spec(4))
    for i in range(1, 5):
        assert f"P{i:02d}" in tasks
    assert "EarlyGate" not in tasks
    assert "FinalGate" in tasks and "Repair" in tasks
    assert "NotesGen" in tasks  # notes 缺省开
    assert "SplitNotes" in tasks and "Export" in tasks
    assert "ImageAcquire" not in tasks  # 无 images 声明
    # 修复回边入 AND 门永不点火（Repair 槽位永不齐整）→ FinalGate 必须 OR-join
    assert "FinalGate.join: OR" in flow


def test_ten_pages_has_early_gate_and_batching():
    tasks, flow = build_generate_tasklist(_spec(10))
    assert "EarlyGate" in tasks and "EarlyRepair" in tasks
    # 批1 = P01..P05 直连 EarlyGate；P06..P10 与 EarlyRepair 汇入 FinalGate
    assert "P05 --> EarlyGate" in flow
    assert "EarlyGate --> EarlyVerdict" in flow
    assert "EarlyRepair --> FinalGate" in flow
    assert "P10 --> FinalGate" in flow
    assert "EarlyDispatch --> P06" in flow
    assert "early_clean2" not in flow
    assert "FinalGate.join: OR" in flow


def test_six_pages_no_early_gate_seven_has():
    assert "EarlyGate" not in build_generate_tasklist(_spec(6))[0]
    assert "EarlyGate" in build_generate_tasklist(_spec(7))[0]


def test_images_branch_and_notes_off():
    spec = _spec(3, images={"sources": ["ai"]},
                 production={"speaker_notes": False})
    tasks, flow = build_generate_tasklist(spec)
    assert "ImageAcquire" in tasks and "IconSync" in tasks
    assert "ImageReadiness" in tasks
    assert "NotesGen" not in tasks and "SplitNotes" not in tasks
    # notes 关 → export 命令带 --no-notes
    assert "--no-notes" in tasks["Export"]["command"]


def test_topic_source_uses_research_node():
    tasks, _ = build_generate_tasklist(_spec(2, source={"kind": "topic", "topic": "量子计算"}))
    assert "Research" in tasks and "Ingest" not in tasks


def test_roster_ids_must_be_exactly_p01_to_pNN():
    """页节点 P01..PNN 按序号反查 roster id：id 集合必须恰为 p01..pNN（顺序不敏感）。"""
    with pytest.raises(ValueError, match="roster"):
        build_generate_tasklist(_spec(2, roster=[{"id": "p01", "title": "a"},
                                                 {"id": "p03", "title": "b"}]))
    with pytest.raises(ValueError, match="roster"):
        build_generate_tasklist(_spec(2, roster=[{"id": "cover", "title": "a"},
                                                 {"id": "p02", "title": "b"}]))


def test_flow_parses_clean_without_deadlock():
    """flow 过 tickflow 解析、无未解死锁、无孤立节点（n=4 与 n=10 两种形状）。

    未解 AND-join 死锁 = Runner 构造即抛 DeadlockError（strict_deadlock）；
    孤立节点 = TasklistValidator 校验失败。
    """
    from tickflow import Registry, parse
    from tickflow.checker import check

    reg = Registry()
    for guard_name in ("early_issues", "early_clean", "final_errors", "final_clean"):
        reg.guard(guard_name, lambda view: True)

    for n in (4, 7, 10):
        tasks, flow = build_generate_tasklist(_spec(n))
        graph = parse(flow, registry=reg)
        assert check(graph) == []
        for key in tasks:
            assert key in flow  # 无孤立节点


def test_tl_generate_writes_envelope_and_returns_tasks(tmp_path, monkeypatch):
    from example.ppt_master.translator import tl_generate
    from module_harness.model.translator import _translator_view

    monkeypatch.chdir(tmp_path)  # output.dir 回填为相对路径 projects/t
    spec = _spec(2)
    # 翻译器视图：v.field("spec") 取 spec dict——框架 _translator_view 合成
    # 视图供数（DictView 是 positional bind，field() 抛 TypeError，不可用）
    out = tl_generate(_translator_view({"spec": spec}, "__translator__"))
    assert "Tasks" in out and "Flow" in out
    envelope = workspace.read_envelope()
    assert envelope["roster"][0]["id"] == "p01"
    assert (tmp_path / "projects" / "t").is_dir()
