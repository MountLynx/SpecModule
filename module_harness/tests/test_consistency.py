# module_harness/tests/test_consistency.py
"""一致性审核：ConsistencyReport / REVIEW_HARNESS_CONFIG / ConsistencyReviewer。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMError, LLMResponse
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.consistency import (
    ConsistencyError,
    ConsistencyReport,
    ConsistencyReviewer,
    REVIEW_HARNESS_CONFIG,
    register_review_harness,
)
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _spec() -> Spec:
    return Spec({"source_text": "Hello", "target": "中文"})


def _tasklist() -> Tasklist:
    return Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate", inputs={"text": "source_text"},
            ),
        },
        flow="[A]",
    )


class TestConsistencyModels:
    def test_report_fields(self):
        report = ConsistencyReport(consistent=False, suggestions="缺少覆盖", raw="{}")
        assert report.consistent is False
        assert report.suggestions == "缺少覆盖"

    def test_error_carries_report(self):
        report = ConsistencyReport(consistent=False, suggestions="flow 有死路", raw="{}")
        err = ConsistencyError(report)
        assert err.report is report
        assert "flow 有死路" in str(err)


class TestRegisterReviewHarness:
    def test_registers_builtin(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_review_harness(reg)
        assert reg.is_harness("spec_tasklist_review")
        cfg = reg.harness_config("spec_tasklist_review")
        assert cfg.output_format is not None
        assert cfg.output_format.type == "json_object"

    def test_custom_name(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_review_harness(reg, name="my_review")
        assert reg.is_harness("my_review")
        assert not reg.is_harness("spec_tasklist_review")


class TestConsistencyReviewer:
    @pytest.fixture
    def reg(self, mock_llm):
        r = HarnessRegistry(llm_client=mock_llm)
        register_review_harness(r)
        r.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
        return r

    @pytest.mark.asyncio
    async def test_review_pass(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        report = await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        assert report.consistent is True
        assert report.suggestions == ""

    @pytest.mark.asyncio
    async def test_review_fail_returns_report(self, mock_llm, reg):
        """consistent=false 是合法审核结果：reviewer 返回 report，由 Module 决定阻塞。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": false, "suggestions": "缺少目标覆盖"}',
            usage={}, finish_reason="end_turn",
        )
        report = await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        assert report.consistent is False
        assert "缺少目标覆盖" in report.suggestions

    @pytest.mark.asyncio
    async def test_review_non_json_raises(self, mock_llm, reg):
        """内置审核 harness 带 json_object 约束：非 JSON 在 OutputValidator 层转 Failure。"""
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all", usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="审核 harness 返回 Failure"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_str_path_non_json_raises(self, mock_llm):
        """text 输出格式的审核 harness：str 结果走 json.loads 路径。"""
        reg = HarnessRegistry(llm_client=mock_llm)
        reg.harness("review_text", HarnessConfig(
            prompt_core="审核：{spec} / {tasklist}",
            output_format=OutputFormat(type="text"),
        ))
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all", usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="不是合法 JSON"):
            await ConsistencyReviewer(
                reg, harness_name="review_text"
            ).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_missing_suggestions_raises(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true}', usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="suggestions"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_wrong_consistent_type_raises(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": "yes", "suggestions": "x"}',
            usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="consistent"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_harness_not_registered(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        with pytest.raises(ValueError, match="spec_tasklist_review"):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_llm_error_blocks(self, mock_llm, reg):
        mock_llm.complete.side_effect = LLMError("API 不可用")
        with pytest.raises(ValueError):
            await ConsistencyReviewer(reg).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_prompt_injects_spec_and_tasklist(self, mock_llm, reg):
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert "Hello" in prompt          # spec 数据注入
        assert "source_text" in prompt    # tasklist 字段注入

    @pytest.mark.asyncio
    async def test_review_str_path_non_dict_raises(self, mock_llm):
        """合法 JSON 但非对象（如数组）→ ValueError 而非 AttributeError。"""
        reg = HarnessRegistry(llm_client=mock_llm)
        reg.harness("review_text", HarnessConfig(
            prompt_core="审核：{spec} / {tasklist}",
            output_format=OutputFormat(type="text"),
        ))
        mock_llm.complete.return_value = LLMResponse(
            content='[1, 2]', usage={}, finish_reason="end_turn",
        )
        with pytest.raises(ValueError, match="必须是 JSON 对象"):
            await ConsistencyReviewer(
                reg, harness_name="review_text"
            ).review(_spec(), _tasklist())

    @pytest.mark.asyncio
    async def test_review_raw_is_literal_llm_output(self, mock_llm, reg):
        """raw 记录 LLM 原始输出（含 markdown 围栏），而非提取后的 dict。"""
        raw_text = '```json\n{"consistent": true, "suggestions": ""}\n```'
        mock_llm.complete.return_value = LLMResponse(
            content=raw_text, usage={}, finish_reason="end_turn",
        )
        report = await ConsistencyReviewer(reg).review(_spec(), _tasklist())
        assert report.raw == raw_text
        assert report.consistent is True
