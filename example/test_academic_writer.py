# example/test_academic_writer.py
"""模块入口文件直接测试：discover_modules 发现 + build_registry 适配。

入口文件（example/modules/academic_writer.py）无运行级测试、仅在 CLI 冒烟时
首次执行——这里直接锁死发现与注册契约，堵上该覆盖空白。
放 modules/ 之外（pytest example/ 收集），避免被 discover_modules 扫描。
"""

from __future__ import annotations

from pathlib import Path

from module_harness.entry import discover_modules
from module_harness.events import EventBus

MODULES_DIR = Path(__file__).parent / "modules"
FAKE_LLM = object()   # 注册不触发 LLM 调用，仅作占位


def test_discover_academic_writer():
    entries = discover_modules(MODULES_DIR)
    assert "academic_writer" in entries
    entry = entries["academic_writer"]
    assert entry.description
    assert entry.default_template == "academic_writer"
    assert "academic_writer" in entry.templates
    assert "academic_writer_detailed" in entry.templates
    assert set(entry.submodules) == {"fact_review_loop"}
    assert entry.review_harness is None


def test_build_registry_submodule_mode():
    entry = discover_modules(MODULES_DIR)["academic_writer"]
    reg = entry.build_registry(FAKE_LLM, "academic_writer", EventBus())
    for harness in ("organize", "polish", "finalize"):
        assert reg.is_harness(harness)


def test_build_registry_detailed_mode():
    entry = discover_modules(MODULES_DIR)["academic_writer"]
    reg = entry.build_registry(FAKE_LLM, "academic_writer_detailed", EventBus())
    # 详细模式额外内联 loop harness
    for harness in ("seed_draft", "fact_review", "fix_issues"):
        assert reg.is_harness(harness)