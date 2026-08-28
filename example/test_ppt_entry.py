# example/test_ppt_entry.py
"""模块入口文件直接测试：discover_modules 发现 + build_registry 双模板注册。

放 modules/ 之外（pytest example/ 收集），避免被 discover_modules 扫描。
"""

from __future__ import annotations

from pathlib import Path

from module_harness.entry import discover_modules
from module_harness.events import EventBus

MODULES_DIR = Path(__file__).parent / "modules"
FAKE_LLM = object()   # 注册不触发 LLM 调用，仅作占位


def test_discover_ppt_writer():
    entries = discover_modules(MODULES_DIR)
    assert "ppt_writer" in entries
    entry = entries["ppt_writer"]
    assert entry.description
    assert entry.default_template == "ppt_render"
    assert set(entry.templates) == {"ppt_render", "template_review"}
    assert entry.default_spec is not None
    assert len(entry.default_spec["pages"]) == 3
    assert entry.review_harness is None


def test_build_registry_registers_both_templates_components():
    entry = discover_modules(MODULES_DIR)["ppt_writer"]
    reg = entry.build_registry(FAKE_LLM, "ppt_render", EventBus())
    assert reg.is_harness("template_opinion")
    assert reg.is_command("render_deck")
    for script in ("tl_ppt_render", "tl_template_review", "dump_draft",
                   "verify_draft", "report_reject", "register_template",
                   "ppt_report"):
        assert reg.is_script(script), f"缺 script: {script}"
    assert "has_issues" in reg.guard_names()
    assert "clean" in reg.guard_names()
