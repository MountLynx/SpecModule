"""SubModule / builtins / pack / ModuleLoader 测试。"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.core.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.core.config import HarnessConfig, OutputFormat
from module_harness.orchestrate.consistency import ConsistencyError
from module_harness.infra.events import EventBus, HarnessFailed, ScriptCompleted
from module_harness.cli.loader import ModuleLoader, ModuleManifestError, ModuleRequirementError
from module_harness.model.module import Module
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.model.submodule import SpecValidationError, SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestBuiltins:
    def test_names(self):
        assert BUILTIN_HARNESS_NAMES == frozenset(
            {"spec_to_tasklist", "spec_tasklist_review", "align_check"}
        )

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


def until3(view):
    return view["counter"].value["n"] < 3


class LoopMod(SubModule):
    """带 guard 自循环的 submodule：n 递增到 3 后 guard 放行退出。"""

    name = "loop_mod"
    spec_schema = SpecSchema(input={}, output={"n": "int"})
    guards = [("until3", until3)]
    tasklist = Tasklist(
        tasks={"counter": TaskDefinition(type="script", script="counter")},
        flow="[counter] --|until3|--> counter",
    )

    @script("counter")
    def counter(view):
        n = view.state.get("n", 0) + 1
        view.state["n"] = n
        return {"n": n}


class TestGuards:
    def test_guards_copied_between_subclasses(self):
        class G1(SubModule):
            name = "g1"
            guards = [("g", lambda view: True)]

        class G2(G1):
            name = "g2"

        assert G2.guards == G1.guards
        assert G1.guards is not G2.guards
        G2.guards.append(("h", lambda view: False))
        assert len(G1.guards) == 1  # 不污染父类

    @pytest.mark.asyncio
    async def test_loop_runs_until_guard_opens(self, mock_llm):
        """带 guard 的 loop tasklist 正常终止（校验 + 构建 + 运行全链路）。"""
        firings = await LoopMod(llm_client=mock_llm).run({}, max_ticks=20)
        assert len(firings) == 3
        assert firings[-1].output == {"n": 3}


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
    async def test_embed_mode_events_flow_without_records(
        self, mock_llm, monkeypatch
    ):
        """嵌入模式（audit=False）：宿主 bus 事件可达，但 keep_records=False。

        事件投递与 records 解耦——宿主传 event_bus 即选择性订阅，无需开启审计。
        旧契约（audit=False 时宿主 bus 收不到事件）已反转。
        """
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        # 捕获 SubModule 构造的 Module，验证 audit=False → keep_records=False
        from module_harness import submodule as sm_module
        orig_module = sm_module.Module
        captured: dict = {}

        def spy_module(**kwargs):
            captured["keep_records"] = kwargs.get("keep_records")
            return orig_module(**kwargs)

        monkeypatch.setattr(sm_module, "Module", spy_module)
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=False, max_ticks=10)
        # 事件可达（旧契约相反）
        assert any(isinstance(e, ScriptCompleted) for e in got)
        # 且 audit=False → keep_records=False（事件开、records 关）
        assert captured["keep_records"] is False

    @pytest.mark.asyncio
    async def test_embed_mode_receives_harness_failed(self, mock_llm):
        """嵌入模式：宿主可订阅 HarnessFailed（翻译失败原因给用户反馈）。"""
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("API timeout")
        bus = EventBus()
        failures: list = []
        bus.subscribe(HarnessFailed, lambda e: failures.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=False, max_ticks=10)
        assert any(isinstance(e, HarnessFailed) for e in failures)
        assert failures[0].failure_type == "infrastructure"

    @pytest.mark.asyncio
    async def test_submodule_mode_fast_zero_residue(self, tmp_path, monkeypatch, mock_llm):
        """SubModule(mode="fast")：不写 DB 也不写 status.json（完全零残留）。"""
        monkeypatch.chdir(tmp_path)

        class FastMod(SubModule):
            name = "fast_mod"
            mode = "fast"
            tasklist = Tasklist(
                tasks={"A": TaskDefinition(type="script", script="echo")},
                flow="[A]",
            )

            @script("echo")
            def echo(view):
                return {"ok": True}

        mod = FastMod(llm_client=mock_llm)
        firings = await mod.run({"x": 1}, max_ticks=10)
        assert len(firings) >= 1
        assert not (tmp_path / ".specmodule").exists()

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

    @pytest.mark.asyncio
    async def test_harness_overrides_propagate_to_all_harnesses(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run(
            {"source_text": "Hello", "style": "formal"},
            harness_overrides={"model": "model-x", "temperature": 0.7},
            max_ticks=10,
        )
        calls = mock_llm.complete.await_args_list
        assert calls
        assert all(c.kwargs.get("model") == "model-x" for c in calls)
        assert all(c.kwargs.get("temperature") == 0.7 for c in calls)

    @pytest.mark.asyncio
    async def test_harness_overrides_api_params_merge(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run(
            {"source_text": "Hello", "style": "formal"},
            harness_overrides={"api_params": {"max_tokens": 200}},
            max_ticks=10,
        )
        calls = mock_llm.complete.await_args_list
        assert calls
        assert all(c.kwargs.get("api_params", {}).get("max_tokens") == 200 for c in calls)

    @pytest.mark.asyncio
    async def test_run_persist_false_zero_residue(self, tmp_path, monkeypatch, mock_llm):
        monkeypatch.chdir(tmp_path)
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run(
            {"source_text": "Hello", "style": "formal"}, persist=False, max_ticks=10)
        assert not (tmp_path / ".specmodule").exists()

    @pytest.mark.asyncio
    async def test_run_writes_module_field(self, tmp_path, monkeypatch, mock_llm):
        """溯源：packed 路径 SubModule.run 透传 module=self.name →
        status.json "module" 键 = submodule 名（与 entry 路径一致）。"""
        monkeypatch.chdir(tmp_path)
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        await sm.run({"source_text": "Hello", "style": "formal"}, max_ticks=10)
        run_dir = next((tmp_path / ".specmodule" / "runs").iterdir())
        status = json.loads(
            (run_dir / "status.json").read_text(encoding="utf-8")
        )
        assert status["module"] == "test_translator"


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


class TestPackGuards:
    def test_pack_exports_guards(self, tmp_path):
        out = LoopMod().pack(tmp_path / "dist")
        guard_file = out / "guards" / "until3.py"
        assert guard_file.is_file()
        ns: dict = {}
        exec(compile(guard_file.read_text(encoding="utf-8"), "until3.py", "exec"), ns)
        assert callable(ns["until3"])

    def test_pack_guard_name_mismatch_rejected(self, tmp_path):
        class BadGuard(SubModule):
            name = "bad_guard"
            tasklist = LoopMod.tasklist
            guards = [("renamed", until3)]  # 注册名 ≠ 函数名

        with pytest.raises(ValueError, match="不一致"):
            BadGuard().pack(tmp_path / "dist")


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

    @pytest.mark.asyncio
    async def test_two_instances_namespace_isolation(self, tmp_path, mock_llm):
        out = Translator().pack(tmp_path / "dist")
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        loader = ModuleLoader(llm_client=mock_llm)
        f1 = await loader.load(out).run(
            {"source_text": "Hello", "style": "formal"}, max_ticks=10)
        f2 = await loader.load(out).run(
            {"source_text": "Hi", "style": "formal"}, max_ticks=10)
        assert next(f.output for f in f1 if f.node == "B") == {"translation": "你好世界"}
        assert next(f.output for f in f2 if f.node == "B") == {"translation": "你好世界"}


class TestScriptNameCheck:
    def test_script_name_mismatch_raises(self):
        with pytest.raises(ValueError, match="不一致"):
            class Bad(SubModule):
                name = "bad"

                @script("renamed")
                def actual_fn(view):
                    return {}


class TestModulesAttr:
    def test_modules_copied_between_subclasses(self):
        class M1(SubModule):
            name = "m1"
            modules = {"child": object}

        class M2(M1):
            name = "m2"

        assert M2.modules == {"child": object}
        assert M1.modules is not M2.modules
        M2.modules["other"] = object
        assert "other" not in M1.modules  # 不污染父类

    def test_modules_explicit_override(self):
        class M1(SubModule):
            name = "m1"
            modules = {"child": object}

        class M2(M1):
            name = "m2"
            modules = {"another": object}  # 显式定义覆盖

        assert set(M2.modules) == {"another"}

    def test_module_validates_submodule_refs_against_modules(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="submodule", submodule="missing")},
            flow="[A]",
        )
        m = Module(spec={}, tasklist=tl, llm_client=object())
        with pytest.raises(ValueError, match="未在 modules 中声明"):
            m.build_runner()
