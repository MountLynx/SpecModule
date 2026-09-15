"""确定性 script 节点：ingest / init / plan_validate / 门裁决 / readiness / report。

校验是 Gate 1/2 的机械化子集（spec §5）：页册保真、Audience move 存在、
锁锚点存在。schema 合法 ≠ 保真；保真以页册与必需要素为准，违反即
infrastructure Failure（硬合规 fail-fast）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tickflow import Failure

from . import workspace

_MD_EXTS = {".md", ".markdown", ".txt", ".csv", ".tsv"}

# gate 报告 blocking issue 的 file 是页 SVG 文件名（Task 1 实测锁定命名
# page_<页id>.svg）；项目级条目只有 scope/message，无 file。
_PAGE_FILE_RE = re.compile(r"^page_(.+)\.svg$", re.IGNORECASE)


def _normalize_issue(issue: dict) -> dict:
    """blocking issue 归一化：从 file 派生 page 键（repair 按页分组依赖）。

    真实形状（checker._provenance_categories）：{"file", "message"} 或
    {"scope", "message"}——无 page。派生值统一小写（页 id 约定小写）；
    派生不出 page 的条目原样保留（repair_node 会把它们收进收据 skipped，
    不静默丢弃）。
    """
    out = dict(issue)
    if "page" not in out:
        f = out.get("file")
        if f:
            m = _PAGE_FILE_RE.match(Path(str(f)).name)
            if m:
                out["page"] = m.group(1).lower()
    return out


def _blocking_issues(root: str | Path, stage: str) -> list[dict] | None:
    """读 gate 报告 categories.blocking.issues；报告缺失 → None。

    None = 无证据（checker 未落盘），与"报告在盘但 blocking 为空"是两回事，
    门路由据此区分。
    """
    report_file = workspace.gate_report_path(root, stage)
    if not report_file.exists():
        return None
    data = json.loads(report_file.read_text(encoding="utf-8"))
    return data.get("categories", {}).get("blocking", {}).get("issues", []) or []


def ingest(view) -> dict[str, Any]:
    """files → sources/ 拷贝 + 摘要（topic-only 由 Research 节点供摘要）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    sources = env.get("sources") or []
    digest_parts, copied = [], []
    for s in sources:
        p = Path(s)
        if not p.exists():
            digest_parts.append(f"### {p.name}（源缺失，契约驱动）")
            continue
        if p.suffix.lower() in _MD_EXTS:
            dest = root / "sources" / p.name
            shutil.copy2(p, dest)
            text = p.read_text(encoding="utf-8", errors="replace")
            digest_parts.append(f"### {p.name}\n{text[:6000]}")
            copied.append(str(dest))
        else:
            r = subprocess.run(
                [sys.executable, str(workspace.VENDOR_SCRIPTS / "source_to_md.py"), str(p),
                 "-o", str(root / "sources")],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
            )
            if r.returncode != 0:
                return {"status": "failed", "error": f"source_to_md 失败: {p.name}: {r.stderr[-500:]}"}
            digest_parts.append(f"### {p.name}（已转换，见 sources/）")
            copied.append(str(p))
    return {"status": "ok", "copied": copied,
            "digest": "\n".join(digest_parts)[:20000] or "（无源文件，仅契约）"}


def init(view) -> dict[str, Any]:
    env = workspace.read_envelope()
    workspace.init_workspace(env["output_dir"])
    workspace.append_workflow_log(env["output_dir"], "workspace 初始化（Init 节点）")
    return {"status": "ok", "output_dir": env["output_dir"]}


_AUDIENCE_MOVE = re.compile(r"Audience move", re.IGNORECASE)


