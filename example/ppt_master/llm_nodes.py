"""LLM 节点工厂：call_harness 包裹 → 注册为 script。

为什么是 script 不是裸 harness：tickflow 中 body 返回 Failure 会让下游
AND join 永不点火（饥饿）。页/规划节点必须"永不 Failure"——LLM 错误降级
为失败收据 dict，由质量门/校验节点判定。工厂捕获 llm_client（注册期闭包），
避免模块级全局（多 Module 同进程 namespace 隔离，架构规则 4）。

页 SVG 落盘命名 = ``svg_output/page_<页id>.svg``（Task 1 四件套实测锁定，
checker→finalize→export 全链已验证），统一走 :func:`_page_svg_path`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tickflow import Failure

from module_harness.core.call import HarnessCallError, call_harness

from . import prompts_config as pc
from . import workspace


def _write_text(path: Path | str, text: str) -> None:
    """落盘文本，父目录自动建立（sources/notes 等子目录可能尚未创建）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _page_svg_path(root: Path, page_id: str) -> Path:
    """页 SVG 路径约定（Task 1 四件套实测锁定）：svg_output/page_<页id>.svg。"""
    return root / "svg_output" / f"page_{page_id}.svg"


def _node_name(view: Any) -> str:
    """视图节点裸名（末段），page id 派生与 repair stage 判定共用此一约定。

    运行时证据：引擎传入的 ``view.node`` 是**图节点 key**（tasklist 里的裸
    task 名）——`{module_id}:{key}` 前缀只加在 registry body 名上
    （graph_builder.py:88-90 只改 ``graph.nodes[key].body``），且 DSL key 语法
    不含 ``:``（translator.py:22/145）。末段剥离纯作防御（嵌入者合成视图等
    非常规来源），并保证两处派生永不彼此漂移。
    """
    return str(view.node).split(":")[-1]


def make_plan_node(llm_client: Any, event_bus: Any = None) -> Any:
    """规划节点：契约+源事实 → design_spec.md + spec_lock.md + 收据。"""

    async def plan_node(view: Any) -> dict[str, Any]:
        env = workspace.read_envelope()
        try:
            result = await call_harness(
                pc.plan_config(),
                {
                    "contract": json.dumps(env.get("spec", {}).get("contract", {}), ensure_ascii=False),
                    "roster": json.dumps(env["roster"], ensure_ascii=False),
                    "source_digest": json.dumps(view.field("source"), ensure_ascii=False),
                },
                llm_client=llm_client,
                prompt_extra=pc.plan_prompt_pack(),
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "error": str(e)}
        receipt = result.value  # json_object 配置经 OutputFormat.validate 已是解析后的 dict
        root = Path(env["output_dir"])
        _write_text(root / "design_spec.md", receipt.get("design_spec_md", ""))
        _write_text(root / "spec_lock.md", receipt.get("spec_lock_md", ""))
        return receipt

    return plan_node


def make_research_node(llm_client: Any, event_bus: Any = None) -> Any:
    """研究节点（topic-only）：缺口研究 → sources/research.md + facts。"""

    async def research_node(view: Any) -> dict[str, Any]:
        env = workspace.read_envelope()
        try:
            result = await call_harness(
                pc.research_config(),
                {
                    "topic": env.get("spec", {}).get("source", {}).get("topic", ""),
                    "gaps": "（topic-only：整题研究）",
                },
                llm_client=llm_client,
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "error": str(e)}
        val = result.value  # json_object 配置经 OutputFormat.validate 已是解析后的 dict
        root = Path(env["output_dir"])
        _write_text(root / "sources" / "research.md", val.get("research_md", ""))
        _write_text(root / "sources" / "facts.json", json.dumps(val.get("facts", []), ensure_ascii=False))
        return {"status": "ok", "digest": val.get("research_md", "")[:4000], "topic_researched": True}

    return research_node


def make_page_node(llm_client: Any, event_bus: Any = None) -> Any:
    """页节点：节点名即页 id（P01→p01），输出 SVG 落盘 svg_output/。"""

    async def page_node(view: Any) -> dict[str, Any]:
        env = workspace.read_envelope()
        page_id = _node_name(view).lower()  # "P01" -> "p01"
        page = next((p for p in env["roster"] if p["id"] == page_id), None)
        if page is None:
            return {"status": "failed", "error": f"页册中无 id '{page_id}'"}
        try:
            result = await call_harness(
                pc.page_config(),
                {
                    "page": json.dumps(page, ensure_ascii=False),
                    "lock": (Path(env["output_dir"]) / "spec_lock.md").read_text(encoding="utf-8"),
                    "calibration": json.dumps(view.field("calibration"), ensure_ascii=False),
                },
                llm_client=llm_client,
                prompt_extra=pc.page_prompt_pack(),
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "page": page_id, "error": str(e)}
        svg = result.value
        if not isinstance(svg, str) or "<svg" not in svg:
            return {"status": "failed", "page": page_id, "error": "输出非 SVG"}
        out = _page_svg_path(Path(env["output_dir"]), page_id)
        _write_text(out, svg)
        return {"status": "ok", "page": page_id, "file": str(out)}

    return page_node


