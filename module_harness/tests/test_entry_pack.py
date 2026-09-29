# module_harness/tests/test_entry_pack.py
"""entry → pack 物化共享层：组件提取/别名行/submodule 打包/错误契约。"""

from __future__ import annotations

import pytest

from module_harness.cli.entry import discover_modules
from module_harness.cli.loader import ModuleLoader
from module_harness.infra.entry_pack import entry_to_pack
from module_harness.infra.store import validate_pack_dir

ENTRY_PY = '''
from module_harness.cli.entry import ModuleEntry
from module_harness.core.config import HarnessConfig
from module_harness.core.registry import HarnessRegistry
from module_harness.infra.events import EventBus
from module_harness.model.spec import SpecSchema, Tasklist, TaskDefinition
from module_harness.model.submodule import SubModule, script

GREET = HarnessConfig.from_dict(
    {"name": "greet_h", "prompt_core": "你好 {name}", "temperature": 0.1})


def _registry(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())
    reg.harness("greet_h", GREET)
    reg.script("shout")(_shout_impl)
    reg.guard("is_ok")(_is_ok_impl)
    return reg


def _shout_impl(view):
    return {"text": str(view.field("text")).upper()}


def _is_ok_impl(view):
    return True


class EchoSub(SubModule):
    name = "echo_sub"
    description = "回声子模块"
    spec_schema = SpecSchema(input={"text": "str"}, output={"echo": "str"})
    harnesses = [GREET]
    tasklist = Tasklist(
        tasks={"Echo": TaskDefinition(type="harness", harness="greet_h",
                                      inputs={"name": "{spec.text}"})},
        flow="[Echo]",
    )

    @script("echo_back")
    def echo_back(view):
        return {"echo": "done"}


TASKLIST = {
    "Tasks": {
        "Greet": {"type": "harness", "harness": "greet_h",
                  "inputs": {"name": "{spec.name}"}},
        "Shout": {"type": "script", "script": "shout", "inputs": {"text": "Greet"}},
        "Echo": {"type": "submodule", "submodule": "echo_sub",
                 "inputs": {"text": "Shout"}},
    },
    "Flow": "[Greet] --|is_ok|--> Shout --> Echo",
}

entry = ModuleEntry(
    name="hello_entry",
    description="测试 entry 模块",
    templates={"hello_entry": {"name": "hello_entry", "tasklist": TASKLIST}},
    submodules={"echo_sub": EchoSub},
    build_registry=_registry,
    default_template="hello_entry",
    default_spec={"name": "world"},
    spec_schema={"name": "str"},
    review_harness=None,
)
'''


@pytest.fixture
def entry(tmp_path):
    d = tmp_path / "modules"
    d.mkdir()
    (d / "hello_entry.py").write_text(ENTRY_PY, encoding="utf-8")
    return discover_modules(d)["hello_entry"]


def _pack(entry_obj, tmp_path, **kw):
    return entry_to_pack(entry_obj, out_dir=tmp_path / "packout", **kw)


