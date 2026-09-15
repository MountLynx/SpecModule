"""翻译器展开规则：页节点数、批次门、条件段、≤6 无早门、命令串形状。"""

from __future__ import annotations

import re

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
    # notes 缺省开 → export 引带备注的注册名
    assert tasks["Export"]["command"] == "ppt_export"
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
    # 钉死单一干净路径形状（计划 Step 4 注：干净出边只有一条，无二段守卫）
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
    # notes 关 → export 引无备注的注册名
    assert tasks["Export"]["command"] == "ppt_export_nonotes"


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


def _command_table() -> dict[str, float]:
    """解析 translator.py 模块 docstring 的命令表 → {命令名: 超时秒}。

    表是注册方（module.py）构建 CommandConfig 的唯一契约文本；此解析器
    让"任务引用 ↔ 表"漂移可测（Task 8 可同法扩展断言工具/参数列）。
    """
    from example.ppt_master import translator

    table: dict[str, float] = {}
    for line in (translator.__doc__ or "").splitlines():
        m = re.fullmatch(r"\s+(ppt_\w+)\s+\S.*?\s+(\d+)s\s*", line)
        if m:
            table[m.group(1)] = float(m.group(2))
    assert len(table) >= 6, f"docstring 命令表解析不足 6 行（表格式变了？）: {table}"
    return table


def test_command_tasks_match_docstring_table():
    """生成任务的命令名/超时必须与 docstring 命令表逐项一致（防漂移）。"""
    table = _command_table()
    covered: set[str] = set()
    for spec in (_spec(10),  # 早门 + 终门 + notes 全链
                 _spec(3, production={"speaker_notes": False})):  # nonotes 分叉
        tasks, _ = build_generate_tasklist(spec)
        for task in tasks.values():
            if task["type"] != "command":
                continue
            name = task["command"]
            assert name in table, f"命令 '{name}' 不在 docstring 命令表"
            assert task["timeout"] == table[name], \
                f"命令 '{name}' 超时 {task['timeout']} != 表 {table[name]}"
            covered.add(name)
    assert covered == set(table), f"表中有未被任务覆盖的命令: {set(table) - covered}"


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
            assert key in graph.nodes  # 无孤立节点（图节点级，非子串匹配）


def _simulate(flow: str, page_keys: list[str], gate_errors: list[bool]) -> dict:
    """假 body 引擎仿真：真实 flow 原样过 tickflow Runner，钉死门"波"语义。

    页 body 把自身节点名记入"已落盘"集合；门 body 记录每次点火时在盘的页
    集合；gate_errors 队列控制逐轮终检裁决（True=有错→Repair 回边；耗尽
    后全程干净）。返回点火统计（gate_seen / fired / repair）。
    """
    from tickflow import Registry, Runner, parse

    state: dict = {"pages_done": set(), "gate_seen": [], "repair": 0,
                   "verdicts": list(gate_errors), "last": False}
    reg = Registry()
    reg.guard("early_issues", lambda view: state["last"])
    reg.guard("early_clean", lambda view: not state["last"])
    reg.guard("final_errors", lambda view: state["last"])
    reg.guard("final_clean", lambda view: not state["last"])

    def body_page(view):
        state["pages_done"].add(view.node)  # 页 SVG 视为已落盘
        return {}

    def body_gate(view):
        state["gate_seen"].append(sorted(state["pages_done"]))
        return {}

    def body_verdict(view):
        state["last"] = bool(state["verdicts"].pop(0)) if state["verdicts"] else False
        return {}

    def body_repair(view):
        state["repair"] += 1
        return {}

    def body_noop(view):
        return {}

    reg.body("sim_page", body_page)
    reg.body("sim_gate", body_gate)
    reg.body("sim_verdict", body_verdict)
    reg.body("sim_repair", body_repair)
    reg.body("sim_noop", body_noop)

    graph = parse(flow, registry=reg)
    for key, node in graph.nodes.items():
        if key in page_keys:
            node.body = "sim_page"
        elif key == "FinalGate":
            node.body = "sim_gate"
        elif key == "FinalVerdict":
            node.body = "sim_verdict"
        elif key == "Repair":
            node.body = "sim_repair"
        else:
            node.body = "sim_noop"

    runner = Runner(graph, registry=reg)
    seen = runner.run_until_idle(max_ticks=30)
    state["fired"] = [f.node for f in seen]
    return state


def test_or_join_gate_fires_once_with_full_deck():
    """OR 门"波"语义钉死（勿改回 AND）：n=3 干净路径下扇出页同 tick 齐发
    （同步 tick 屏障），FinalGate 恰 1 次点火且见全册；Report 到达、Repair
    未点火。对照：去 OR 改回 AND → 门 0 次点火、图停滞（条件 producer
    Repair 槽位永不齐整）。"""
    _, flow = build_generate_tasklist(_spec(3))
    page_keys = ["P01", "P02", "P03"]

    sim = _simulate(flow, page_keys, gate_errors=[])
    assert sim["gate_seen"] == [["P01", "P02", "P03"]]
    assert "Repair" not in sim["fired"]
    assert sim["fired"][-1] == "Report"

    sim_and = _simulate(flow.replace("FinalGate.join: OR\n", ""), page_keys, [])
    assert sim_and["gate_seen"] == []            # AND 门一次都点不了火
    assert "Report" not in sim_and["fired"]      # 图在页段后停滞


def test_or_join_repair_loop_fires_gate_once_per_wave():
    """修复回边单独成波：一轮有错 → FinalGate 恰 2 次点火（初检 + 复检，
    非逐页多发）、Repair 1 次、Report 恰 1 次，复检仍见全册。"""
    _, flow = build_generate_tasklist(_spec(3))
    sim = _simulate(flow, ["P01", "P02", "P03"], gate_errors=[True])
    assert len(sim["gate_seen"]) == 2
    assert sim["gate_seen"][1] == ["P01", "P02", "P03"]
    assert sim["fired"].count("Repair") == 1
    assert sim["fired"].count("Report") == 1


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


def test_tl_generate_failfast_leaves_no_workspace(tmp_path, monkeypatch):
    """先展开后落盘：花名册差集 ValueError（schema 抓不到、只在展开期抛）
    必须发生在任何 workspace 目录 / pid 信封写入之前——失败零残留。"""
    from example.ppt_master.translator import tl_generate
    from module_harness.model.translator import _translator_view

    monkeypatch.chdir(tmp_path)
    spec = _spec(2, roster=[{"id": "p01", "title": "a"},
                            {"id": "p03", "title": "b"}])
    with pytest.raises(ValueError, match="roster"):
        tl_generate(_translator_view({"spec": spec}, "__translator__"))
    assert not (tmp_path / "projects").exists()
    assert not list(tmp_path.glob("specmodule_ppt_master_generate_*.json"))
