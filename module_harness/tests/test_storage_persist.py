"""Module 持久化开关测试（spec D9/D11）：persist 两态 + SubModule mode。"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from module_harness.infra.events import EventBus
from module_harness.model.module import Module
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.model.submodule import SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _script_reg(llm_client):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=EventBus())

    @reg.script("echo")
    def echo(view):
        return {"ok": True}

    return reg


def _script_tasklist():
    return Tasklist(
        tasks={"A": TaskDefinition(type="script", script="echo")},
        flow="[A]",
    )


def _run_db(root, module_id):
    return root / ".specmodule" / "runs" / module_id / "run.sqlite"


def _firing_count(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM firings").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_persist_default_creates_run_db(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = _script_reg(mock_llm)
    mod = Module(
        spec={"x": 1},
        tasklist=_script_tasklist(),
        llm_client=mock_llm,
        registry=reg,
        review_harness=None,
        module_id="persist_mod",
    )
    await mod.run(max_ticks=10)
    db = _run_db(tmp_path, "persist_mod")
    assert db.exists()
    assert _firing_count(db) >= 1


@pytest.mark.asyncio
async def test_persist_false_fast_mode_no_files(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = _script_reg(mock_llm)
    mod = Module(
        spec={"x": 1},
        tasklist=_script_tasklist(),
        llm_client=mock_llm,
        registry=reg,
        review_harness=None,
        module_id="fast_mod",
        persist=False,
        status_file=False,   # 关闭所有落盘：DB + status.json
        stream_log=False,    # + 流式落盘（第三个通道；默认开，需显式点名）
    )
    firings = await mod.run(max_ticks=10)
    assert len(firings) >= 1
    assert not (tmp_path / ".specmodule").exists()   # 快速模式零残留
    # 结果与持久模式一致
    reg2 = _script_reg(mock_llm)
    mod2 = Module(
        spec={"x": 1},
        tasklist=_script_tasklist(),
        llm_client=mock_llm,
        registry=reg2,
        review_harness=None,
        module_id="persist_mod2",
    )
    firings2 = await mod2.run(max_ticks=10)
    assert [(f.node, f.output) for f in firings] == \
           [(f.node, f.output) for f in firings2]


class Dig(SubModule):
    """固定 script tasklist 的轻量子模块（无 LLM 调用）。"""

    name = "dig"
    spec_schema = SpecSchema()
    tasklist = _script_tasklist()

    @script("echo")
    def echo(view):
        return {"ok": True}


@pytest.mark.asyncio
async def test_submodule_each_run_own_dir(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # audit=False（嵌入模式）同样落盘（D11），且每次 run 独立 run_id 目录
    await Dig(llm_client=mock_llm).run({"x": 1}, max_ticks=10)
    await Dig(llm_client=mock_llm).run({"x": 1}, max_ticks=10)
    runs_dir = tmp_path / ".specmodule" / "runs"
    dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(dirs) == 2
    for d in dirs:
        assert (d / "run.sqlite").exists()


class FastDig(Dig):
    mode = "fast"


@pytest.mark.asyncio
async def test_submodule_mode_fast_no_persist(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    await FastDig(llm_client=mock_llm).run({"x": 1}, max_ticks=10)
    assert not (tmp_path / ".specmodule").exists()