class TestEntryToPack:
    def test_materializes_self_contained_pack(self, entry, tmp_path):
        result = _pack(entry, tmp_path)
        pack = result.pack_dir
        manifest = validate_pack_dir(pack)
        assert manifest["name"] == "hello_entry"
        assert manifest["tasklist"]["Flow"] == "[Greet] --|is_ok|--> Shout --> Echo"
        assert (pack / "harnesses" / "greet_h.json").is_file()
        scripts = (pack / "scripts" / "shout.py").read_text(encoding="utf-8")
        assert "shout = _shout_impl" in scripts      # 注册名 ≠ 函数名 → 别名行
        assert (pack / "guards" / "is_ok.py").is_file()
        assert (pack / "submodules" / "echo_sub" / "module.json").is_file()
        assert manifest["modules"] == ["echo_sub"]
        assert manifest["default_spec"] == {"name": "world"}
        assert not any("default_spec" in w for w in result.warnings)
        assert any("shout" in w for w in result.warnings)
        assert result.dropped_templates == []

    def test_loader_roundtrip(self, entry, tmp_path):
        result = _pack(entry, tmp_path)
        sub = ModuleLoader().load(result.pack_dir, lazy_client=True)
        assert callable(sub._scripts["shout"])
        assert callable(dict(sub.guards)["is_ok"])
        assert sub.modules["echo_sub"].name == "echo_sub"
        assert any(h.name == "greet_h" for h in sub.harnesses)

    def test_dropped_templates_listed(self, entry, tmp_path):
        entry.templates["second"] = dict(entry.templates["hello_entry"])
        result = _pack(entry, tmp_path)
        assert result.dropped_templates == ["second"]
        assert any("second" in w for w in result.warnings)

    def test_explicit_template(self, entry, tmp_path):
        entry.templates["second"] = dict(entry.templates["hello_entry"])
        result = _pack(entry, tmp_path, template_name="second")
        assert result.template_name == "second"

    def test_no_default_template_error(self, entry, tmp_path):
        entry.default_template = None
        with pytest.raises(ValueError, match="default_template"):
            _pack(entry, tmp_path)

    def test_unknown_template_error(self, entry, tmp_path):
        with pytest.raises(ValueError, match="未注册"):
            _pack(entry, tmp_path, template_name="nope")

    def test_missing_harness_error(self, entry, tmp_path):
        entry.templates["hello_entry"]["tasklist"]["Tasks"]["Ghost"] = {
            "type": "harness", "harness": "ghost_h"}
        with pytest.raises(ValueError, match="ghost_h"):
            _pack(entry, tmp_path)

    def test_harness_name_mismatch_error(self, entry, tmp_path):
        from module_harness.core.config import HarnessConfig
        from module_harness.core.registry import HarnessRegistry
        from module_harness.infra.events import EventBus

        cfg = HarnessConfig.from_dict({"name": "real_name", "prompt_core": "x"})

        def bad_registry(llm_client, template_name, event_bus):
            reg = HarnessRegistry(llm_client=llm_client, event_bus=EventBus.null())
            reg.harness("greet_h", cfg)   # 注册键 greet_h ≠ cfg.name
            return reg

        entry.build_registry = bad_registry
        with pytest.raises(ValueError, match="不一致"):
            _pack(entry, tmp_path)

    def test_submodule_missing_error(self, entry, tmp_path):
        entry.submodules.clear()
        with pytest.raises(ValueError, match="echo_sub"):
            _pack(entry, tmp_path)


    def test_mutated_name_guard_alias(self, entry, tmp_path):
        # __name__ 被运行期改写为注册名、源码 def 名不变（_make_loop_guards
        # 形态）——别名判定须看源码 def 名，不看 __name__
        def _mk():
            def clean(view):
                return True
            clean.__name__ = "clean_1"
            return clean

        orig = entry.build_registry

        def wrapped_registry(llm_client, template_name, event_bus):
            reg = orig(llm_client, template_name, event_bus)
            reg.guard("clean_1", _mk())
            return reg

        entry.build_registry = wrapped_registry
        entry.templates["hello_entry"]["tasklist"]["Flow"] = (
            "[Greet] --|clean_1|--> Shout --> Echo")
        result = _pack(entry, tmp_path)
        src = (result.pack_dir / "guards" / "clean_1.py").read_text(encoding="utf-8")
        assert "clean_1 = clean" in src
        ModuleLoader().load(result.pack_dir, lazy_client=True)   # 装载通过

    def test_missing_guard_error(self, entry, tmp_path):
        entry.templates["hello_entry"]["tasklist"]["Flow"] = (
            "[Greet] --|ghost_g|--> Shout")
        with pytest.raises(ValueError, match="ghost_g"):
            _pack(entry, tmp_path)
