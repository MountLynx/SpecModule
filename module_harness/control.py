# module_harness/control.py
"""跨进程运行控制——控制文件协议（cancel/pause/unpause）。

status.json 的反向通道：status.json 把运行状态带出运行进程，control.json
把控制请求带进运行进程——文件即协议，任何消费端（CLI/Web/TUI）可写，
运行进程在 tick 边界协作式消费。零依赖、不触碰运行状态的单写者规则
（status.json 仍只由运行进程写）。

协议：``.specmodule/runs/<run_id>/control.json``，单发一次性请求::

    {"action": "cancel" | "pause" | "unpause", "reason": str|null,
     "requested_at": float}

- 写方：``request_control`` 原子写（tmp + os.replace，与 status.json 同款）。
- 读方：``control_tick_start`` 工厂返回 async on_tick_start 回调，注册到
  runner 后在每个 tick 边界检查：
  - cancel → ``runner.cancel(reason)``（引擎无 tick 中途终止——当前 tick
    内已开始的 firing 会跑完，下一 tick 前停，即取消有一 tick 延迟）；
  - pause → 在 tick 边界挂起（即将 fire 的 tick 不启动、tick 计数不前进），
    轮询等待 unpause 或 cancel；挂起期间保留文件——文件本身就是"暂停中"
    状态，监控方 ``read_control`` 可读；
  - unpause → 释放挂起；无挂起时出现（一次性写不会在非暂停期发生）→ 忽略。
- 消费即删（delete-on-consume）：动作执行后删除文件，防重放。
- 新执行清场：``Module.run()/resume()`` 开始时 ``clear_control``——启动新
  执行即作废陈旧请求（进程崩溃残留的 pause 不会拖住下一次运行）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "clear_control",
    "control_path",
    "control_tick_start",
    "read_control",
    "request_control",
]

#: 合法控制动作。
ACTIONS: tuple[str, ...] = ("cancel", "pause", "unpause")

#: pause 挂起期间的轮询间隔（秒）。
POLL_SECONDS = 0.5


def control_path(module_id: str, base_dir: Path | None = None) -> Path:
    """控制文件路径：``<base_dir>/.specmodule/runs/<run_id>/control.json``。"""
    return (base_dir or Path.cwd()) / ".specmodule" / "runs" / module_id / "control.json"


def read_control(module_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    """读当前控制请求；无请求 / 文件损坏 / action 非法 → None（容错读）。"""
    path = control_path(module_id, base_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("control.json 损坏或不可读（忽略）: %s", path)
        return None
    if not isinstance(data, dict) or data.get("action") not in ACTIONS:
        log.warning("control.json 内容非法（忽略）: %s", path)
        return None
    return data


def request_control(
    module_id: str,
    action: str,
    *,
    reason: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """写入控制请求（原子写）。action 非法 → ValueError。返回写入的请求。"""
    if action not in ACTIONS:
        raise ValueError(
            f"未知控制动作: {action!r}（可用: {'/'.join(ACTIONS)}）"
        )
    req: dict[str, Any] = {
        "action": action,
        "reason": reason,
        "requested_at": time.time(),
    }
    path = control_path(module_id, base_dir)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.exception("写 control.json 失败: %s", path)
        raise
    return req


def clear_control(module_id: str, *, base_dir: Path | None = None) -> None:
    """删除控制文件（消费/清场）。缺失不报错；删除失败仅 log。"""
    try:
        control_path(module_id, base_dir).unlink(missing_ok=True)
    except OSError:
        log.exception("删除 control.json 失败（忽略）")


def control_tick_start(
    runner: Any,
    module_id: str,
    *,
    base_dir: Path | None = None,
    poll: float = POLL_SECONDS,
):
    """工厂：返回注册到 ``runner.on_tick_start`` 的 async 控制回调。

    ``runner`` 需提供 ``cancel(reason)``（tickflow Runner/AsyncRunner 均有）；
    由 Module 在构建 runner 后注册（闭包捕获），CLI run/resume 默认接线。
    """
    async def _on_tick_start(tick: int, fireable: list[str]) -> None:
        req = read_control(module_id, base_dir=base_dir)
        if req is None:
            return
        action = req["action"]
        if action == "cancel":
            runner.cancel(req.get("reason") or "cancelled")
            clear_control(module_id, base_dir=base_dir)
            return
        if action != "pause":
            return
        # pause：tick 边界挂起；文件保留 = "暂停中"状态（监控方 read_control 可读）
        while True:
            await asyncio.sleep(poll)
            nxt = read_control(module_id, base_dir=base_dir)
            if nxt is None:
                # 挂起期间文件被外部删除（人工清理）→ 视为释放
                return
            if nxt["action"] == "cancel":
                runner.cancel(nxt.get("reason") or "cancelled")
                clear_control(module_id, base_dir=base_dir)
                return
            if nxt["action"] == "unpause":
                clear_control(module_id, base_dir=base_dir)
                return

    return _on_tick_start
