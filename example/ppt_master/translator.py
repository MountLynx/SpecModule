"""generate 模板翻译器：spec → 信封 + 完整 tasklist（动态展开）。

页册先行：roster 静态展开为 N 个页节点（P01..P{nn}，运行时按 P01→p01
反查 roster id）；批1 = min(5, N) 过 EarlyGate（N ≤ 6 无早门）；早门两
分支（干净/修复）都汇入 EarlyDispatch 后批2 扇出——EarlyRepair 若直连
FinalGate，issues 分支下 EarlyDispatch 永不点火，批2 页永不生成（缺半册
deck 静默导出，roadmap 遗留缺口已钉死回归）；条件段按 spec 声明生成/省略。
命令任务按框架约定
引**注册名**（字面串归注册方 module.py 的 CommandConfig：
run_tool.py --envelope <workspace._envelope_path()> --tool <tool> [-- args]；
同进程注册 → 同 pid → 信封路径一致）。命令名 → vendor 工具 + 参数 +
超时（module.py 据此构建 CommandConfig，六个名字缺一不可；注册缺失会在
TasklistValidator / 构图时点名 fail-fast）::

    ppt_early_gate      svg_quality_checker.py --stage early --canonical-authoring --json   600s
    ppt_final_gate      svg_quality_checker.py --stage final --canonical-authoring --json   900s
    ppt_split_notes     total_md_split.py                                                   120s
    ppt_finalize        finalize_svg.py                                                     600s
    ppt_export          svg_to_pptx.py                                                     1200s
    ppt_export_nonotes  svg_to_pptx.py --no-notes                                          1200s

守卫环：FinalVerdict --|final_errors|--> Repair --> FinalGate（环上含
守卫边，满足 tickflow 环约束）。

FinalGate 与 EarlyDispatch 必须声明 OR-join（flow 中 ``*.join: OR`` 行）：
二者的 producer 都是**条件**的——FinalGate 的 Repair 回边只在有错波出现，
EarlyDispatch 的两入边是 XOR 分支（每跑恰一）——AND-join 要求全部 producer
槽位齐整，条件槽位永不齐整即饿死（n > 6 时还会被 tickflow checker 判
XOR-splitter 死锁，Runner 构造即抛 DeadlockError）。OR-join 的"波"语义
（勿"修"回 AND——饿死；也非"每页到齐各发一次"）：同步 tick 屏障下同波
producer 齐发共占一个 tick，扇出的页节点天然同波，故门**单次点火即见全
册**；修复回边单独成波，每波恰点一次火。该语义由
test_or_join_gate_fires_once_with_full_deck（干净路径恰 1 次点火见全册，
及去 OR 改回 AND 后 0 次点火饿死的对照）与
test_or_join_repair_loop_fires_gate_once_per_wave（有错一轮：门 2 次、
Repair 1 次、Report 1 次）以假 body 引擎仿真动态钉死；早门两分支汇流后
批2 照常展开由 test_early_issues_branch_still_generates_batch2 钉死。
"""

from __future__ import annotations

from typing import Any

from . import spec_schema, workspace


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
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(
            f"非法 spec: 'roster' 页 id 集合必须恰为 "
            f"['p01'..'p{n:02d}']（顺序不敏感）"
            + (f"，缺失 {missing}" if missing else "")
            + (f"，多余 {extra}" if extra else "")
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
    # 注册名核实：files 源 ingest / topic 源 research_node（tools_nodes 与
    # llm_nodes 的注册名不对称，不可"补齐"命名）
    src_task, src_script = (
        ("Research", "research_node") if topic else ("Ingest", "ingest")
    )
    tasks[src_task] = {"type": "script", "script": src_script}
    tasks["Init"] = {"type": "script", "script": "init",
                     "inputs": {"source": src_task}}
    flow.append(f"[{src_task}] --> Init")
    tasks["Plan"] = {"type": "script", "script": "plan_node",
                     "inputs": {"source": src_task}}
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

    def _page_task() -> dict:
        """页任务 dict（批1/批2 两个循环共用，输入绑定一致）。"""
        return {"type": "script", "script": "page_node",
                "inputs": {"plan": "Plan", "calibration": "Calibrate"}}

    # ── 页段：批1（P01..P05）→ 门1；早门段守卫分流 ──
    for i in range(1, batch1 + 1):
        tasks[f"P{i:02d}"] = _page_task()
        flow.append(f"Calibrate --> P{i:02d}")
        flow.append(f"P{i:02d} --> {gate1}")

    if early:
        tasks["EarlyGate"] = {"type": "command", "command": "ppt_early_gate",
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
                 "EarlyVerdict --|early_clean|--> EarlyDispatch",
                 "EarlyRepair --> EarlyDispatch",
                 "EarlyDispatch.join: OR"]
        batch2_src = "EarlyDispatch"
    else:
        batch2_src = None  # n ≤ 6：无批2

    # ── 终门 + 修复守卫环 + 汇出 ──
    tasks["FinalGate"] = {"type": "command", "command": "ppt_final_gate",
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
            tasks[f"P{i:02d}"] = _page_task()
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
        tasks["SplitNotes"] = {"type": "command", "command": "ppt_split_notes",
                               "timeout": 120.0}
        flow.append(f"{prev} --> SplitNotes")
        prev = "SplitNotes"
    tasks["Finalize"] = {"type": "command", "command": "ppt_finalize",
                         "timeout": 600.0}
    flow.append(f"{prev} --> Finalize")
    tasks["Export"] = {"type": "command",
                       "command": "ppt_export" if notes_on else "ppt_export_nonotes",
                       "timeout": 1200.0}
    flow.append("Finalize --> Export")
    tasks["Report"] = {"type": "script", "script": "ppt_report",
                       "inputs": {"export": "Export", "finalize": "Finalize"}}
    flow.append("Export --> Report")

    return tasks, "\n".join(flow)


def tl_generate(view: Any) -> dict[str, Any]:
    """模板翻译入口：校验回填 spec → 展开 tasklist（花名册差集校验在此，
    schema 不知道 N）→ 初始化 workspace + 信封 → 返回。

    先展开后落盘：展开期 ValueError（如花名册缺页 id）fail-fast 于任何
    workspace 目录/信封写入之前，不残留空项目目录与陈旧 pid 信封。
    """
    spec = view.field("spec")
    spec_schema.validate_ppt_spec(spec)   # 原地校验 + 缺省回填（一次）
    tasks, flow = build_generate_tasklist(spec)
    root = workspace.init_workspace(spec["output"]["dir"])
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": spec["roster"],
        "sources": spec["source"].get("paths") or [],
        "spec": spec,
    })
    return {
        "Tasks": tasks,
        "Flow": flow,
        # 产物声明：最终交付物 = exports/ 下最新导出的 pptx（命名带时间戳，
        # 同项目多次运行积累——pick=latest 取 mtime 最新；output.dir 已回填）
        "Artifacts": [{
            "name": f"{spec['project']} 演示文稿",
            "kind": "deliverable",
            "pick": "latest",
            "path": f"{spec['output']['dir']}/exports/*.pptx",
        }],
    }
