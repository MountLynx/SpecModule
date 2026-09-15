# example/test_ppt_master_vendor.py
"""vendor 闭包完整性：入口脚本可被 python 语法加载（AST 导入名均可在
vendor 内、标准库、或显式允许的第三方清单中解析）；attribution_guard
行为已核对。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

VENDOR_SCRIPTS = (
    Path(__file__).parent / "ppt_master" / "vendor" / "ppt_master" / "scripts"
)

ENTRY_SCRIPTS = [
    "svg_quality_checker.py", "finalize_svg.py", "svg_to_pptx.py",
    "text_measure.py", "icon_sync.py", "image_gen.py", "image_search.py",
    "slice_images.py", "analyze_images.py", "source_to_md.py",
    "total_md_split.py",
]

# 11 个入口脚本直接绝对导入的第三方顶层名（AST 审计所得，就这两个之外的
# 一律视为缺依赖）。不做 find_spec 兜底：site-packages 里恰好装了同名包
# 会掩盖 vendor 缺文件。
_THIRD_PARTY_ALLOWED = {"PIL", "requests"}

# 有意独立于 vendor 源码的见证值：不引用 attribution_guard 的常量，
# 否则"校验逻辑未动"的断言就成了同义反复。
_UPSTREAM_LICENSE_DIGEST = (
    "80cefc234c1ec12a8cece4344f16300c634fa03df7891686fcf979e3828f0921"
)


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # 只统计绝对导入（level == 0）；相对导入在所属子包内解析，
            # 不应按顶层名参与闭包判定。
            if node.level == 0:
                names.add(node.module.split(".")[0])
    return names


def _vendor_local_names() -> set[str]:
    """vendor 内可解析的顶层名。

    除 scripts 根的平铺模块/子包外，还含各子包一层内的 .py 模块名——
    入口脚本运行时会把自己的子包目录插入 sys.path 再做绝对导入
    （如 source_to_md.py 导入 source_to_md/_batch.py），闭包判定须与
    该运行时解析一致。
    """
    local = {p.stem for p in VENDOR_SCRIPTS.glob("*.py")}
    entries = list(VENDOR_SCRIPTS.iterdir())
    local |= {p.name for p in entries if p.is_dir()}
    for pkg in entries:
        if pkg.is_dir():
            local |= {p.stem for p in pkg.glob("*.py")}
    return local


def test_entry_scripts_exist():
    for name in ENTRY_SCRIPTS:
        assert (VENDOR_SCRIPTS / name).is_file(), name


def test_entry_scripts_parse_and_imports_resolvable():
    local = _vendor_local_names()
    for name in ENTRY_SCRIPTS:
        imports = _top_level_imports(VENDOR_SCRIPTS / name)
        missing = {
            m for m in imports
            if m not in local and m not in sys.stdlib_module_names
            and m not in _THIRD_PARTY_ALLOWED
        }
        assert not missing, f"{name} 缺依赖: {sorted(missing)}"


def test_attribution_guard_vendor_relaxation(monkeypatch):
    """attribution_guard 在 vendored 闭包（NOTICE 标记）下不阻断；
    校验逻辑本身（digest 常量等）保持上游原样。"""
    vendor_root = VENDOR_SCRIPTS.parent
    assert (vendor_root / "NOTICE").is_file(), "vendored 标记 NOTICE 缺失"
    assert (vendor_root / "LICENSE").is_file()
    monkeypatch.syspath_prepend(str(VENDOR_SCRIPTS))
    import attribution_guard

    assert attribution_guard.is_vendored_copy() is True
    # 不抛 SystemExit 即为通过
    attribution_guard.require_skill_integrity()
    guard_src = (VENDOR_SCRIPTS / "attribution_guard.py").read_text(
        encoding="utf-8"
    )
    assert '"Copyright (c) 2025-2026 Hugo He"' in guard_src
    assert _UPSTREAM_LICENSE_DIGEST in guard_src
