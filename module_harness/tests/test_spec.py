# module_harness/tests/test_spec.py
import pytest
from module_harness.spec import (
    Spec, SpecSchema, TaskDefinition, Tasklist, TranslationSpec,
    TasklistTemplate,
)


class TestSpec:
    def test_spec_is_dict_wrapper(self):
        s = Spec({"task_type": "translate", "style": "formal"})
        assert s["task_type"] == "translate"
        assert s.get("style") == "formal"
        assert s.get("missing", "default") == "default"

    def test_spec_len_and_in(self):
        s = Spec({"a": 1, "b": 2})
        assert len(s) == 2
        assert "a" in s
        assert "c" not in s

    def test_spec_iter_keys(self):
        s = Spec({"x": 1, "y": 2})
        assert set(s.keys()) == {"x", "y"}


class TestTaskDefinition:
    def test_minimal_harness_task(self):
        t = TaskDefinition(type="harness", harness="translate")
        assert t.type == "harness"
        assert t.harness == "translate"
        assert t.script is None

    def test_minimal_script_task(self):
        t = TaskDefinition(type="script", script="post_process")
        assert t.type == "script"
        assert t.script == "post_process"
        assert t.harness is None

    def test_full_harness_task(self):
        t = TaskDefinition(
            type="harness",
            harness="translate",
            promptmode="formal",
            prompt="特别注意术语",
            outputformat={"type": "json_object"},
            notdo=["不要直译"],
            model="gpt-4o",
            temperature=0.3,
            inputs={"text": "source_text"},
        )
        assert t.promptmode == "formal"
        assert t.notdo == ["不要直译"]
        assert t.inputs == {"text": "source_text"}

    def test_from_dict_harness(self):
        d = {"type": "harness", "harness": "t", "promptmode": "formal"}
        t = TaskDefinition.from_dict(d)
        assert t.type == "harness"
        assert t.harness == "t"
        assert t.promptmode == "formal"

    def test_from_dict_script(self):
        d = {"type": "script", "script": "s", "inputs": {"x": "y"}}
        t = TaskDefinition.from_dict(d)
        assert t.type == "script"
        assert t.script == "s"
        assert t.inputs == {"x": "y"}


class TestTasklist:
    def test_from_json(self):
        data = {
            "Tasks": {
                "A": {"type": "harness", "harness": "t", "inputs": {"text": "src"}},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
            },
            "Flow": "A --> B",
        }
        tl = Tasklist.from_json(data)
        assert len(tl.tasks) == 2
        assert tl.tasks["A"].type == "harness"
        assert tl.tasks["B"].type == "script"
        assert tl.flow == "A --> B"

    def test_from_json_missing_tasks_raises(self):
        with pytest.raises(ValueError, match="Tasks"):
            Tasklist.from_json({"Flow": "A --> B"})

    def test_from_json_missing_flow_raises(self):
        with pytest.raises(ValueError, match="Flow"):
            Tasklist.from_json({"Tasks": {"A": {"type": "script", "script": "s"}}})


class TestTranslationSpec:
    def test_harness_translation(self):
        ts = TranslationSpec(type="harness", harness="spec_to_tasklist", prompt="...")
        assert ts.type == "harness"
        assert ts.script is None

    def test_script_translation(self):
        ts = TranslationSpec(type="script", script="my_translator")
        assert ts.type == "script"
        assert ts.harness is None

    def test_from_dict(self):
        d = {"type": "harness", "harness": "h", "prompt": "p"}
        ts = TranslationSpec.from_dict(d)
        assert ts.type == "harness"
        assert ts.harness == "h"
        assert ts.prompt == "p"


class TestTasklistTemplate:
    def test_from_json(self):
        data = {
            "name": "translate",
            "description": "翻译模块",
            "translation": {"type": "harness", "harness": "stt", "prompt": "..."},
            "tasklist": {
                "Tasks": {"A": {"type": "harness", "harness": "t"}},
                "Flow": "A",
            },
        }
        tmpl = TasklistTemplate.from_json(data)
        assert tmpl.name == "translate"
        assert tmpl.translation.type == "harness"
        assert tmpl.tasklist.tasks["A"].type == "harness"

    def test_from_json_missing_name_raises(self):
        data = {"translation": {"type": "script", "script": "s"}, "tasklist": {"Tasks": {}, "Flow": ""}}
        with pytest.raises(ValueError, match="name"):
            TasklistTemplate.from_json(data)


class TestSpecSchema:
    def test_validate_passes(self):
        schema = SpecSchema(input={"a": "str", "n": "int", "b": "bool"})
        assert schema.validate({"a": "x", "n": 1, "b": True}) == []

    def test_validate_missing_field(self):
        schema = SpecSchema(input={"a": "str"})
        errors = schema.validate({})
        assert len(errors) == 1
        assert "a" in errors[0]

    def test_validate_wrong_type(self):
        schema = SpecSchema(input={"n": "int"})
        errors = schema.validate({"n": "1"})
        assert len(errors) == 1

    def test_bool_and_int_not_interchangeable(self):
        schema = SpecSchema(input={"n": "int", "b": "bool"})
        errors = schema.validate({"n": True, "b": 1})
        assert len(errors) == 2

    def test_any_type(self):
        schema = SpecSchema(input={"x": "any"})
        assert schema.validate({"x": object()}) == []

    def test_unknown_type_declared(self):
        schema = SpecSchema(input={"x": "date"})
        errors = schema.validate({"x": "2026-01-01"})
        assert len(errors) == 1

    def test_undeclared_fields_allowed(self):
        schema = SpecSchema(input={"a": "str"})
        assert schema.validate({"a": "x", "extra": 42}) == []

    def test_default_empty_schema(self):
        assert SpecSchema().validate({}) == []


class TestTasklistToDict:
    def test_matches_tasklist_to_dict(self):
        from module_harness.checkpoint import tasklist_to_dict
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="echo",
                                       inputs={"x": "B"})},
            flow="[A] --> B",
        )
        assert tl.to_dict() == tasklist_to_dict(tl) == {
            "Tasks": {"A": {"type": "script", "script": "echo",
                            "harness": None, "command": None, "timeout": None,
                            "cwd": None, "promptmode": None, "prompt": None,
                            "outputformat": None, "notdo": None, "model": None,
                            "temperature": None, "think": None,
                            "api_params": None, "inputs": {"x": "B"}}},
            "Flow": "[A] --> B",
        }
