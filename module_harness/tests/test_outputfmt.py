import json
import pytest
from tickflow import Failure
from module_harness.core.outputfmt import OutputFormat, OutputValidator


class TestOutputFormat:
    def test_defaults(self):
        fmt = OutputFormat(type="text")
        assert fmt.type == "text"
        assert fmt.schema is None
        assert fmt.instruction is None


class TestOutputValidatorText:
    def test_text_passthrough(self):
        v = OutputValidator(OutputFormat(type="text"))
        result = v.validate("任意文本")
        assert result == "任意文本"

    def test_prompt_instruction_text_is_empty(self):
        v = OutputValidator(OutputFormat(type="text"))
        assert v.prompt_instruction() == ""


class TestOutputValidatorJsonObject:
    def test_valid_json(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        result = v.validate('{"a": 1}')
        assert result == {"a": 1}

    def test_valid_json_array(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        result = v.validate('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_invalid_json_stripped_by_extractor(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        # markdown fence + trailing text
        raw = '```json\n{"name": "test"}\n```'
        result = v.validate(raw)
        assert result == {"name": "test"}

    def test_json_buried_in_text_extracted(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        raw = '解释：{"result": 42} 这是输出'
        result = v.validate(raw)
        assert result == {"result": 42}

    def test_trailing_junk_after_json(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        raw = '{"ok": true}。'
        result = v.validate(raw)
        assert result == {"ok": True}

    def test_completely_invalid_returns_failure(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        raw = '这不是 JSON 也不是任何可提取的内容'
        result = v.validate(raw)
        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert "输出格式校验失败" in result.error

    def test_register_custom_extractor(self):
        v = OutputValidator(OutputFormat(type="json_object"))

        def my_extractor(s: str) -> str | None:
            if s.startswith("RESULT:"):
                return s[7:].strip()
            return None

        v.register_extractor(my_extractor)
        result = v.validate("RESULT: [1,2]")
        assert result == [1, 2]

    def test_prompt_json_object_instruction(self):
        v = OutputValidator(OutputFormat(type="json_object"))
        inst = v.prompt_instruction()
        assert "JSON" in inst


class TestOutputValidatorJsonSchema:
    def test_valid_against_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        result = v.validate('{"name": "Alice", "age": 30}')
        assert result == {"name": "Alice", "age": 30}

    def test_invalid_against_schema_returns_failure(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        result = v.validate('{"age": 30}')
        assert isinstance(result, Failure)
        assert result.type == "llm"

    def test_schema_extraction_then_validates(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "number"}},
            "required": ["x"],
        }
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        raw = '```json\n{"x": 3.14}\n```'
        result = v.validate(raw)
        assert result == {"x": 3.14}

    def test_prompt_instruction_includes_schema(self):
        schema = {"type": "object", "properties": {}}
        v = OutputValidator(OutputFormat(type="json_schema", schema=schema))
        inst = v.prompt_instruction()
        assert "schema" in inst.lower()


class TestOutputValidatorCustomInstruction:
    def test_custom_instruction_overrides_default(self):
        v = OutputValidator(OutputFormat(type="json_object", instruction="请返回 {'key': value} 格式"))
        inst = v.prompt_instruction()
        assert inst == "请返回 {'key': value} 格式"
