# module_harness/cli
"""CLI 层：命令行入口、模块发现/装载、脚手架、command 节点。

``main`` 惰性再导出（PEP 562）：core.registry import 本包的 command 节点时，
不得经包 __init__ 急切拉入 cli.cli 的重 import 链（会与
infra.checkpoint → orchestrate.graph_builder → core.registry 成环）。
"""

from typing import Any

__all__ = ["main"]


def __getattr__(name: str) -> Any:
    if name == "main":
        from module_harness.cli.cli import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
