# module_harness/tests/test_stream_log.py
"""stream.log 流式落盘：writer 记录形状 + Module 内置接线（默认开）。"""

from __future__ import annotations

import json
import os

import pytest

from llm.client import LLMError, LLMResponse
from module_harness.config import HarnessConfig
from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist
from module_harness.query import read_stream
from module_harness.stream import StreamLogWriter, stream_log_path


def _read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestStreamLogWriter:
    def test_record_shape_and_order(self, tmp_path):
        w = StreamLogWriter(tmp_path / "d" / "stream.log")   # 目录不存在 → 懒建
        w.write({"type": "run_start", "pid": 123, "max_ticks": 100})
        w.write({"type": "token", "node": "A", "chunk": "你"})
        w.write({"type": "token", "node": "A", "chunk": "好"})
        w.close()
        w.close()   # 幂等
        recs = _read_records(tmp_path / "d" / "stream.log")
        assert [r["type"] for r in recs] == ["run_start", "token", "token"]
        assert recs[0]["pid"] == 123 and recs[0]["max_ticks"] == 100
        assert "".join(r["chunk"] for r in recs[1:]) == "你好"
        assert all(isinstance(r["ts"], float) and r["ts"] > 0 for r in recs)

    def test_event_records(self, tmp_path):
        w = StreamLogWriter(tmp_path / "stream.log")
        w.write({"type": "call_start", "node": "A", "model": "m", "prompt_chars": 3})
        w.write({"type": "call_end", "node": "A", "content_chars": 5, "finish_reason": "end_turn"})
        w.write({"type": "call_error", "node": "A", "reason": "boom", "failure_type": "infrastructure"})
        w.close()
        recs = _read_records(tmp_path / "stream.log")
        assert [r["type"] for r in recs] == ["call_start", "call_end", "call_error"]
        assert recs[2]["failure_type"] == "infrastructure"

    def test_write_failure_logged_not_raised(self, tmp_path, caplog):
        # 父路径是文件 → open 必败；写失败仅 log 不抛（观测不阻断运行）
        (tmp_path / "f").write_text("x", encoding="utf-8")
        w = StreamLogWriter(tmp_path / "f" / "s.log")
        w.write({"type": "run_start", "pid": 1, "max_ticks": 1})
        w.close()


