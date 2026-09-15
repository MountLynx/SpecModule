"""generate 模板翻译器：spec → 信封 + 完整 tasklist（动态展开）。

页册先行：roster 静态展开为 N 个页节点（P01..P{nn}，运行时按 P01→p01
反查 roster id）；批1 = min(5, N) 过 EarlyGate（N ≤ 6 无早门，批2 页从
EarlyDispatch 扇出）；条件段按 spec 声明生成/省略。命令串静态，动态路径
经信封（workspace._envelope_path() 字面量嵌入）。守卫环：
FinalVerdict --|final_errors|--> Repair --> FinalGate（环上含守卫边，
满足 tickflow 环约束）。

FinalGate 必须声明 OR-join（flow 中 ``FinalGate.join: OR`` 行）：Repair
是门的**条件** producer，AND-join 要求全部 producer 槽位齐整——修复回边
只有在出错波次才产槽位，干净波次永不齐 → 门一次都点不了火（n > 6 时还
会被 tickflow checker 判 XOR-splitter 死锁，Runner 构造即抛
DeadlockError）。OR-join 在同步 tick 屏障下每"波"输入恰好点一次火：
同波页节点齐发共占一个 tick，修复回边单独成波——正是门语义。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import spec_schema, workspace

_RUN_TOOL = Path(__file__).resolve().parent / "tools" / "run_tool.py"


def _cmd(tool: str, *tool_args: str) -> str:
    args = " ".join(tool_args)
    suffix = f" -- {args}" if args else ""
    return (f'"{sys.executable}" "{_RUN_TOOL}" '
            f'--envelope "{workspace._envelope_path()}" --tool {tool}{suffix}')


def build_generate_tasklist(
    spec: dict[str, Any],
) -> tuple[dict[str, dict], str]:
    """返回 (Tasks dict, Flow str)。spec 须已过 validate_ppt_spec（含回填）。

    直调本函数的轻量路径（测试/嵌入者）可省缺省键：production.images 按
    schema 同款缺省兜底（speaker_notes=True / 无图像）；source.roster 必备。
    """
    roster = spec["roster"]
    n = len(roster)
    # 页节点名按序号生成（P01..PNN），page_node 运行时按 P01→p01 反查
    # roster——id 集合必须恰为 p01..pNN（schema 侧不知道 N，约束归翻译器）
    expected = {f"p{i:02d}" for i in range(1, n + 1)}
    actual = {p["id"] for p in roster}
    if actual != expected:
        raise ValueError(
            f"非法 spec: 'roster' 页 id 集合必须恰为 "
            f"['p01'..'p{n:02d}']（顺序不敏感），实际 {sorted(actual)}"
        )
    batch1 = min(5, n)
    notes_on = (spec.get("production") or {}).get("speaker_notes", True)
    img_sources = [s for s in (spec.get("images") or {}).get("sources") or []
                   if s != "none"]
    has_images = bool(img_sources)
    topic = spec["source"]["kind"] == "topic"
    early = n > 6
    gate1 = "EarlyGate" if early else "FinalGate"

    tasks: dict[str, dict] = {}
    flow: list[str] = []

    # ── 头段：源 → Init → Plan → PlanValidate ──
    if topic:
        tasks["Research"] = {"type": "script", "script": "research_node"}
        tasks["Init"] = {"type": "script", "script": "init",
                         "inputs": {"source": "Research"}}
        flow.append("[Research] --> Init")
        plan_src = "Research"
    else:
        tasks["Ingest"] = {"type": "script", "script": "ingest"}
        tasks["Init"] = {"type": "script", "script": "init",
                         "inputs": {"source": "Ingest"}}
        flow.append("[Ingest] --> Init")
        plan_src = "Ingest"
    tasks["Plan"] = {"type": "script", "script": "plan_node",
                     "inputs": {"source": plan_src}}
    tasks["PlanValidate"] = {"type": "script", "script": "plan_validate",
                             "inputs": {"plan": "Plan"}}
    flow.append("Init --> Plan")
    flow.append("Plan --> PlanValidate")

    # ── 资源段：IconSync → (ImageAcquire) → Calibrate ──
    tasks["IconSync"] = {"type": "script", "script": "icon_sync_node",
                         "inputs": {"plan": "Plan"}}
    flow.append("PlanValidate --> IconSync")
    prev = "IconSync"
    if has_images:
        tasks["ImageAcquire"] = {"type": "script", "script": "image_acquire",
                                 "inputs": {"plan": "Plan"}}
        flow.append(f"{prev} --> ImageAcquire")
        prev = "ImageAcquire"
    tasks["Calibrate"] = {"type": "script", "script": "calibrate"}
    flow.append(f"{prev} --> Calibrate")

    # ── 页段：批1（P01..P05）→ 门1；早门段守卫分流 ──
    for i in range(1, batch1 + 1):
        tasks[f"P{i:02d}"] = {"type": "script", "script": "page_node",
                              "inputs": {"plan": "Plan", "calibration": "Calibrate"}}
        flow.append(f"Calibrate --> P{i:02d}")
        flow.append(f"P{i:02d} --> {gate1}")

    if early:
        tasks["EarlyGate"] = {"type": "command",
                              "command": _cmd("svg_quality_checker.py",
                                              "--stage early --canonical-authoring --json"),
                              "timeout": 600.0}
        tasks["EarlyVerdict"] = {"type": "script", "script": "early_verdict",
                                 "inputs": {"gate": "EarlyGate"}}
        tasks["EarlyRepair"] = {"type": "script", "script": "repair_node",
                                "inputs": {"verdict": "EarlyVerdict",
                                           "calibration": "Calibrate"}}
        tasks["EarlyDispatch"] = {"type": "script", "script": "passthrough",
                                  "inputs": {"verdict": "EarlyVerdict"}}
        flow += ["EarlyGate --> EarlyVerdict",
                 "EarlyVerdict --|early_issues|--> EarlyRepair",
                 "EarlyRepair --> FinalGate",
                 "EarlyVerdict --|early_clean|--> EarlyDispatch"]
        batch2_src = "EarlyDispatch"
    else:
        batch2_src = None  # n ≤ 6：无批2

    # ── 终门 + 修复守卫环 + 汇出 ──
    tasks["FinalGate"] = {"type": "command",
                          "command": _cmd("svg_quality_checker.py",
                                          "--stage final --canonical-authoring --json"),
                          "timeout": 900.0}
    tasks["FinalVerdict"] = {"type": "script", "script": "final_verdict",
                             "inputs": {"gate": "FinalGate"}}
    tasks["Repair"] = {"type": "script", "script": "repair_node",
                       "inputs": {"verdict": "FinalVerdict",
                                  "calibration": "Calibrate"}}
    tasks["FinalDispatch"] = {"type": "script", "script": "passthrough",
                              "inputs": {"verdict": "FinalVerdict"}}
    flow += ["FinalGate.join: OR",
             "FinalGate --> FinalVerdict",
             "FinalVerdict --|final_errors|--> Repair",
             "Repair --> FinalGate",
             "FinalVerdict --|final_clean|--> FinalDispatch"]

    # 批2 页（仅 n > 6）：EarlyDispatch 扇出后进 FinalGate
    if early:
        for i in range(batch1 + 1, n + 1):
            tasks[f"P{i:02d}"] = {"type": "script", "script": "page_node",
                                  "inputs": {"plan": "Plan", "calibration": "Calibrate"}}
            flow.append(f"{batch2_src} --> P{i:02d}")
            flow.append(f"P{i:02d} --> FinalGate")

    # ── 尾段：readiness → notes → split → finalize → export → report ──
    prev = "FinalDispatch"
    if has_images:
        tasks["ImageReadiness"] = {"type": "script", "script": "image_readiness",
                                   "inputs": {"rows": "ImageAcquire"}}
        flow.append(f"{prev} --> ImageReadiness")
        prev = "ImageReadiness"
    if notes_on:
        tasks["NotesGen"] = {"type": "script", "script": "notes_node"}
        flow.append(f"{prev} --> NotesGen")
        prev = "NotesGen"
        tasks["SplitNotes"] = {"type": "command",
                               "command": _cmd("total_md_split.py"),
                               "timeout": 120.0}
        flow.append(f"{prev} --> SplitNotes")
        prev = "SplitNotes"
    tasks["Finalize"] = {"type": "command", "command": _cmd("finalize_svg.py"),
                         "timeout": 600.0}
    flow.append(f"{prev} --> Finalize")
    export_args = [] if notes_on else ["--no-notes"]
    tasks["Export"] = {"type": "command",
                       "command": _cmd("svg_to_pptx.py", *export_args),
                       "timeout": 1200.0}
    flow.append("Finalize --> Export")
    tasks["Report"] = {"type": "script", "script": "ppt_report",
                       "inputs": {"export": "Export", "finalize": "Finalize"}}
    flow.append("Export --> Report")

    return tasks, "\n".join(flow)


def tl_generate(view) -> dict[str, Any]:
    """模板翻译入口：校验并回填 spec → 初始化 workspace + 信封 → tasklist。"""
    spec = view.field("spec")
    spec_schema.validate_ppt_spec(spec)   # 原地校验 + 缺省回填（一次）
    root = workspace.init_workspace(spec["output"]["dir"])
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": spec["roster"],
        "sources": spec["source"].get("paths") or [],
        "spec": spec,
    })
    tasks, flow = build_generate_tasklist(spec)
    return {"Tasks": tasks, "Flow": flow}
