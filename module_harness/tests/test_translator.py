from module_harness.model.translator import TemplateLoader
from module_harness.model.spec import TasklistTemplate


class TestTemplateLoader:
    def test_register_and_get(self):
        loader = TemplateLoader()
        loader.register("test", {
            "name": "test",
            "description": "测试模板",
            "translation": {"type": "script", "script": "t"},
            "tasklist": {"Tasks": {"A": {"type": "script", "script": "s"}}, "Flow": "A"},
        })
        tmpl = loader.get("test")
        assert tmpl is not None
        assert tmpl.name == "test"
        assert tmpl.translation.type == "script"

    def test_get_nonexistent_returns_none(self):
        loader = TemplateLoader()
        assert loader.get("nope") is None

    def test_list_names(self):
        loader = TemplateLoader()
        loader.register("a", {"name": "a", "translation": {"type": "script", "script": "x"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        loader.register("b", {"name": "b", "translation": {"type": "script", "script": "y"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        names = loader.list_names()
        assert "a" in names
        assert "b" in names

    def test_duplicate_register_overwrites(self):
        loader = TemplateLoader()
        loader.register("x", {"name": "x", "description": "first", "translation": {"type": "script", "script": "a"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        loader.register("x", {"name": "x", "description": "second", "translation": {"type": "script", "script": "b"}, "tasklist": {"Tasks": {}, "Flow": ""}})
        assert loader.get("x").description == "second"

    def test_load_directory(self, tmp_path):
        import json
        tmpl_dir = tmp_path / "templates"
        tmpl_dir.mkdir()
        data = {
            "name": "from_file",
            "description": "loaded from file",
            "translation": {"type": "script", "script": "s"},
            "tasklist": {"Tasks": {"A": {"type": "script", "script": "s"}}, "Flow": "A"},
        }
        (tmpl_dir / "from_file.json").write_text(json.dumps(data), encoding="utf-8")

        loader = TemplateLoader()
        loader.load_directory(str(tmpl_dir))
        assert loader.get("from_file") is not None

    def test_load_directory_skips_invalid_json(self, tmp_path):
        tmpl_dir = tmp_path / "templates"
        tmpl_dir.mkdir()
        (tmpl_dir / "bad.json").write_text("not json", encoding="utf-8")

        loader = TemplateLoader()
        loader.load_directory(str(tmpl_dir))  # 不应抛异常
        assert "bad" not in loader.list_names()

    def test_load_builtins(self):
        loader = TemplateLoader()
        loader.load_builtins()
        # 内置 translate 模板应已注册
        tmpl = loader.get("translate")
        assert tmpl is not None
        assert tmpl.name == "translate"


import pytest
from unittest.mock import AsyncMock
from module_harness.model.spec import Spec
from module_harness.model.translator import Translator
from module_harness.core.registry import HarnessRegistry


class TestTranslator:
    @pytest.mark.asyncio
    async def test_script_translation(self, mock_llm):
        """script 翻译：调用已注册的 script 函数。"""
        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        # 注册翻译 script
        @reg.script("my_translator")
        def my_translator(view):
            spec = view.field("spec")  # 翻译器合成视图具名字段
            return {
                "A": {"type": "harness", "harness": spec["harness_name"], "inputs": {"text": spec["source_text"]}}
            }

        # 注册引用到的 harness
        from module_harness.core.config import HarnessConfig
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))

        loader.register("test_module", {
            "name": "test_module",
            "translation": {"type": "script", "script": "my_translator"},
            "tasklist": {"Tasks": {}, "Flow": "[A]"},
        })

        translator = Translator(reg)
        tmpl = loader.get("test_module")
        spec = Spec({"harness_name": "translate", "source_text": "Hello"})
        tasklist = await translator.translate(spec, tmpl)

        assert tasklist is not None
        assert "A" in tasklist.tasks
        assert tasklist.tasks["A"].harness == "translate"
        assert tasklist.tasks["A"].inputs == {"text": "Hello"}

    @pytest.mark.asyncio
    async def test_harness_translation(self, mock_llm):
        """harness 翻译：LLM 生成 tasklist JSON。"""
        from llm.client import LLMResponse

        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        # 注册翻译 harness
        from module_harness.core.config import HarnessConfig
        from module_harness.core.outputfmt import OutputFormat
        reg.harness("spec_to_tasklist", HarnessConfig(
            prompt_core="生成 tasklist JSON",
            output_format=OutputFormat(type="json_object"),
        ))

        # 注册引用到的 harness
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))

        mock_llm.complete = AsyncMock(return_value=LLMResponse(
            content='{"A": {"type": "harness", "harness": "translate", "inputs": {"text": "Hello"}}}',
            usage={},
            finish_reason="end_turn",
        ))

        loader.register("test_llm_module", {
            "name": "test_llm_module",
            "translation": {"type": "harness", "harness": "spec_to_tasklist", "prompt": "根据 spec 生成 tasklist"},
            "tasklist": {"Tasks": {}, "Flow": "[A]"},
        })

        translator = Translator(reg)
        tmpl = loader.get("test_llm_module")
        spec = Spec({"task_type": "translate", "source_text": "Hello"})

        tasklist = await translator.translate(spec, tmpl)

        assert tasklist is not None
        # harness 翻译返回的 tasklist 用 LLM 响应解析，由 call_translation_harness 内部解析
        assert "A" in tasklist.tasks

    @pytest.mark.asyncio
    async def test_translation_validates_result(self, mock_llm):
        """翻译结果需通过 TasklistValidator 校验。"""
        from llm.client import LLMResponse

        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        mock_llm.complete = AsyncMock(return_value=LLMResponse(
            content='{"A": {"type": "harness", "harness": "nonexistent"}}',
            usage={},
            finish_reason="end_turn",
        ))

        from module_harness.core.config import HarnessConfig
        from module_harness.core.outputfmt import OutputFormat
        reg.harness("spec_to_tasklist", HarnessConfig(
            prompt_core="...",
            output_format=OutputFormat(type="json_object"),
        ))

        loader.register("bad_module", {
            "name": "bad_module",
            "translation": {"type": "harness", "harness": "spec_to_tasklist", "prompt": "..."},
            "tasklist": {"Tasks": {}, "Flow": ""},
        })

        translator = Translator(reg)
        tmpl = loader.get("bad_module")
        spec = Spec({})

        with pytest.raises(ValueError, match="校验"):
            await translator.translate(spec, tmpl)

    @pytest.mark.asyncio
    async def test_harness_translation_renders_spec_placeholder(self, mock_llm):
        """harness 翻译 prompt 的 {spec}/{prompt_extra} 占位符必须渲染。

        bind 迁移回归钉：翻译器合成视图按具名 bind 供数（v.named），
        占位符不得原样直达 LLM。
        """
        from llm.client import LLMResponse

        reg = HarnessRegistry(llm_client=mock_llm)
        loader = TemplateLoader()

        from module_harness.core.config import HarnessConfig
        from module_harness.core.outputfmt import OutputFormat
        reg.harness("spec_to_tasklist", HarnessConfig(
            prompt_core="根据 spec 生成 tasklist JSON。\nspec：{spec}\n附加：{prompt_extra}",
            output_format=OutputFormat(type="json_object"),
        ))
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))

        mock_llm.complete = AsyncMock(return_value=LLMResponse(
            content='{"A": {"type": "harness", "harness": "translate", "inputs": {"text": "Hello"}}}',
            usage={},
            finish_reason="end_turn",
        ))

        loader.register("test_llm_module_placeholder", {
            "name": "test_llm_module_placeholder",
            "translation": {
                "type": "harness",
                "harness": "spec_to_tasklist",
                "prompt": "附加说明文本",
            },
            "tasklist": {"Tasks": {}, "Flow": "[A]"},
        })

        translator = Translator(reg)
        tmpl = loader.get("test_llm_module_placeholder")
        spec = Spec({"task_type": "translate", "source_text": "Hello"})

        await translator.translate(spec, tmpl)

        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        # {spec} → spec dict（str() 渲染，与迁移前 view[key].value 语义一致）
        assert str(spec.to_dict()) in prompt
        assert "附加：附加说明文本" in prompt          # {prompt_extra} → ts.prompt 字符串
        assert "{spec}" not in prompt                # 不得原样直达 LLM
        assert "{prompt_extra}" not in prompt