class _StreamingLLM:
    """流式 fake：先经 on_token 发 chunk 再返回（走 harness 流式通道）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    async def complete(self, *, prompt, on_token=None, **kw):
        for c in self._chunks:
            on_token(c)
        return LLMResponse(content='{"ok": true}')


class _ErrorLLM:
    async def complete(self, *, prompt, on_token=None, **kw):
        raise LLMError("连接超时")


def _harness_module(llm, tmp_path, monkeypatch, module_id="mod_stream", **kw):
    monkeypatch.chdir(tmp_path)
    reg = HarnessRegistry(llm_client=llm, event_bus=EventBus())
    reg.harness("probe", HarnessConfig(prompt_core="x={spec}"))
    return Module(
        spec={"x": 1},
        tasklist=Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="probe",
                                       inputs={"spec": "{spec}"})},
            flow="[A]",
        ),
        llm_client=llm,
        registry=reg,
        review_harness=None,
        module_id=module_id,
        **kw,
    )


class TestModuleWiring:
    @pytest.mark.asyncio
    async def test_full_chain_records(self, tmp_path, monkeypatch):
        llm = _StreamingLLM(["你", "好"])
        mod = _harness_module(llm, tmp_path, monkeypatch)
        await mod.run()
        recs = _read_records(stream_log_path("mod_stream", tmp_path))
        types = [r["type"] for r in recs]
        assert types[0] == "run_start"
        assert types.count("run_start") == 1
        assert recs[0]["max_ticks"] == 100 and recs[0]["pid"] == os.getpid()
        assert {"call_start", "token", "call_end"} <= set(types)
        tokens = [r for r in recs if r["type"] == "token"]
        assert "".join(t["chunk"] for t in tokens) == "你好"
        end = next(r for r in recs if r["type"] == "call_end")
        assert end["node"] == "A"
        assert end["content_chars"] == len('{"ok": true}')

    @pytest.mark.asyncio
    async def test_llm_error_writes_call_error(self, tmp_path, monkeypatch):
        # LLMError 在 harness body 内被捕获 → Failure(infrastructure) → 引擎
        # ABORTED → run() 正常返回（不抛），phase 落 aborted
        mod = _harness_module(_ErrorLLM(), tmp_path, monkeypatch)
        await mod.run()
        st = json.loads(
            (tmp_path / ".specmodule" / "runs" / "mod_stream" / "status.json")
            .read_text(encoding="utf-8")
        )
        assert st["phase"] == "aborted"
        recs = _read_records(stream_log_path("mod_stream", tmp_path))
        err = next(r for r in recs if r["type"] == "call_error")
        assert err["reason"] == "连接超时"
        assert err["failure_type"] == "infrastructure"

    @pytest.mark.asyncio
    async def test_stream_log_disabled(self, tmp_path, monkeypatch):
        mod = _harness_module(_StreamingLLM(["a"]), tmp_path, monkeypatch,
                              stream_log=False)
        await mod.run()
        assert not stream_log_path("mod_stream", tmp_path).exists()

    @pytest.mark.asyncio
    async def test_two_executions_append(self, tmp_path, monkeypatch):
        llm = _StreamingLLM(["x"])
        mod = _harness_module(llm, tmp_path, monkeypatch)
        await mod.run(max_ticks=1)
        mod2 = _harness_module(llm, tmp_path, monkeypatch)
        await mod2.resume()
        recs = _read_records(stream_log_path("mod_stream", tmp_path))
        starts = [r for r in recs if r["type"] == "run_start"]
        assert len(starts) == 2
        assert starts[0]["max_ticks"] == 1 and starts[1]["max_ticks"] == 100

    @pytest.mark.asyncio
    async def test_writer_detached_on_exception(self, tmp_path, monkeypatch):
        class _BoomLLM:
            async def complete(self, *, prompt, on_token=None, **kw):
                raise RuntimeError("boom")

        mod = _harness_module(_BoomLLM(), tmp_path, monkeypatch)
        with pytest.raises(RuntimeError):
            await mod.run()
        # finally 已摘当前 writer；已落盘记录完整可读
        assert mod._stream_writer is None
        recs = _read_records(stream_log_path("mod_stream", tmp_path))
        assert recs[0]["type"] == "run_start"

class TestReadStream:
    def _write_log(self, tmp_path, lines, run_id="r1"):
        p = stream_log_path(run_id, tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(lines), encoding="utf-8")
        return p

    def test_missing_returns_none(self, tmp_path):
        assert read_stream("ghost", base_dir=tmp_path) is None

    def test_incremental_with_off(self, tmp_path):
        self._write_log(tmp_path, [
            json.dumps({"type": "run_start", "pid": 1}) + "\n",
            json.dumps({"type": "token", "node": "A", "chunk": "hi"}) + "\n",
        ])
        r1 = read_stream("r1", base_dir=tmp_path)
        assert [r["type"] for r in r1["records"]] == ["run_start", "token"]
        assert r1["next_offset"] == r1["file_size"]
        assert r1["records"][0]["off"] == 0
        assert r1["records"][1]["off"] > r1["records"][0]["off"]
        r2 = read_stream("r1", offset=r1["next_offset"], base_dir=tmp_path)
        assert r2["records"] == []

    def test_partial_line_not_consumed(self, tmp_path):
        self._write_log(tmp_path, [
            json.dumps({"type": "run_start"}) + "\n",
            '{"type": "tok',
        ])
        r = read_stream("r1", base_dir=tmp_path)
        assert [x["type"] for x in r["records"]] == ["run_start"]
        # 半行 + 补齐串须拼成完整 json：{"type": "toke" 少一个 n，故补 'en"'
        with stream_log_path("r1", tmp_path).open("a", encoding="utf-8") as fh:
            fh.write('en", "node": "A"}\n')
        r2 = read_stream("r1", offset=r["next_offset"], base_dir=tmp_path)
        assert r2["records"][0]["type"] == "token"
        assert r2["records"][0]["node"] == "A"

    def test_corrupt_line_skipped(self, tmp_path):
        self._write_log(tmp_path, [
            json.dumps({"type": "run_start"}) + "\n",
            "not json{{\n",
            json.dumps({"type": "token", "node": "A", "chunk": "x"}) + "\n",
        ])
        r = read_stream("r1", base_dir=tmp_path)
        assert [x["type"] for x in r["records"]] == ["run_start", "token"]

    def test_offset_beyond_size_clamped(self, tmp_path):
        """offset 超过文件大小（文件被替换/重建）→ 钳回 0 自愈。"""
        self._write_log(tmp_path, [json.dumps({"type": "run_start"}) + "\n"])
        r = read_stream("r1", offset=10_000, base_dir=tmp_path)
        assert [x["type"] for x in r["records"]] == ["run_start"]
