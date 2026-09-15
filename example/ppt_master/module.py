"""ppt_master — ppt-master Generate 主线的 SpecModule 复刻（单模板 generate）。

节点类型总原则（spec §5/§7）：LLM 节点 = script 包 call_harness（失败收据，
AND 不饿死）；确定性工具 = script 或 command（vendor 闭包）；门 = command +
verdict script + 守卫修复环（上限 2 轮，repair_node 内计数）。

本文件是注册方：翻译器引用的 15 个 script 名、4 个守卫名、6 个命令名
（docstring 命令表为唯一契约文本，见 translator.py 模块 docstring）全部
在此落地——命令名 → CommandConfig（run_tool.py + pid 信封路径字面量），
同进程注册 → 同 pid → 信封路径一致。
"""

from __future__ import annotations

import sys
from collections.abc import Coroutine
from typing import Any

from llm import LLMConfig, create_llm_client

from module_harness.cli.command import CommandConfig
from module_harness.core.registry import HarnessRegistry
from module_harness.infra.events import EventBus
from module_harness.model.module import Module
from module_harness.model.spec import TaskDefinition, Tasklist
from module_harness.model.translator import TemplateLoader

from . import llm_nodes, tools_nodes, workspace
from .translator import build_generate_tasklist, tl_generate

_RUN_TOOL = workspace.MODULE_DIR / "tools" / "run_tool.py"


def _run_tool_command(tool: str, *args: str, timeout: float = 300.0) -> CommandConfig:
    """vendor 工具 → CommandConfig（命令串静态嵌 pid 信封路径字面量）。

    形如 ``"{python}" "{run_tool.py}" --envelope "{信封}" --tool <tool>[ -- args]``。
    """
    command = (
        f'"{sys.executable}" "{_RUN_TOOL}" '
        f'--envelope "{workspace._envelope_path()}" --tool {tool}'
    )
    if args:
        command += " -- " + " ".join(args)
    return CommandConfig(command=command, timeout=timeout)


def _build_registry(
    llm_client: Any,
    event_bus: EventBus | None = None,
    max_repair_rounds: int = 2,
) -> HarnessRegistry:
    """组装完整 registry：翻译器 + LLM 节点 + 确定性节点 + 六命令 + 四守卫。

    LLM 节点用工厂闭包捕获 client/bus（namespace 隔离：同进程多 Module
    实例各建各的 registry，互不共享 body 状态）。
    """
    bus = event_bus or EventBus.null()
    reg = HarnessRegistry(llm_client=llm_client, event_bus=bus)

    # 翻译入口（Module.run 翻译期经 reg.get_body 取用）
    reg.script("tl_generate")(tl_generate)

    # LLM 节点（工厂闭包捕获 client）
    reg.script("plan_node")(llm_nodes.make_plan_node(llm_client, bus))
    reg.script("research_node")(llm_nodes.make_research_node(llm_client, bus))
    reg.script("page_node")(llm_nodes.make_page_node(llm_client, bus))
    reg.script("repair_node")(
        llm_nodes.make_repair_node(llm_client, bus, max_repair_rounds))
    reg.script("notes_node")(llm_nodes.make_notes_node(llm_client, bus))
    reg.script("image_acquire")(llm_nodes.make_image_node(llm_client, bus))

    # 确定性 script 节点
    reg.script("ingest")(tools_nodes.ingest)
    reg.script("init")(tools_nodes.init)
    reg.script("plan_validate")(tools_nodes.plan_validate)
    reg.script("calibrate")(tools_nodes.calibrate)
    reg.script("icon_sync_node")(tools_nodes.icon_sync_node)
    reg.script("image_readiness")(tools_nodes.image_readiness)
    reg.script("passthrough")(tools_nodes.passthrough)
    reg.script("early_verdict")(tools_nodes.make_gate_verdict("early"))
    reg.script("final_verdict")(tools_nodes.make_gate_verdict("final"))
    reg.script("ppt_report")(tools_nodes.report)

    # 命令节点（翻译器六个注册名 → vendor 工具，超时 ↔ docstring 命令表）
    reg.command("ppt_early_gate", _run_tool_command(
        "svg_quality_checker.py", "--stage early --canonical-authoring --json",
        timeout=600.0))
    reg.command("ppt_final_gate", _run_tool_command(
        "svg_quality_checker.py", "--stage final --canonical-authoring --json",
        timeout=900.0))
    reg.command("ppt_split_notes", _run_tool_command(
        "total_md_split.py", timeout=120.0))
    reg.command("ppt_finalize", _run_tool_command(
        "finalize_svg.py", timeout=600.0))
    reg.command("ppt_export", _run_tool_command(
        "svg_to_pptx.py", timeout=1200.0))
    reg.command("ppt_export_nonotes", _run_tool_command(
        "svg_to_pptx.py", "--no-notes", timeout=1200.0))

    # 守卫（读源任务 inputs 键 'gate'；文件复检，want_clean 分正反）
    reg.guard("early_issues", tools_nodes.make_guard("early", want_clean=False))
    reg.guard("early_clean", tools_nodes.make_guard("early", want_clean=True))
    reg.guard("final_errors", tools_nodes.make_guard("final", want_clean=False))
    reg.guard("final_clean", tools_nodes.make_guard("final", want_clean=True))
    return reg


def _generate_tasklist() -> Tasklist:
    """静态骨架（翻译器运行时会整体替换；模板声明需要合法 tasklist）。"""
    tasks, flow = build_generate_tasklist({
        "project": "scaffold",
        "source": {"kind": "files", "paths": ["x.md"]},
        "roster": [{"id": "p01", "title": "占位"}],
    })
    return Tasklist(
        tasks={k: TaskDefinition.from_dict(v) for k, v in tasks.items()},
        flow=flow,
    )


GENERATE_TEMPLATE: dict[str, Any] = {
    "name": "generate",
    "description": (
        "ppt-master Generate 主线复刻：源处理 → 规划（design_spec/spec_lock）"
        "→ 图像/图标 → 并行逐页 SVG → 早/终质量门（守卫修复环）→ 导出 pptx"
    ),
    "translation": {"type": "script", "script": "tl_generate"},
    "tasklist": _generate_tasklist().to_dict(),
}


def run_generate(
    spec: dict[str, Any],
    *,
    llm_client: Any = None,
    max_ticks: int = 400,
    persist: bool = False,
) -> Coroutine[Any, Any, list]:
    """编程 API（测试/嵌入用）：构造并运行 generate。

    llm_client 缺省从环境变量构建（LLMConfig.from_env）；传入 mock/脚本
    客户端即全链仿真（asyncio.run(run_generate(spec, llm_client=...))）。
    persist=False（缺省）：NullBackend 快速模式，零落盘零残留。
    """
    if llm_client is None:
        llm_client = create_llm_client(LLMConfig.from_env())
    loader = TemplateLoader()
    loader.register("generate", GENERATE_TEMPLATE)
    mod = Module(
        spec=spec,
        template_name="generate",
        template_loader=loader,
        llm_client=llm_client,
        registry=_build_registry(llm_client),
        review_harness=None,   # 固定流程模板，发布前已验证
        persist=persist,
        status_file=persist,
    )
    return mod.run(max_ticks=max_ticks)
