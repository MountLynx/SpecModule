"""项目目录契约 + 进程级信封（ppt_writer normalize 信封模式复用）。

command 节点命令串是静态的（框架约束）——动态路径（output_dir 等）写入
pid 修饰的临时信封 JSON，路径字面量嵌进命令串，工具入口 run_tool.py 读信封。
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
VENDOR_SCRIPTS = MODULE_DIR / "vendor" / "ppt_master" / "scripts"

_ENVELOPE_DIR = Path(tempfile.gettempdir())

WORKSPACE_DIRS = (
    "sources", "analysis", "images", "icons", "svg_output",
    "svg_final", "notes", "validation", "exports",
)


def _envelope_path() -> Path:
    return _ENVELOPE_DIR / f"specmodule_ppt_master_generate_{os.getpid()}.json"


def write_envelope(data: dict[str, Any]) -> Path:
    path = _envelope_path()
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def read_envelope() -> dict[str, Any]:
    path = _envelope_path()
    if not path.exists():
        raise RuntimeError(f"缺少运行信封 {path.name}：翻译器未先执行？")
    return json.loads(path.read_text(encoding="utf-8"))


def init_workspace(output_dir: str | Path) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    for d in WORKSPACE_DIRS:
        (root / d).mkdir(exist_ok=True)
    (root / "validation" / "workflow.log").touch(exist_ok=True)
    return root


def append_workflow_log(root: str | Path, detail: str) -> None:
    log = Path(root) / "validation" / "workflow.log"
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {detail}\n")


def gate_report_path(root: str | Path, stage: str) -> Path:
    if stage not in ("early", "final"):
        raise ValueError(f"未知 gate stage {stage!r}（仅接受 'early' / 'final'）")
    name = "svg_quality_early_report.json" if stage == "early" else "svg_quality_report.json"
    return Path(root) / "validation" / name
