"""fact_review_loop 模块 mock 测试（无 key 可跑，MagicMock 逐节点预设输出）。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.spec import SpecValidationError

from example.fact_review_loop import FactReviewLoop


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage={}, finish_reason="end_turn")


def _make_side_effect(review_bodies: list[str], fix_text: str = '{"text": "draft fixed"}'):
    """按 prompt 关键词分发：seed 原样转发 / review 按给定序列返回 / fix 返回修复稿。"""
    review_calls = {"n": 0}

    async def side_effect(**kwargs):
        prompt = kwargs.get("prompt", "")
        if "原样转发" in prompt:
            return _resp('{"text": "draft v1"}')
        if "修复者" in prompt:
            return _resp(fix_text)
        # 审阅：按 review_bodies 序列依次返回
        body = review_bodies[min(review_calls["n"], len(review_bodies) - 1)]
        review_calls["n"] += 1
        return _resp(body)

    return side_effect


class TestFactReviewLoop:
    @pytest.mark.asyncio
    async def test_clean_first_round(self, mock_llm):
        """首轮审阅即 clean：Seed → Review → Exit，无 Fix 触发。"""
        mock_llm.complete.side_effect = _make_side_effect(
            ['{"issues": [], "clean": true}'])
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out == {
            "text": "draft v1", "attempt": 1, "clean": True, "issues_remaining": [],
        }

    @pytest.mark.asyncio
    async def test_fix_then_clean(self, mock_llm):
        """首轮报 issues → Fix 触发 → 二轮 clean → 输出 attempt=2。"""
        mock_llm.complete.side_effect = _make_side_effect(
            [
                '{"issues": [{"type": "omission", "detail": "缺结论", '
                '"quote_original": "…", "quote_draft": "…"}], "clean": false}',
                '{"issues": [], "clean": true}',
            ],
            fix_text='{"text": "draft v2 fixed"}',
        )
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out == {
            "text": "draft v2 fixed", "attempt": 2, "clean": True, "issues_remaining": [],
        }

    @pytest.mark.asyncio
    async def test_max_attempts_exit_with_issues(self, mock_llm):
        """审阅恒报 issues → 第 3 轮后 clean 边强制退出，遗留 issues 进 issues_remaining。"""
        issues_body = (
            '{"issues": [{"type": "hallucination", "detail": "杜撰数据", '
            '"quote_original": "无", "quote_draft": "85%"}], "clean": false}'
        )
        mock_llm.complete.side_effect = _make_side_effect([issues_body])
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=50,
        ))[-1].output
        assert out["clean"] is False
        assert out["attempt"] == 3
        assert out["issues_remaining"] == [{
            "type": "hallucination", "detail": "杜撰数据",
            "quote_original": "无", "quote_draft": "85%",
        }]

    @pytest.mark.asyncio
    async def test_review_missing_fields_safe(self, mock_llm):
        """审阅输出缺 issues/clean 字段：不循环、不崩溃（防御侧返回 False）。"""
        mock_llm.complete.side_effect = _make_side_effect(['{"foo": 1}'])
        out = (await FactReviewLoop(llm_client=mock_llm).run(
            {"original_text": "orig", "draft_text": "draft v0"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out["attempt"] == 1
        assert out["issues_remaining"] == []
        assert out["clean"] is False

    @pytest.mark.asyncio
    async def test_missing_spec_field_raises(self, mock_llm):
        """缺 original_text → SpecValidationError（spec 契约校验）。"""
        with pytest.raises(SpecValidationError):
            await FactReviewLoop(llm_client=mock_llm).run(
                {"draft_text": "draft v0"}, persist=False, max_ticks=20)

    def test_pack_exports_guards(self, tmp_path):
        """pack 导出 guards/*.py（注册名 = 函数名 = 文件名）。"""
        dist = FactReviewLoop().pack(tmp_path / "dist")
        assert (dist / "guards" / "has_issues.py").is_file()
        assert (dist / "guards" / "clean.py").is_file()
        ns: dict = {}
        exec(compile(
            (dist / "guards" / "has_issues.py").read_text(encoding="utf-8"),
            "has_issues.py", "exec"), ns)
        assert callable(ns["has_issues"])

    @pytest.mark.asyncio
    async def test_pack_load_roundtrip_runs(self, tmp_path, mock_llm):
        """pack → load roundtrip 后 guard 可解析、loop 照常运行。"""
        from module_harness.loader import ModuleLoader

        dist = FactReviewLoop().pack(tmp_path / "dist")
        loaded = ModuleLoader(llm_client=mock_llm).load(dist)
        assert {name for name, _ in loaded.guards} == {"has_issues", "clean"}
        mock_llm.complete.side_effect = _make_side_effect(
            ['{"issues": [], "clean": true}'])
        out = (await loaded.run(
            {"original_text": "orig", "draft_text": "d"},
            persist=False, max_ticks=20,
        ))[-1].output
        assert out["text"] == "draft v1"
        assert out["clean"] is True
