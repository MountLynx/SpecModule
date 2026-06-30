# module_harness/command.py
"""Command 节点 — 一行 shell 命令即一个 tickflow body。"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from tickflow import Failure
from tickflow.views import DictView

from .events import (
    EventBus,
    CommandStarted,
    CommandCompleted,
    CommandFailed,
)


@dataclass
class CommandConfig:
    """shell 命令配置。"""

    command: str                        # shell 命令字符串
    timeout: float = 60.0               # 超时秒数
    cwd: str | None = None              # 工作目录
    env: dict[str, str] | None = None   # 额外环境变量
    capture_output: bool = True
    shell: bool = True


class Command:
    """持有 CommandConfig + EventBus，类似 Harness 类。"""

    def __init__(self, config: CommandConfig, event_bus: EventBus) -> None:
        self.config = config
        self.bus = event_bus

    def build_body(self, *, timeout: float | None = None, cwd: str | None = None):
        """返回一个 sync body callable。

        body 执行流程：
          1. emit CommandStarted
          2. subprocess.run(command, ...)
          3. 成功 → emit CommandCompleted → return {"stdout", "stderr", "returncode"}
          4. 异常 → emit CommandFailed → return Failure(type="llm")
        """
        config = self.config
        bus = self.bus
        final_timeout = timeout if timeout is not None else config.timeout
        final_cwd = cwd if cwd is not None else config.cwd

        def body(view: DictView):
            node = view.node
            bus.emit(CommandStarted(
                timestamp=time.monotonic(), node=node, tick=0,
            ))

            try:
                result = subprocess.run(
                    config.command,
                    shell=config.shell,
                    timeout=final_timeout,
                    cwd=final_cwd,
                    env=config.env,
                    capture_output=config.capture_output,
                    text=True,
                )
            except subprocess.TimeoutExpired as e:
                error_msg = f"命令超时 ({final_timeout}s): {e}"
                bus.emit(CommandFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    error=error_msg,
                ))
                return Failure(error_msg, type="llm")
            except Exception as e:
                error_msg = f"命令执行失败: {e}"
                bus.emit(CommandFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    error=error_msg,
                ))
                return Failure(error_msg, type="llm")

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            bus.emit(CommandCompleted(
                timestamp=time.monotonic(), node=node, tick=0,
                stdout=stdout, stderr=stderr, returncode=result.returncode,
            ))

            return {
                "stdout": stdout,
                "stderr": stderr,
                "returncode": result.returncode,
            }

        return body
