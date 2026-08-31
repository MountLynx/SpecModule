# module_harness/stream.py
"""LLM 流式输出落盘——stream.log（跨进程流式观测通道）。

Harness 每个LLM chunk 经 EventBus 发 ``LlmToken``，但 EventBus 是进程内的——
独立消费进程（Web/TUI）看不到。本模块把流式事件序列化为 JSONL 追加写
``.specmodule/runs/<run_id>/stream.log``（与 status.json/run.sqlite 同目录），
跨进程可读。写失败仅 log 不抛（观测不阻断运行，同 _write_phase 哲学）。

记录格式（``ts`` 由写入方统一打 wall-clock——harness 事件的 timestamp 是
``time.monotonic()``，进程本地时钟，不落盘、不跨进程比较）::

    {"type": "run_start",  "ts", "pid", "max_ticks"}
    {"type": "call_start", "ts", "node", "model", "prompt_chars"}
    {"type": "token",      "ts", "node", "chunk"}
    {"type": "call_end",   "ts", "node", "content_chars", "finish_reason"}
    {"type": "call_error", "ts", "node", "reason", "failure_type"}

append-only：每次执行以 ``run_start`` 开边界，不截断旧执行（崩溃残留可事后
查看）。不带 tick 字段——harness 事件恒为 tick=0，落盘假数据不如不写。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["StreamLogWriter", "stream_log_path"]


def stream_log_path(module_id: str, base_dir: Path | None = None) -> Path:
    """``<base_dir>/.specmodule/runs/<module_id>/stream.log``。"""
    return (base_dir or Path.cwd()) / ".specmodule" / "runs" / module_id / "stream.log"


class StreamLogWriter:
    """stream.log 追加写器：懒开句柄、逐记录 flush（无 fsync）、close 幂等。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: Any = None

    def write(self, record: dict[str, Any]) -> None:
        """单条 JSONL 记录落盘（ts 在此统一打 wall-clock）。失败仅 log。"""
        record = {"ts": time.time(), **record}
        try:
            if self._fh is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", encoding="utf-8")
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        except OSError:
            log.exception("写 stream.log 失败（不阻断运行）: %s", self._path)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                log.exception("关闭 stream.log 失败: %s", self._path)
            self._fh = None