def plan_validate(view) -> dict[str, Any]:
    """Gate 1/2 机械化：页册保真 + 每页 Audience move + 锁锚点。违反即停图。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    roster = env["roster"]
    plan = view.field("plan") or {}

    errors: list[str] = []
    if plan.get("roster_ids") != [p["id"] for p in roster]:
        errors.append(
            f"收据页册 {plan.get('roster_ids')} != spec 页册 {[p['id'] for p in roster]}")

    spec_md: str | None = None
    try:
        spec_md = (root / "design_spec.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append("design_spec.md 缺失（规划节点未落盘？）")
    lock_md: str | None = None
    try:
        lock_md = (root / "spec_lock.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append("spec_lock.md 缺失（规划节点未落盘？）")

    if spec_md is not None:
        for page in roster:
            block = re.search(
                rf"##\s+{re.escape(page['id'])}\b(.*?)(?=\n## |\Z)", spec_md, re.S)
            if block is None:
                errors.append(f"design_spec 缺页块 '{page['id']}'")
            elif not _AUDIENCE_MOVE.search(block.group(1)):
                errors.append(f"页 '{page['id']}' 缺 Audience move")
    if lock_md is not None:
        for anchor in ("palette", "typography"):
            if anchor not in lock_md:
                errors.append(f"spec_lock 缺锚点节 '{anchor}'")

    if errors:
        return Failure("规划校验失败（硬合规）:\n" + "\n".join(errors),
                       type="infrastructure")
    return {"status": "ok"}


def make_gate_verdict(stage: str):
    """门裁决 script：读 validation 报告 → 结构化 verdict（guard 消费）。

    ok = checker exit 0 **且** 无 blocking issues——rc≠0 时报告再干净也不
    是 ok（checker 在落盘前可 sys.exit，收据必须如实记 rc）。issues 归一化
    出 page 键（repair 按页分组）；全局性条目无 page，由 repair 收进 skipped。
    """

    def verdict(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        gate = view.field("gate") or {}
        raw = _blocking_issues(env["output_dir"], stage)
        issues = [_normalize_issue(i) for i in (raw or []) if isinstance(i, dict)]
        returncode = gate.get("returncode")
        return {"stage": stage, "returncode": returncode,
                "ok": returncode == 0 and not issues, "issues": issues}

    return verdict


def make_guard(stage: str, want_clean: bool):
    """守卫工厂：读源任务 inputs 键 'gate' 的输出（门 command 收据）并复检报告。

    放行条件 = checker exit 0 且报告在盘且无 blocking——无证据 ≠ 干净
    （checker 有落盘前的 sys.exit 路径，崩溃的门不得静默放行未验证的稿）。
    循环安全：crash 路由进 Repair 消耗修复轮，轮上限触发 infrastructure
    Failure 停图 → 崩溃必然 loud abort，不会静默循环。
    """
    def guard(view) -> bool:
        env = workspace.read_envelope()
        rc = (view.field("gate") or {}).get("returncode")
        # 无证据 ≠ 干净：checker 崩溃（rc≠0 / 报告未落盘）不得放行
        issues = _blocking_issues(env["output_dir"], stage)
        has_issues = True if (rc != 0 or issues is None) else bool(issues)
        return (not has_issues) if want_clean else has_issues
    return guard


def calibrate(view) -> dict[str, Any]:
    """text_measure calibrate → validation/text_calibration.json（全体页节点共享）。

    软降级是有意的：产物缺失时收据记 empty、页节点拿 "{}"（vendor 估宽
    自带保守默认），stderr 全文进收据供审计——降级可见，不静默。
    """
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    r = subprocess.run(
        [sys.executable, str(workspace.VENDOR_SCRIPTS / "text_measure.py"),
         "calibrate", str(root), "--outline"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    cal_file = root / "validation" / "text_calibration.json"
    return {"status": "ok" if cal_file.exists() else "empty",
            "returncode": r.returncode,
            "stderr": r.stderr[-500:],
            "calibration": cal_file.read_text(encoding="utf-8") if cal_file.exists() else "{}"}


def icon_sync_node(view) -> dict[str, Any]:
    """图标池物化（池来自 Plan 收据；空池 no-op）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    pool = (view.field("plan") or {}).get("icon_pool") or []
    if not pool:
        return {"status": "ok", "synced": 0}
    r = subprocess.run(
        [sys.executable, str(workspace.VENDOR_SCRIPTS / "icon_sync.py"), str(root), *pool],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if r.returncode != 0:
        return Failure(f"icon_sync 失败: {r.stderr[-500:]}", type="llm")
    return {"status": "ok", "synced": len(pool)}


def image_readiness(view) -> dict[str, Any]:
    """Needs-Manual 行必须已有真实文件，否则 fail-fast 列名（Step 7 readiness 门）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    rows = (view.field("rows") or {}).get("rows") or []
    missing = []
    for row in rows:
        if row.get("status") == "Needs-Manual":
            f = row.get("file")
            if not f or not (root / "images" / Path(f).name).exists():
                missing.append(row.get("name") or f or row.get("id", "?"))
    if missing:
        return Failure("图像就绪门：以下行缺文件：" + ", ".join(missing),
                       type="infrastructure")
    return {"status": "ready"}


def report(view) -> dict[str, Any]:
    export = view.field("export") or {}
    finalize = view.field("finalize") or {}
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    pptx_files = sorted(str(p) for p in (root / "exports").glob("*.pptx"))
    ok = export.get("returncode") == 0 and bool(pptx_files)
    return {"status": "ok" if ok else "error",
            "pptx": pptx_files, "export_returncode": export.get("returncode"),
            "finalize_returncode": finalize.get("returncode"),
            "message": "导出完成" if ok else "导出失败（见 exports/ 与 validation/）"}


def passthrough(view) -> dict[str, Any]:
    """扇出中转（tickflow 一条守卫边只写一个槽，扇出须经中转节点平边展开）。"""
    return {"status": "ok"}
