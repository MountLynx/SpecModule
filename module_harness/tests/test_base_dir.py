"""Module(base_dir=...)：持久化落盘位置可控（MCP 服务器 --base-dir 依赖）。"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.client import LLMResponse
from module_harness.config import HarnessConfig
from module_harness.graph_builder import TasklistTranslator
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist


def _make_module(monkeypatch, work_dir, **kw):
    """任务书直通 + 零 LLM：mock client + review_harness=None。"""
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock(return_value=LLMResponse(content='{"ok": true}'))
    reg = HarnessRegistry(llm_client=mock_llm)
    reg.harness("probe", HarnessConfig(prompt_core="x={spec}"))
    tl = Tasklist(
        tasks={"A": TaskDefinition(type="harness", harness="probe",
                                   inputs={"spec": "{spec}"})},
        flow="[A]",
    )
    return Module(
        spec={"a": 1}, tasklist=tl, llm_client=mock_llm, registry=reg,
        module_id="mod_base", review_harness=None, **kw,
    )


@pytest.mark.asyncio
async def test_base_dir_persists_under_base(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")   # cwd 故意指向别处
    mod = _make_module(monkeypatch, work, base_dir=work)
    await mod.run(max_ticks=5)
    run_dir = work / ".specmodule" / "runs" / "mod_base"
    assert (run_dir / "run.sqlite").exists()
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "done"
    assert not (tmp_path / "elsewhere" / ".specmodule").exists()


@pytest.mark.asyncio
async def test_default_stays_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mod = _make_module(monkeypatch, tmp_path)
    await mod.run(max_ticks=5)
    assert (tmp_path / ".specmodule" / "runs" / "mod_base" / "status.json").exists()
