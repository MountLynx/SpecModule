# module_harness/cli.py
"""specmodule CLI — 使用者层面入口（run / status / review）。

用法（无打包，与 ``python -m tickflow`` 一致）::

    python -m module_harness.cli run --module academic_writer --spec-file spec.json
    python -m module_harness.cli run --module academic_writer --spec '{"raw_text": "..."}'
    python -m module_harness.cli status [--run-id xxx] [--json]
    python -m module_harness.cli review [--run-id xxx] [--tick N] [--node xxx] [--failed] [--json]

场景归属：使用者层面（usage scenario）——第二级用户只写 spec/tasklist，
不写 Python。模块按名选择，入口注册由开发者在 ``modules/<name>.py`` 声明。
查询组合逻辑只 import 共享层（module_harness.query），绝不重实现。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm import LLMConfig, create_llm_client
from llm.client import LLMResponse

from .entry import discover_modules
from .events import EventBus
from .module import Module
from .query import (
    ReviewTimeline,
    build_timeline,
    filter_failed,
    filter_node,
    filter_tick,
    timeline_to_dict,
)
from .registry import HarnessRegistry
from .spec import Tasklist
from .status import query_run_status
from .translator import TemplateLoader


class MockLLMClient:
    """--mock 冒烟用：通用假客户端（免 key / 免网络）。

    output_format=json_object 时返回宽松合法 JSON（通过 validator）；text
    时返回占位文本。翻译通道（script 翻译器）不经 LLM，天然可用。
    """

    async def complete(self, **kwargs: Any) -> LLMResponse:
        fmt = kwargs.get("output_format") or {}
        if fmt.get("type") == "json_object":
            content = json.dumps(
                {"result": "mock output", "summary": "mock", "issues": []}
            )
        else:
            content = "mock output"
        return LLMResponse(content=content)


def _preview(value: Any, width: int = 80) -> str:
    """产出预览：JSON 序列化（失败回退 str）→ 单行 → 截断。"""
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width] + "…"


class RunDisplay:
    """三级实时显示（--verbose 1..3），由 runner hooks 驱动。

    L1（默认）：``tick 3  Organize ✓`` 一行；失败节点附加 error + 产出预览
    L2：L1 + 全部节点产出预览（约 80 字符截断）
    L3：完整详情块（tick 分隔线 + 输入摘要 + 完整产出 + error）

    回调均为 sync——tickflow ``_maybe_await`` 自动兼容；异常由 tickflow 吞。
    """

    _STATUS_ICON = {"ok": "✓", "failed": "✗", "aborted": "✗"}

    def __init__(self, verbose: int = 1, stream: Any = None) -> None:
        self.verbose = verbose
        self._out = stream or sys.stdout
        self.firings: list = []  # 全部 NodeState（结束汇总用）

    def hooks(self) -> dict:
        return {"on_tick_start": self._on_tick_start, "on_fire": self._on_fire}

    def _write(self, text: str) -> None:
        print(text, file=self._out)

    def _on_tick_start(self, tick: int, fireable: list[str]) -> None:
        if self.verbose >= 3:
            self._write(f"═══ tick {tick} ═══ fireable: {', '.join(fireable) or '—'}")

    def _on_fire(self, ns: Any) -> None:
        self.firings.append(ns)
        icon = self._STATUS_ICON.get(ns.status, ns.status)
        if self.verbose >= 3:
            self._write(f"── tick {ns.tick}  {ns.node}  [{ns.status}]")
            if ns.inputs:
                self._write(f"    inputs : {_preview(ns.inputs, width=200)}")
            if ns.output is not None:
                self._write(f"    output : {_preview(ns.output, width=2000)}")
            if ns.error:
                self._write(f"    error  : {ns.error}")
            return
        line = f"tick {ns.tick}  {ns.node:<24} {icon}"
        if ns.status != "ok":
            line += f"  error={ns.error}"
            if ns.output is not None:
                line += f"  output={_preview(ns.output)}"
        elif self.verbose >= 2:
            line += f"  output={_preview(ns.output)}"
        self._write(line)


def _resolve_spec(entry: Any, args: argparse.Namespace) -> dict[str, Any]:
    """spec 解析优先级：--spec > --spec-file > entry.default_spec。"""
    if args.spec:
        try:
            data = json.loads(args.spec)
        except json.JSONDecodeError as e:
            raise ValueError(f"--spec 不是合法 JSON: {e}")
        if not isinstance(data, dict):
            raise ValueError("--spec 必须是 JSON 对象")
        return data
    if args.spec_file:
        path = Path(args.spec_file)
        if not path.exists():
            raise ValueError(f"--spec-file 不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"--spec-file 不是合法 JSON: {e}")
        except OSError as e:
            raise ValueError(f"--spec-file 读取失败: {e}")
        if not isinstance(data, dict):
            raise ValueError("--spec-file 内容必须是 JSON 对象")
        return data
    if entry.default_spec is not None:
        return dict(entry.default_spec)
    raise ValueError("缺少 spec——请用 --spec（内联 JSON）或 --spec-file（文件）")


_TYPE_CHECKS = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


def _check_spec_schema(entry: Any, spec: dict[str, Any]) -> None:
    """可选的 spec_schema 校验：{字段: 类型名}，失败列出全部错误。"""
    if not entry.spec_schema:
        return
    errors: list[str] = []
    for field, type_name in entry.spec_schema.items():
        if field not in spec:
            errors.append(f"缺少字段 '{field}'（期望 {type_name}）")
            continue
        check = _TYPE_CHECKS.get(str(type_name))
        if check is not None and not isinstance(spec[field], check):
            errors.append(
                f"字段 '{field}' 应为 {type_name}，实际 {type(spec[field]).__name__}"
            )
    if errors:
        raise ValueError("spec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))


def _build_llm_client(mock: bool) -> Any:
    """--mock 用内置假客户端；否则从环境加载（失败提示 --mock）。"""
    if mock:
        return MockLLMClient()
    try:
        config = LLMConfig.from_env()
    except ValueError as e:
        raise ValueError(
            f"LLM 环境配置失败: {e}\n提示：可加 --mock 免 key 冒烟运行"
        )
    if not config.is_configured:
        raise ValueError(
            "LLM 未配置 API key——请配置 config.json + .env，或加 --mock 冒烟"
        )
    return create_llm_client(config)


def _load_tasklist(path_str: str) -> Tasklist:
    """加载 tasklist JSON 文件（{Tasks, Flow} 结构）。"""
    path = Path(path_str)
    if not path.exists():
        raise ValueError(f"--tasklist 文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"--tasklist 读取失败: {e}")
    return Tasklist.from_json(data)


def _print_available(modules: dict) -> None:
    """打印可用模块列表（模块未找到时）。"""
    if not modules:
        print(
            "modules_dir 中未发现任何模块（modules/<name>.py + entry 声明）",
            file=sys.stderr,
        )
        return
    print("可用模块:", file=sys.stderr)
    for name, entry in modules.items():
        print(f"  {name}: {entry.description}", file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    modules = discover_modules(Path(args.modules_dir))
    entry = modules.get(args.module)
    if entry is None:
        print(
            f"模块 '{args.module}' 未找到（modules_dir={args.modules_dir}）",
            file=sys.stderr,
        )
        _print_available(modules)
        return 1
    if args.tasklist and args.template:
        print("--tasklist 与 --template 互斥——只能二选一", file=sys.stderr)
        return 1
    # 预绑定：Ctrl+C 可能落在 display/mod 赋值前（pre-run 阶段），
    # 避免 KeyboardInterrupt 处理器引用未绑定变量抛 NameError（探索定稿已知缺陷）。
    display = None
    mod = None
    try:
        spec = _resolve_spec(entry, args)
        _check_spec_schema(entry, spec)
        llm_client = _build_llm_client(args.mock)
        template_name = args.template or entry.default_template
        if template_name is not None and template_name not in entry.templates:
            raise ValueError(
                f"模板 '{template_name}' 未注册——可用: {', '.join(entry.templates)}"
            )
        loader = TemplateLoader()
        for name, data in entry.templates.items():
            loader.register(name, data)
        event_bus = EventBus()
        if entry.build_registry is not None:
            registry = entry.build_registry(llm_client, template_name, event_bus)
        else:
            registry = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)
        display = RunDisplay(args.verbose)
        mod = Module(
            spec=spec,
            template_name=template_name,
            tasklist=_load_tasklist(args.tasklist) if args.tasklist else None,
            llm_client=llm_client,
            event_bus=event_bus,
            template_loader=loader,
            module_id=args.run_id or args.module,
            registry=registry,
            review_harness=entry.review_harness,
            modules=entry.submodules,
            hooks=display.hooks(),
        )
        asyncio.run(mod.run(max_ticks=args.max_ticks))
    except KeyboardInterrupt:
        n = len(display.firings) if display is not None else 0
        run_id = mod.module_id if mod is not None else (args.run_id or args.module)
        print(
            f"\n已中断：已执行 {n} 次节点 firing。"
            f"运行数据已落盘 .specmodule/runs/{run_id}/（status/review 可查）",
            file=sys.stderr,
        )
        return 2
    except (ValueError, ModuleNotFoundError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    # 结束汇总（display/mod 此处必已赋值）
    assert display is not None and mod is not None
    print(f"\n运行完成: module={entry.name} run_id={mod.module_id}")
    print(f"共 {len(display.firings)} 次节点 firing")
    by_node: dict[str, Any] = {
        ns.node: ns.output for ns in display.firings if ns.status == "ok"
    }
    if by_node:
        print("节点最新输出摘要:")
        for node, out in by_node.items():
            print(f"  {node}: {_preview(out)}")
    return 0


def _latest_run_id() -> str | None:
    """扫描 .specmodule/runs/ 取最新修改的子目录名（status/review 缺省）。"""
    runs = Path.cwd() / ".specmodule" / "runs"
    if not runs.is_dir():
        return None
    try:
        dirs = [d for d in runs.iterdir() if d.is_dir()]
    except OSError:
        return None
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime).name


def _cmd_status(args: argparse.Namespace) -> int:
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    st = query_run_status(run_id)
    if st is None:
        print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(asdict(st), ensure_ascii=False, indent=2))
        return 0
    line = f"模块 {st.module_id}: phase={st.phase}"
    if st.tick is not None:
        line += f" tick={st.tick}"
    print(line)
    if st.status:
        print(f"runner: {st.status}")
    if st.fired:
        print(f"本 tick fired: {', '.join(st.fired)}")
    if st.error:
        print(f"error: {st.error}")
    return 0


def _render_timeline(timeline: ReviewTimeline, show_outputs: bool = False) -> None:
    """按 tick 分组文本时间线（失败节点高亮 + error 详情）。

    ``show_outputs``（--tick/--node 过滤时）对每条 entry 附加产出预览。
    """
    if not timeline.entries:
        print("（空时间线——无节点 firing 记录）")
        return
    ticks: dict[int, list] = {}
    for e in timeline.entries:
        ticks.setdefault(e.tick, []).append(e)
    for tick in sorted(ticks):
        cells = [
            f"{e.node} {RunDisplay._STATUS_ICON.get(e.status, e.status)}"
            for e in ticks[tick]
        ]
        print(f"tick {tick}: " + ", ".join(cells))
        for e in ticks[tick]:
            if e.status != "ok":
                print(f"  ✗ {e.node}: {e.error or '无错误信息'}")
            if show_outputs and e.output is not None:
                print(f"    {e.node} output: {_preview(e.output, width=200)}")
    if timeline.latest_tick is not None:
        print(f"\n最新 tick: {timeline.latest_tick}")


def _cmd_review(args: argparse.Namespace) -> int:
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    timeline = build_timeline(run_id)
    if timeline is None:
        print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    if args.failed:
        timeline = filter_failed(timeline)
    if args.tick is not None:
        timeline = filter_tick(timeline, args.tick)
    if args.node:
        timeline = filter_node(timeline, args.node)
    if args.json:
        print(json.dumps(timeline_to_dict(timeline), ensure_ascii=False, indent=2))
        return 0
    _render_timeline(timeline, show_outputs=args.tick is not None or bool(args.node))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="specmodule",
        description="SpecModule CLI——选择模块、传入 spec/tasklist、观察与审阅运行",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行模块")
    p_run.add_argument("--module", required=True, help="模块名（modules/ 目录中发现）")
    p_run.add_argument(
        "--modules-dir", default="modules",
        help="模块目录（默认 modules/，cwd 相对；未来 init 实例布局即此目录）",
    )
    p_run.add_argument("--spec", help="内联 JSON spec")
    p_run.add_argument("--spec-file", help="spec JSON 文件路径")
    p_run.add_argument("--template", help="模板名（默认 entry.default_template）")
    p_run.add_argument(
        "--tasklist", help="tasklist JSON 文件路径（跳过翻译，与 --template 互斥）"
    )
    p_run.add_argument("--run-id", help="运行目录名（默认模块名）")
    p_run.add_argument(
        "--verbose", type=int, choices=(1, 2, 3), default=1,
        help="实时显示级别：1=tick+节点+状态（默认），2=+产出预览，3=完整详情块",
    )
    p_run.add_argument("--max-ticks", type=int, default=100, help="tick 上限（默认 100）")
    p_run.add_argument("--mock", action="store_true", help="免 key 假 LLM 冒烟（测试/演示）")
    p_run.set_defaults(func=_cmd_run)

    p_status = sub.add_parser("status", help="查询运行状态")
    p_status.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_status.add_argument("--json", action="store_true", help="JSON 输出")
    p_status.set_defaults(func=_cmd_status)

    p_review = sub.add_parser("review", help="审阅历史时间线")
    p_review.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_review.add_argument("--tick", type=int, help="只看指定 tick")
    p_review.add_argument("--node", help="只看指定节点")
    p_review.add_argument("--failed", action="store_true", help="只看失败节点")
    p_review.add_argument("--json", action="store_true", help="JSON 输出")
    p_review.set_defaults(func=_cmd_review)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(
            "\n已中断——运行数据已落盘 .specmodule/runs/（specmodule status/review 可查）",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())