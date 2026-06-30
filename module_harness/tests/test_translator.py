from module_harness.translator import TemplateLoader
from module_harness.spec import TasklistTemplate


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
