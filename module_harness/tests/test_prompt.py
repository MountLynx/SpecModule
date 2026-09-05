# module_harness/tests/test_prompt.py
from tickflow.views import Missing, NodeView
from module_harness.core.config import HarnessConfig
from module_harness.core.prompt import PromptRenderer


def _make_view(**inputs) -> NodeView:
    """构造一个测试用 NodeView：模拟引擎对具名 bind body 供数的视图
    （字段名 → 值经 v.named 消费）。"""
    return NodeView(
        node="test_node",
        fields=tuple((k, k) for k in inputs),
        values=tuple(inputs.values()),
    )


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


class TestNamedBindSubstitution:
    """_substitute 的具名 bind 查找契约（bind 迁移回归钉）。

    Missing 三态（tickflow bind 设计 §4.6）：
      - 字段已点火 → 渲染值（含生产者真产出 None → 渲染 "None"）；
      - 字段未点火（值 = Missing）→ extra_values 兜底（spec 常量）；
      - 非 bind 字段的 key → extra_values 兜底 -> 保留原样（不隐藏问题）。
    """

    TEMPLATE = "值：{text} / {unknown_key}"

    def _sub(self, view, extra_values=None):
        cfg = HarnessConfig(prompt_core="x")
        return PromptRenderer(cfg)._substitute(self.TEMPLATE, view, extra_values)

    def test_field_renders_value(self):
        view = _make_view(text="Hello")
        assert self._sub(view) == "值：Hello / {unknown_key}"

    def test_missing_field_falls_back_to_extra_values(self):
        # 字段已声明（具名 bind）但生产者未点火 → Missing → spec 常量兜底
        view = NodeView(node="n", fields=(("text", "src"),), values=(Missing,))
        assert self._sub(view, {"text": "SPEC-CONST"}) == "值：SPEC-CONST / {unknown_key}"

    def test_missing_field_without_extra_keeps_literal(self):
        view = NodeView(node="n", fields=(("text", "src"),), values=(Missing,))
        assert self._sub(view) == "值：{text} / {unknown_key}"

    def test_none_field_renders_none_not_fallback(self):
        # 生产者真产出 None：渲染 "None"，不落兜底、不保留字面（与 Missing 可区分）
        view = NodeView(node="n", fields=(("text", "src"),), values=(None,))
        assert self._sub(view, {"text": "SPEC-CONST"}) == "值：None / {unknown_key}"

    def test_unknown_key_uses_extra_then_literal(self):
        # 非 bind 字段：先落 extra_values（常量），再保留字面
        view = _make_view(other="x")
        assert self._sub(view, {"text": "SPEC-CONST"}) == "值：SPEC-CONST / {unknown_key}"
