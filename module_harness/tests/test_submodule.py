"""SubModule / builtins / pack / ModuleLoader 测试。"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.consistency import ConsistencyError
from module_harness.events import EventBus, ScriptCompleted
from module_harness.loader import ModuleLoader, ModuleManifestError, ModuleRequirementError
from module_harness.registry import HarnessRegistry
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SpecValidationError, SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestBuiltins:
    def test_names(self):
        assert BUILTIN_HARNESS_NAMES == frozenset({"spec_to_tasklist", "spec_tasklist_review"})

    def test_register_builtins(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus())
        register_builtin_harnesses(reg)
        for name in BUILTIN_HARNESS_NAMES:
            assert reg.harness_config(name) is not None

    def test_register_builtins_idempotent(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        register_builtin_harnesses(reg)  # 重复注册不抛异常


class Translator(SubModule):
    """测试用固定翻译 submodule。"""

    name = "test_translator"
    version = "1.0.0"
    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )
    harnesses = [
        HarnessConfig(
            name="translate",
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式", "casual": "随意"},
            output_format=OutputFormat(type="json_object"),
        ),
    ]
    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                promptmode="{spec.style}",
                inputs={"text": "{spec.source_text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_output", inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )

    @script("format_output")
    def format_output(view):
        return {"translation": view.A.value["translation"].strip()}


class TestSubModule:
    def test_scripts_collected(self):
        assert set(Translator._scripts) == {"format_output"}

    def test_no_scripts_when_none_declared(self):
        class Empty(SubModule):
            name = "empty"
        assert Empty._scripts == {}

    @pytest.mark.asyncio
    async def test_run_fixed_tasklist(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        firings = await sm.run({"source_text": "Hello", "style": "formal"}, max_ticks=10)
        assert len(firings) >= 2
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"translation": "你好世界"}

    @pytest.mark.asyncio
    async def test_spec_validation_failure(self, mock_llm):
        sm = Translator(llm_client=mock_llm)
        with pytest.raises(SpecValidationError) as ei:
            await sm.run({"source_text": "Hello"})  # 缺 style
        assert "style" in str(ei.value)

    @pytest.mark.asyncio
    async def test_run_without_tasklist_raises(self, mock_llm):
        class NoTask(SubModule):
            name = "no_task"
        with pytest.raises(ValueError, match="tasklist"):
            await NoTask(llm_client=mock_llm).run({"a": 1})

    @pytest.mark.asyncio
    async def test_audit_mode_emits_events(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=True, max_ticks=10)
        assert any(isinstance(e, ScriptCompleted) for e in got)

    @pytest.mark.asyncio
    async def test_embedded_mode_no_events(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=False, max_ticks=10)
        assert got == []

    @pytest.mark.asyncio
    async def test_custom_tasklist_with_review(self, mock_llm):
        async def fake_complete(*args, **kwargs):
            return LLMResponse(
                content='{"consistent": true, "suggestions": ""}',
                usage={}, finish_reason="end_turn",
            )
        mock_llm.complete = AsyncMock(side_effect=fake_complete)
        sm = Translator(llm_client=mock_llm)
        custom = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                    outputformat={"type": "json_object"},
                ),
            },
            flow="[A]",
        )
        firings = await sm.run(
            {"source_text": "Hello", "style": "formal"}, tasklist=custom, max_ticks=10)
        assert any(f.node == "A" for f in firings)

    @pytest.mark.asyncio
    async def test_custom_tasklist_inconsistent_review_blocks(self, mock_llm):
        async def fake_complete(*args, **kwargs):
            return LLMResponse(
                content='{"consistent": false, "suggestions": "tasklist 不覆盖 spec"}',
                usage={}, finish_reason="end_turn",
            )
        mock_llm.complete = AsyncMock(side_effect=fake_complete)
        sm = Translator(llm_client=mock_llm)
        custom = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                    outputformat={"type": "json_object"},
                ),
            },
            flow="[A]",
        )
        with pytest.raises(ConsistencyError):
            await sm.run({"source_text": "Hello", "style": "formal"}, tasklist=custom, max_ticks=10)


class TestPack:
    def test_pack_structure(self, tmp_path):
        out = Translator().pack(tmp_path / "dist")
        assert (out / "module.json").is_file()
        assert (out / "harnesses" / "translate.json").is_file()
        assert (out / "scripts" / "format_output.py").is_file()

    def test_pack_manifest_content(self, tmp_path):
        out = Translator().pack(tmp_path / "dist")
        manifest = json.loads((out / "module.json").read_text(encoding="utf-8"))
        assert manifest["name"] == "test_translator"
        assert manifest["submodule"] is True
        assert manifest["spec_schema"] == {
            "input": {"source_text": "str", "style": "str"},
            "output": {"translation": "str"},
        }
        assert set(manifest["tasklist"]["Tasks"]) == {"A", "B"}
        assert manifest["tasklist"]["Flow"] == "A --> B"

    def test_pack_script_source_executable(self, tmp_path):
        out = Translator().pack(tmp_path / "dist")
        src = (out / "scripts" / "format_output.py").read_text(encoding="utf-8")
        ns: dict = {}
        exec(compile(src, "format_output.py", "exec"), ns)
        assert callable(ns["format_output"])
        # 导出后的函数签名与类内一致（纯函数，无 self）
        import inspect as _inspect
        assert "self" not in _inspect.signature(ns["format_output"]).parameters

    def test_pack_requires_missing_name(self, tmp_path):
        class NoName(SubModule):
            name = ""
            tasklist = Translator.tasklist
        with pytest.raises(ValueError, match="name"):
            NoName().pack(tmp_path / "dist")


class TestModuleLoader:
    def test_load_returns_instance(self, tmp_path, mock_llm):
        out = Translator().pack(tmp_path / "dist")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        assert isinstance(module, SubModule)
        assert module.name == "test_translator"
        assert set(module._scripts) == {"format_output"}

    @pytest.mark.asyncio
    async def test_load_roundtrip_run(self, tmp_path, mock_llm):
        out = Translator().pack(tmp_path / "dist")
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        firings = await module.run({"source_text": "Hello", "style": "formal"}, max_ticks=10)
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"translation": "你好世界"}

    def test_requires_missing(self, tmp_path, mock_llm):
        class NeedsX(Translator):
            name = "needs_x"
            requires = ["does_not_exist"]
        out = NeedsX().pack(tmp_path / "dist")
        with pytest.raises(ModuleRequirementError) as ei:
            ModuleLoader(llm_client=mock_llm).load(out)
        assert "does_not_exist" in str(ei.value)

    def test_requires_builtin_ok(self, tmp_path, mock_llm):
        class NeedsReview(Translator):
            name = "needs_review"
            requires = ["spec_tasklist_review"]
        out = NeedsReview().pack(tmp_path / "dist")
        module = ModuleLoader(llm_client=mock_llm).load(out)
        assert module.name == "needs_review"

    def test_missing_manifest(self, tmp_path, mock_llm):
        with pytest.raises(ModuleManifestError):
            ModuleLoader(llm_client=mock_llm).load(tmp_path / "nope")

    def test_bad_manifest_json(self, tmp_path, mock_llm):
        d = tmp_path / "bad"
        d.mkdir()
        (d / "module.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ModuleManifestError):
            ModuleLoader(llm_client=mock_llm).load(d)

    def test_manifest_missing_name(self, tmp_path, mock_llm):
        d = tmp_path / "noname"
        d.mkdir()
        (d / "module.json").write_text(
            json.dumps({"tasklist": {"Tasks": {}, "Flow": ""}}), encoding="utf-8")
        with pytest.raises(ModuleManifestError, match="name"):
            ModuleLoader(llm_client=mock_llm).load(d)

    def test_duplicate_provides_rejected(self, tmp_path, mock_llm):
        d = tmp_path / "dup"
        (d / "harnesses").mkdir(parents=True)
        for i in range(2):
            (d / "harnesses" / f"h{i}.json").write_text(
                json.dumps({"name": "translate", "prompt_core": "x"}),
                encoding="utf-8")
        (d / "module.json").write_text(json.dumps({
            "name": "dup_mod", "tasklist": {"Tasks": {}, "Flow": ""},
        }), encoding="utf-8")
        with pytest.raises(ModuleManifestError, match="重复"):
            ModuleLoader(llm_client=mock_llm).load(d)

    def test_manifest_not_object(self, tmp_path, mock_llm):
        d = tmp_path / "arr"
        d.mkdir()
        (d / "module.json").write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ModuleManifestError):
            ModuleLoader(llm_client=mock_llm).load(d)

    def test_requires_non_string_rejected(self, tmp_path, mock_llm):
        class BadReq(Translator):
            name = "bad_req"
            requires = [42]
        out = BadReq().pack(tmp_path / "dist")
        with pytest.raises(ModuleManifestError, match="requires"):
            ModuleLoader(llm_client=mock_llm).load(out)

    def test_script_syntax_error_wrapped(self, tmp_path, mock_llm):
        d = tmp_path / "badscript"
        (d / "scripts").mkdir(parents=True)
        (d / "scripts" / "oops.py").write_text("def broken(:\n", encoding="utf-8")
        (d / "module.json").write_text(json.dumps({
            "name": "bad_script", "tasklist": {"Tasks": {}, "Flow": ""},
        }), encoding="utf-8")
        with pytest.raises(ModuleManifestError, match="oops.py"):
            ModuleLoader(llm_client=mock_llm).load(d)

    def test_script_missing_function_wrapped(self, tmp_path, mock_llm):
        d = tmp_path / "nofn"
        (d / "scripts").mkdir(parents=True)
        (d / "scripts" / "target.py").write_text("x = 1\n", encoding="utf-8")
        (d / "module.json").write_text(json.dumps({
            "name": "no_fn", "tasklist": {"Tasks": {}, "Flow": ""},
        }), encoding="utf-8")
        with pytest.raises(ModuleManifestError, match="target"):
            ModuleLoader(llm_client=mock_llm).load(d)
