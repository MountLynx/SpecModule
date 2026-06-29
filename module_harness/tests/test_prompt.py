# module_harness/tests/test_prompt.py
from tickflow.views import DictView, Resolved
from module_harness.config import HarnessConfig
from module_harness.prompt import PromptRenderer


def _make_view(**inputs) -> DictView:
    """构造一个测试用 DictView。"""
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="test_node")


class TestPromptRenderer:
    def test_layer1_only(self):
        cfg = HarnessConfig(prompt_core="请翻译以下内容。")
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        assert result == "请翻译以下内容。"

    def test_layer1_with_keyword_substitution(self):
        cfg = HarnessConfig(prompt_core="翻译：{text}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view(text="Hello world"))
        assert result == "翻译：Hello world"

    def test_multiple_keywords(self):
        cfg = HarnessConfig(prompt_core="将 {source} 翻译为 {target}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view(source="Hello", target="Chinese"))
        assert "Hello" in result
        assert "Chinese" in result

    def test_layer2_selected_by_promptmode(self):
        cfg = HarnessConfig(
            prompt_core="任务：{input}",
            prompt_modes={"formal": "请使用正式语气。", "casual": "请使用日常语气。"},
        )
        r = PromptRenderer(cfg)
        result = r.render(_make_view(input="介绍"), promptmode="formal")
        assert "请使用正式语气。" in result
        assert result.index("任务") < result.index("请使用正式语气")

    def test_layer3_prompt_extra(self):
        cfg = HarnessConfig(prompt_core="翻译：{text}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view(text="Hi"), prompt_extra="特别注意：不要意译。")
        assert "特别注意：不要意译。" in result

    def test_all_three_layers(self):
        cfg = HarnessConfig(
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式风格。"},
        )
        r = PromptRenderer(cfg)
        result = r.render(
            _make_view(text="Hello"),
            promptmode="formal",
            prompt_extra="术语要准确。",
        )
        # Layer 1 在前，Layer 2 在中，Layer 3 在后
        assert result.index("翻译") < result.index("正式风格") < result.index("术语")

    def test_promptmode_keyerror_on_invalid(self):
        cfg = HarnessConfig(
            prompt_core="x",
            prompt_modes={"a": "mode a"},
        )
        r = PromptRenderer(cfg)
        import pytest
        with pytest.raises(KeyError):
            r.render(_make_view(), promptmode="nonexistent")

    def test_none_promptmode_skips_layer2(self):
        cfg = HarnessConfig(
            prompt_core="核心内容。",
            prompt_modes={"x": "不应该出现"},
        )
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        assert "不应该出现" not in result
        assert result == "核心内容。"

    def test_keyword_not_in_view_becomes_missing_text(self):
        cfg = HarnessConfig(prompt_core="值：{missing_key}")
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        # 未解析的占位符保持不变（或替换为空）
        # 此处根据设计：未匹配的 key 保留原样（不隐藏问题）
        assert "{missing_key}" in result or "Missing" in result

    def test_no_double_whitespace(self):
        cfg = HarnessConfig(prompt_core="核心。")
        r = PromptRenderer(cfg)
        result = r.render(_make_view())
        # 单一层不应有多余空行
        assert result == "核心。"
