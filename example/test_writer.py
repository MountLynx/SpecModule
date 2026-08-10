"""academic_writer 模块 mock 测试（无 key 可跑，MagicMock 逐节点预设输出）。

覆盖两种使用方式（模板通道，template_name 选择）：
- mode="submodule"：事实审阅 loop 以 submodule 节点复用（黑盒嵌入）
- mode="detailed"：loop 内联展开到主图（详细模式，全程可审计）
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse

from example.academic_writer import (
    academic_tasklist,
    detailed_tasklist,
    run_writer,
)


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage={}, finish_reason="end_turn")


RAW = (
    "灵感草稿：我们 propose 一个方法，方法很好。它 can 提高 accuracy，"
    "accuracy 提升明显，非常 impressive。"
)


class TestAcademicWriter:
    def test_tasklist_has_two_submodule_nodes(self):
        """Loop1/Loop2 两个 submodule 节点引用同一 fact_review_loop（结构校验）。"""
        subs = [t for t in academic_tasklist.tasks.values() if t.type == "submodule"]
        assert len(subs) == 2
        assert all(t.submodule == "fact_review_loop" for t in subs)

    def _run_with(self, mock_llm, review_plan: dict[str, list[str]]):
        """review_plan：{"loop1": [审阅响应...], "loop2": [审阅响应...]}
        按阶段分发审阅；其余节点按关键词返回预设输出。"""
        review_calls = {"loop1": 0, "loop2": 0}

        def _next_review(stage: str):
            bodies = review_plan[stage]
            body = bodies[min(review_calls[stage], len(bodies) - 1)]
            review_calls[stage] += 1
            return _resp(body)

        async def side_effect(**kwargs):
            prompt = kwargs.get("prompt", "")
            # 分发键必须是各 prompt_core 独有引导短语——不能选用户文本
            # 可能出现的词（如"灵感草稿"），因为 {original} 占位符会把
            # raw_text 渲染进 Review/Finalize 的 prompt。
            # 文本节点（organize/seed/fix/polish）为 text 类型 → 返回纯文本；
            # 仅 finalize/review 为 json_object → 返回 JSON。
            if "整理成逻辑通顺的英文文段" in prompt:
                return _resp("organized draft")
            if "原样转发" in prompt:
                # 子模块 Seed 的 draft 是父节点输出 —— 原样转发
                return _resp("child draft")
            if "修复者" in prompt:
                return _resp("child draft fixed")
            if "学术英语写作规范" in prompt:
                return _resp("polished draft")
            if "整合输出最终版本" in prompt:
                return _resp('{"text": "final version", "notes": "将被动语态改为主动语态"}')
            # 审阅：按调用顺序区分 loop1 / loop2
            if "逐句对比" in prompt:
                if review_calls["loop1"] < len(review_plan["loop1"]):
                    return _next_review("loop1")
                return _next_review("loop2")
            return _resp('{"issues": [], "clean": true}')

        mock_llm.complete.side_effect = side_effect

    @pytest.mark.asyncio
    async def test_normal_path_two_variables(self, mock_llm):
        """两阶段均一次 clean → 输出 final_text + modification_notes 两变量；
        fact_review harness 被两个 submodule 节点各调用一次（同一 harness，两个节点）。"""
        self._run_with(mock_llm, review_plan={
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        out = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=50,
        ))[-1].output
        assert set(out) == {"final_text", "modification_notes"}
        assert out["final_text"] == "final version"
        notes = out["modification_notes"]
        assert "阶段 1 审阅" in notes and "阶段 2 审阅" in notes
        assert "通过（无事实问题）" in notes
        assert "被动语态改为主动语态" in notes  # finalize 的语言调整说明
        review_prompts = [
            c.kwargs.get("prompt", "") for c in mock_llm.complete.await_args_list
            if "逐句对比" in c.kwargs.get("prompt", "")
        ]
        assert len(review_prompts) == 2  # 同一 harness、两个节点各触发一次

    @pytest.mark.asyncio
    async def test_loop1_fix_path(self, mock_llm):
        """阶段 1 loop 先 issues 后 clean → notes 记 2 轮、结论通过。"""
        self._run_with(mock_llm, review_plan={
            "loop1": [
                '{"issues": [{"type": "omission", "detail": "缺实验细节", '
                '"quote_original": "…", "quote_draft": "…"}], "clean": false}',
                '{"issues": [], "clean": true}',
            ],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        out = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=50,
        ))[-1].output
        notes = out["modification_notes"]
        assert "事实审阅轮数：2" in notes
        assert "通过（无事实问题）" in notes

    @pytest.mark.asyncio
    async def test_loop2_max_attempts_notes_remaining(self, mock_llm):
        """阶段 2 loop 恒 issues → 达上限退出，notes 含遗留问题明细。"""
        self._run_with(mock_llm, review_plan={
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": [
                '{"issues": [{"type": "hallucination", "detail": "杜撰引用文献", '
                '"quote_original": "无", "quote_draft": "[1]"}], "clean": false}',
            ],
        })
        out = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=80,
        ))[-1].output
        notes = out["modification_notes"]
        assert "达上限未清" in notes
        assert "杜撰引用文献" in notes  # 遗留问题逐条列出


class TestDetailedMode:
    """详细模式：loop 内联展开到主图（不用 submodule），全程可审计。"""

    # 复用 TestAcademicWriter 的 mock 分发帮助（self 未被使用，跨类共用）
    _run_with = TestAcademicWriter._run_with

    def test_detailed_tasklist_inline_no_submodule(self):
        """详细模式 tasklist：无 submodule 节点；两组 loop 节点内联展开。"""
        tasks = detailed_tasklist.tasks
        assert not any(t.type == "submodule" for t in tasks.values())
        for key in ("Seed1", "Merge1", "Review1", "Fix1", "Exit1",
                    "Seed2", "Merge2", "Review2", "Fix2", "Exit2"):
            assert key in tasks

    @pytest.mark.asyncio
    async def test_detailed_nodes_visible_in_firings(self, mock_llm):
        """详细模式核心价值：内联节点进入主图审计记录（submodule 黑盒做不到）；
        修复路径下 Fix1 触发、Loop1 两轮收敛。"""
        self._run_with(mock_llm, review_plan={
            "loop1": [
                '{"issues": [{"type": "omission", "detail": "缺结论", '
                '"quote_original": "…", "quote_draft": "…"}], "clean": false}',
                '{"issues": [], "clean": true}',
            ],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        firings = await run_writer(
            {"raw_text": RAW}, mode="detailed",
            llm_client=mock_llm, persist=False, max_ticks=80,
        )
        fired = {f.node for f in firings}
        assert {"Organize", "Polish", "Finalize", "Report"} <= fired
        assert {"Seed1", "Merge1", "Review1", "Fix1", "Exit1"} <= fired
        assert {"Seed2", "Merge2", "Review2", "Exit2"} <= fired
        assert any(f.node == "Fix1" for f in firings)  # 修复被触发过
        assert any(f.node == "Review1" and f.output.get("clean") for f in firings)

    @pytest.mark.asyncio
    async def test_detailed_normal_path(self, mock_llm):
        """详细模式正常路径：输出 final_text + modification_notes 两变量。"""
        self._run_with(mock_llm, review_plan={
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        out = (await run_writer(
            {"raw_text": RAW}, mode="detailed",
            llm_client=mock_llm, persist=False, max_ticks=80,
        ))[-1].output
        assert set(out) == {"final_text", "modification_notes"}
        assert out["final_text"] == "final version"
        notes = out["modification_notes"]
        assert "阶段 1 审阅" in notes and "阶段 2 审阅" in notes
        assert "通过（无事实问题）" in notes

    @pytest.mark.asyncio
    async def test_detailed_loop1_fix_path(self, mock_llm):
        """详细模式阶段 1 修复路径：notes 记 2 轮、结论通过。"""
        self._run_with(mock_llm, review_plan={
            "loop1": [
                '{"issues": [{"type": "omission", "detail": "缺实验细节", '
                '"quote_original": "…", "quote_draft": "…"}], "clean": false}',
                '{"issues": [], "clean": true}',
            ],
            "loop2": ['{"issues": [], "clean": true}'],
        })
        out = (await run_writer(
            {"raw_text": RAW}, mode="detailed",
            llm_client=mock_llm, persist=False, max_ticks=80,
        ))[-1].output
        notes = out["modification_notes"]
        assert "事实审阅轮数：2" in notes
        assert "通过（无事实问题）" in notes

    @pytest.mark.asyncio
    async def test_detailed_loop2_max_attempts(self, mock_llm):
        """详细模式阶段 2 达上限：notes 含遗留问题明细。"""
        self._run_with(mock_llm, review_plan={
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": [
                '{"issues": [{"type": "hallucination", "detail": "杜撰引用文献", '
                '"quote_original": "无", "quote_draft": "[1]"}], "clean": false}',
            ],
        })
        out = (await run_writer(
            {"raw_text": RAW}, mode="detailed",
            llm_client=mock_llm, persist=False, max_ticks=80,
        ))[-1].output
        notes = out["modification_notes"]
        assert "达上限未清" in notes
        assert "杜撰引用文献" in notes

    @pytest.mark.asyncio
    async def test_modes_equivalent_output(self, mock_llm):
        """同一 module 两种使用方式（submodule / detailed）在同 mock 下输出等价。"""
        plan = {
            "loop1": ['{"issues": [], "clean": true}'],
            "loop2": ['{"issues": [], "clean": true}'],
        }
        self._run_with(mock_llm, review_plan=plan)
        out_sub = (await run_writer(
            {"raw_text": RAW}, llm_client=mock_llm, persist=False, max_ticks=50,
        ))[-1].output
        self._run_with(mock_llm, review_plan=plan)
        out_det = (await run_writer(
            {"raw_text": RAW}, mode="detailed",
            llm_client=mock_llm, persist=False, max_ticks=80,
        ))[-1].output
        assert out_sub["final_text"] == out_det["final_text"]
        assert out_sub["modification_notes"] == out_det["modification_notes"]
