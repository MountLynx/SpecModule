# module_harness/infra/artifacts.py
"""run 产物收集与清单——artifacts.json（跨进程读的声明制产物通道）。

模块作者经 tasklist 顶层 ``Artifacts`` 声明产出（``ArtifactDecl``：name/kind/
path/pick）；Module 终态（done/truncated）收尾时收集——glob 展开 → 绝对路径
+ size/mtime 落 ``<run_dir>/artifacts.json``，文件本体不搬。cancelled/aborted
不收集（部分产物不保证）。清单是消费端唯一下载依据：客户端只按 index 引用
条目，路径永不为客户端输入（无遍历面）。
"""

from __future__ import annotations

import glob
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..model.spec import ArtifactDecl

log = logging.getLogger(__name__)

__all__ = ["artifacts_path", "collect_artifacts", "write_artifacts_manifest"]


def artifacts_path(module_id: str, base_dir: Path | None = None) -> Path:
    """``<base_dir>/.specmodule/runs/<module_id>/artifacts.json``。"""
    return (base_dir or Path.cwd()) / ".specmodule" / "runs" / module_id / "artifacts.json"


def collect_artifacts(decls: list["ArtifactDecl"]) -> list[dict]:
    """声明 → 清单条目（glob 展开，绝对路径 + size/mtime）。

    pick="latest" 在该声明的匹配集内取 mtime 最新一个；pick="all" 按 path
    排序全收；目录命中跳过（v1 只收文件）。零匹配的声明跳过——清单只含
    真实存在的文件（零匹配整体仍写空清单，见 write_artifacts_manifest）。

    标注经 ``from __future__ import annotations`` 字符串化，ArtifactDecl
    仅运行期类型引用（TYPE_CHECKING 导入）——避免与 model.spec 导入环
    （model.module 会导入本模块）。
    """
    entries: list[dict] = []
    for decl in decls:
        matches = [p for p in glob.glob(decl.path, recursive=True)
                   if os.path.isfile(p)]
        if not matches:
            continue
        if decl.pick == "latest":
            matches = [max(matches, key=os.path.getmtime)]
        else:
            matches = sorted(matches)
        for m in matches:
            abs_path = os.path.abspath(m)
            st = os.stat(abs_path)
            entries.append({
                "name": decl.name,
                "kind": decl.kind,
                "path": abs_path,
                "size": st.st_size,
                "modified": datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds"),
            })
    return entries


def write_artifacts_manifest(
    module_id: str, decls: list["ArtifactDecl"], base_dir: Path | None = None
) -> None:
    """终态收集 → artifacts.json 原子写（tmp + os.replace，同 status.json）。

    run 目录不存在（纯内存模式）不落盘；无声明不产文件；声明存在但零匹配
    → 写空清单。失败仅 log 不阻断运行（对齐 _write_phase 哲学）。
    """
    if not decls:
        return
    path = artifacts_path(module_id, base_dir)
    if not path.parent.exists():
        return
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(
                {"run_id": module_id, "artifacts": collect_artifacts(decls)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        log.exception("写 artifacts.json 失败（不阻断运行）: %s", path)