def make_repair_node(llm_client: Any, event_bus: Any = None, max_rounds: int = 2) -> Any:
    """修复节点（Early/Final 共用，按 view.node 区分 stage）。

    轮次上限：validation/repair_rounds.json 计数（{early: n, final: n}），
    超限 → Failure(infrastructure) 停图（非交互环境无人工兜底）。
    """

    async def repair_node(view: Any) -> dict[str, Any]:
        # 与 page_node 同一裸名约定：前缀（如合成视图的 "mod:EarlyRepair"）
        # 不得把 Early 修复轮误记进 final 预算（反之亦然）
        stage = "early" if _node_name(view).startswith("Early") else "final"
        env = workspace.read_envelope()
        root = Path(env["output_dir"])
        rounds_file = root / "validation" / "repair_rounds.json"
        rounds: dict = {}
        if rounds_file.exists():
            rounds = json.loads(rounds_file.read_text(encoding="utf-8"))
        rounds[stage] = rounds.get(stage, 0) + 1
        rounds_file.write_text(json.dumps(rounds), encoding="utf-8")
        if rounds[stage] > max_rounds:
            return Failure(
                f"{stage} 修复轮超过上限 {max_rounds}", type="infrastructure"
            )

        verdict = view.field("verdict")
        all_issues = verdict.get("issues", [])
        # 全局性条目（无 page 归属）不静默丢弃——收进收据 skipped 供审计
        skipped = [i for i in all_issues if not i.get("page")]
        by_page: dict[str, list] = {}
        for i in all_issues:
            if i.get("page"):
                by_page.setdefault(i["page"], []).append(i)
        repaired, failed = [], []
        for page_id, page_issues in by_page.items():
            page = next((p for p in env["roster"] if p["id"] == page_id), None)
            if page is None:
                failed.append({"page": page_id, "error": "页册无此 id"})
                continue
            try:
                result = await call_harness(
                    pc.repair_config(),
                    {
                        "page": json.dumps(page, ensure_ascii=False),
                        "lock": (root / "spec_lock.md").read_text(encoding="utf-8"),
                        "calibration": json.dumps(view.field("calibration"), ensure_ascii=False),
                        "issues": json.dumps(page_issues, ensure_ascii=False),
                    },
                    llm_client=llm_client,
                    prompt_extra=pc.repair_prompt_pack(),
                    event_bus=event_bus,
                )
            except HarnessCallError as e:
                failed.append({"page": page_id, "error": str(e)})
                continue
            svg = result.value
            if not isinstance(svg, str) or "<svg" not in svg:
                failed.append({"page": page_id, "error": "修复输出非 SVG"})
                continue
            _write_text(_page_svg_path(root, page_id), svg)
            repaired.append(page_id)
        return {"status": "ok", "stage": stage, "round": rounds[stage],
                "repaired": repaired, "failed": failed, "skipped": skipped}

    return repair_node


def make_notes_node(llm_client: Any, event_bus: Any = None) -> Any:
    """备注节点：最终页 SVG → notes/total.md。"""

    async def notes_node(view: Any) -> dict[str, Any]:
        env = workspace.read_envelope()
        root = Path(env["output_dir"])
        digest = []
        for p in env["roster"]:
            f = _page_svg_path(root, p["id"])
            if f.exists():
                digest.append({"id": p["id"], "svg_head": f.read_text(encoding="utf-8")[:1200]})
            else:
                # 缺页自描述——LLM 面前的摘要不能伪装成"空页"
                digest.append({"id": p["id"], "missing": True})
        try:
            result = await call_harness(
                pc.notes_config(),
                {
                    "roster": json.dumps(env["roster"], ensure_ascii=False),
                    "pages_digest": json.dumps(digest, ensure_ascii=False),
                },
                llm_client=llm_client,
                prompt_extra=pc.notes_prompt_pack(),
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "error": str(e)}
        _write_text(root / "notes" / "total.md", result.value)
        return {"status": "ok", "file": str(root / "notes" / "total.md")}

    return notes_node


def make_image_node(llm_client: Any, event_bus: Any = None) -> Any:
    """AI 图像行获取：§VIII 行 prompt → harness 图像模式 → images/。

    图像 harness 失败（infrastructure Failure）在 call_harness 侧抛
    HarnessCallError → 行标 failed（Needs-Manual 语义），不中断运行。
    """

    async def image_node(view: Any) -> dict[str, Any]:
        env = workspace.read_envelope()
        root = Path(env["output_dir"])
        rows = (view.field("plan") or {}).get("image_rows") or []
        out_rows = []
        for row in rows:
            if row.get("acquire") != "ai":
                out_rows.append(row)
                continue
            try:
                result = await call_harness(
                    pc.image_config(image_dir=str(root / "images")),
                    {"image_prompt": row.get("prompt", "")},
                    llm_client=llm_client,
                    event_bus=event_bus,
                )
                row = {**row, "status": "terminal", "file": result.value}
            except HarnessCallError as e:
                row = {**row, "status": "Needs-Manual", "error": str(e)}
            out_rows.append(row)
        return {"status": "ok", "rows": out_rows}

    return image_node
