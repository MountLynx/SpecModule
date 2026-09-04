# module_harness/cli.py
"""specmodule CLI — 使用者层面入口（run / status / review / resume / checkpoints / snapshot / rollback / init）。

用法（无打包，与 ``python -m tickflow`` 一致）::

    python -m module_harness.cli run --module academic_writer --spec-file spec.json
    python -m module_harness.cli run --module academic_writer --spec '{"raw_text": "..."}'
    python -m module_harness.cli resume <rollback> --module <名> [--spec ...]
    python -m module_harness.cli status [--run-id xxx] [--json]
    python -m module_harness.cli review [--run-id xxx] [--tick N] [--node xxx] [--failed] [--json]
    python -m module_harness.cli checkpoints [--run-id xxx] [--json]
    python -m module_harness.cli snapshot [<tick>] [--run-id xxx] [--json] [--out FILE]
    python -m module_harness.cli rollback <目标> --module <名> [--spec ...]
    python -m module_harness.cli checkpoint <label> [<tick>] [--run-id xxx]
    python -m module_harness.cli cancel | pause | unpause [--run-id xxx]
    python -m module_harness.cli runs [--json]
    python -m module_harness.cli delete-run <run_id>
    python -m module_harness.cli visualize --module <名> [--tasklist x.json | --run-id xxx] [--out FILE]

场景归属：使用者层面（usage scenario）——第二级用户只写 spec/tasklist，
不写 Python。模块按名选择，入口注册由开发者在 ``modules/<name>.py`` 声明。
查询组合逻辑只 import 共享层（module_harness.query），绝不重实现。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm import LLMConfig, create_llm_client
from llm.mock import MockLLMClient

from tickflow.persistence import SqliteBackend

from ..infra.checkpoint import ResumeError
from .entry import discover_modules
from ..infra.events import EventBus
from ..orchestrate.feed import RunFeedServer
from ..model.module import Module, _persist_dir
from ..infra.query import (
    CheckpointList,
    ReviewTimeline,
    build_checkpoints,
    build_timeline,
    checkpoints_to_dict,
    create_checkpoint,
    delete_run,
    filter_failed,
    filter_node,
    filter_tick,
    list_runs,
    load_snapshot_summary,
    run_db_path,
    timeline_to_dict,
)
from ..core.registry import HarnessRegistry
from .scaffold import scaffold, scaffold_dir
from ..infra import store
from ..model.spec import Tasklist
from ..infra.status import query_run_status
from ..model.translator import TemplateLoader


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
        config = LLMConfig.from_env(store_root=store.store_home())
    except ValueError as e:
        raise ValueError(
            f"LLM 环境配置失败: {e}\n提示：可加 --mock 免 key 冒烟运行"
        )
    if not config.is_configured:
        raise ValueError(
            "LLM 未配置 API key——请配置 config.json + .env（项目根或 "
            f"store 家目录 {store.store_home()}），或加 --mock 冒烟"
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


def _print_final_summary(
    display: RunDisplay, entry: Any, module_id: str, label: str = "运行完成"
) -> None:
    """结束汇总：运行/续跑完成 + firing 总数 + 节点输出摘要（run/resume 共用）。"""
    print(f"\n{label}: module={entry.name} run_id={module_id}")
    print(f"共 {len(display.firings)} 次节点 firing")
    by_node: dict[str, Any] = {
        ns.node: ns.output for ns in display.firings if ns.status == "ok"
    }
    if by_node:
        print("节点最新输出摘要:")
        for node, out in by_node.items():
            print(f"  {node}: {_preview(out)}")


def _resolve_module_cmd(args: argparse.Namespace) -> store.ResolvedModule | None:
    """按搜索路径统一解析模块（--modules-dir 兼容保留为最高优先）。

    解析顺序：显式 ``--modules-dir`` 的 entry（旧语义）→ 统一搜索路径
    （收编进 store.resolve_module_full：cwd/modules + $SPECMODULE_PATH +
    store/modules + pip；packed 形态轻量加载——ModuleLoader.load 不创建
    LLM client，client 由 ``_build_llm_client`` 延后，校验/渲染零 LLM）。
    未找到 → 打印可用清单并返回 None；加载失败/入口解析失败 → ValueError
    消息打印后返回 None。
    """
    # 1. 显式 --modules-dir：保持旧语义（只找 entry 单文件）
    if args.modules_dir != "modules" or (args.modules_dir == "modules"
                                         and not (Path.cwd() / "modules").is_dir()):
        # --modules-dir 显式给出：仅该目录的 entry 形态
        entries = discover_modules(Path(args.modules_dir))
        entry = entries.get(args.module)
        if entry is not None:
            res = store.ResolvedModule(args.module, store.ModuleSource(
                name=args.module, kind="entry",
                path=Path(args.modules_dir) / f"{args.module}.py",
            ))
            res.entry = entry
            return res

    # 2. 统一搜索路径（store.resolve_module_full 共享归一层）
    try:
        res = store.resolve_module_full(args.module)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return None
    if res is None:
        print(
            f"模块 '{args.module}' 未找到——可用: specmodule list 查看全部",
            file=sys.stderr,
        )
        _print_available(discover_modules(Path(args.modules_dir)))
        return None
    return res


def _cmd_run(args: argparse.Namespace) -> int:
    res = _resolve_module_cmd(args)
    if res is None:
        return 1
    if args.tasklist and args.template:
        print("--tasklist 与 --template 互斥——只能二选一", file=sys.stderr)
        return 1
    # 预绑定：Ctrl+C 可能落在 display/mod 赋值前（pre-run 阶段），
    # 避免 KeyboardInterrupt 处理器引用未绑定变量抛 NameError（探索定稿已知缺陷）。
    display = None
    mod = None
    try:
        spec = _resolve_spec(res, args)
        _check_spec_schema(res, spec)
        llm_client = _build_llm_client(args.mock)
        template_name = args.template or res.default_template
        if args.tasklist:
            # tasklist 路径：跳过翻译，template_name 置 None（与 Module
            # "template/tasklist 二选一"不变量对齐）
            template_name = None
        display = RunDisplay(args.verbose)
        if res.submodule is not None:
            # packed 形态：SubModule.run（tasklist 固定，审核关闭）
            sub = res.submodule
            event_bus = EventBus()
            if args.template:
                print("--template 不适用于已打包模块（tasklist 固定）", file=sys.stderr)
                return 1
            asyncio.run(sub.run(
                spec,
                tasklist=_load_tasklist(args.tasklist) if args.tasklist else None,
                llm_client=llm_client,
                event_bus=event_bus,
                hooks=display.hooks(),
                max_ticks=args.max_ticks,
            ))
            run_id = sub._module_id()
        else:
            # entry 形态：统一接线 build_module（模板校验/registry/loader/Module）
            mod = res.entry.build_module(
                spec,
                template_name=template_name,
                tasklist=_load_tasklist(args.tasklist) if args.tasklist else None,
                llm_client=llm_client,
                module_id=args.run_id or args.module,
                hooks=display.hooks(),
            )
            asyncio.run(mod.run(max_ticks=args.max_ticks))
            run_id = mod.module_id
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
    # 结束汇总（display 此处必已赋值；mod 仅取 module_id）
    assert display is not None
    _print_final_summary(display, res, run_id)
    return 0


def _run_resume_cmd(args: argparse.Namespace, *, require_target: bool) -> int:
    """续跑/回退共用实现：镜像 run 接线，最后一步换 Module.resume。

    回退目标：显式 tick 号 / ``manual:<label>`` 直传库解析（无效目标由库
    KeyError 携带可用清单）；``require_target=False``（resume）缺省（None）
    由库解析为最新 tick 快照；``require_target=True``（rollback）目标必填，
    防"想回退却续了最新"。
    """
    res = _resolve_module_cmd(args)
    if res is None:
        return 1
    if args.tasklist and args.template:
        print("--tasklist 与 --template 互斥——只能二选一", file=sys.stderr)
        return 1
    module_id = args.run_id or args.module
    # 前置：续跑目标运行必须已落盘（run.sqlite = 快照 backend）
    db_path = _persist_dir(module_id)
    if not db_path.exists():
        print(f"无运行记录: {module_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    # 预绑定：Ctrl+C 可能落在 display/mod 赋值前，避免处理器引用未绑定变量
    display = None
    try:
        spec = _resolve_spec(res, args)
        _check_spec_schema(res, spec)
        llm_client = _build_llm_client(args.mock)
        template_name = args.template or res.default_template
        tasklist = _load_tasklist(args.tasklist) if args.tasklist else None
        if args.tasklist:
            # tasklist 路径：跳过翻译，template_name 置 None（与 Module
            # "template/tasklist 二选一"不变量对齐）
            template_name = None
        elif template_name is None and tasklist is None:
            # 流程来源兜底：显式参数 > entry.default_template > module_inputs
            # 归档 tasklist（续跑语义本该默认沿用原任务书——tasklist 通道
            # 启动的 run 无模板可回落，此前只能靠显式 --tasklist 续跑）
            from ..infra.query import read_module_inputs

            archived = read_module_inputs(module_id)
            if archived and archived.get("tasklist"):
                tasklist = Tasklist.from_json(archived["tasklist"])
                print("流程来源：沿用 module_inputs 归档 tasklist", file=sys.stderr)
        # 回退目标：显式直传；resume 缺省（None）由库解析为最新 tick 快照
        # （须在 Module 构造前检查——构造即写 status.json idle，会覆盖前次终态）
        rollback_to = args.rollback
        if rollback_to is None and require_target:
            print(
                "rollback 需要显式回退目标——可用: specmodule checkpoints --run-id "
                f"{module_id} 查看全部回退点（resume <目标> 亦可，缺省续最新）",
                file=sys.stderr,
            )
            return 1
        display = RunDisplay(args.verbose)
        if res.submodule is not None:
            # packed 形态：与 run 相同的 SubModule 内部接线（固定 tasklist）
            event_bus = EventBus()
            registry = res.submodule._build_registry(
                False, llm_client=llm_client, event_bus=event_bus
            )
            mod = Module(
                spec=spec,
                template_name=template_name,
                tasklist=res.submodule.tasklist,
                llm_client=llm_client,
                event_bus=event_bus,
                template_loader=TemplateLoader(),
                module_id=module_id,
                module=res.name,
                registry=registry,
                review_harness=None,
                modules=res.submodule.modules,
                hooks=display.hooks(),
            )
        else:
            # entry 形态：统一接线 build_module（模板校验/registry/loader/Module）
            mod = res.entry.build_module(
                spec,
                template_name=template_name,
                tasklist=tasklist,
                llm_client=llm_client,
                module_id=module_id,
                hooks=display.hooks(),
            )
        asyncio.run(mod.resume(rollback_to=rollback_to, max_ticks=args.max_ticks))
    except KeyboardInterrupt:
        n = len(display.firings) if display is not None else 0
        print(
            f"\n已中断：已执行 {n} 次节点 firing。"
            f"运行数据已落盘 .specmodule/runs/{module_id}/（status/review 可查）",
            file=sys.stderr,
        )
        return 2
    except KeyError as e:
        print(f"错误: {e}", file=sys.stderr)
        print(
            "可用回退点清单: specmodule checkpoints --run-id "
            f"{module_id}（resume/rollback 目标即其中 target）",
            file=sys.stderr,
        )
        return 1
    except (ValueError, ModuleNotFoundError, ResumeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    # 结束汇总（display 此处必已赋值）
    assert display is not None
    _print_final_summary(display, res, module_id, label="续跑完成")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """从中断处续跑模块（tick 截断 / Ctrl+C 后）：缺省 = 最新 tick 快照。"""
    return _run_resume_cmd(args, require_target=False)


def _cmd_rollback(args: argparse.Namespace) -> int:
    """回退到指定 tick/manual 检查点并重跑（目标必填）。"""
    return _run_resume_cmd(args, require_target=True)


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


def _cmd_control(args: argparse.Namespace) -> int:
    """cancel/pause/unpause 共享实现：写控制文件（运行进程 tick 边界消费）。

    纯数据操作（file 即通道），不接触运行进程；前置只校验目标 run 存在
    （status.json 落盘）。生效时机取决于运行进程的下一 tick 边界。
    """
    from ..infra.control import request_control

    module_id = args.run_id or _latest_run_id()
    if module_id is None or not (
        _persist_dir(module_id).parent / "status.json"
    ).exists():
        print(f"无运行记录: {module_id or '(无任何运行)'}", file=sys.stderr)
        return 1
    try:
        request_control(module_id, args.command, reason=getattr(args, "reason", None))
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(f"已请求 {args.command}: {module_id}（运行进程将在下一 tick 边界生效）")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """列出全部运行历史（数据走共享层 list_runs，本命令只渲染）。"""
    runs = list_runs()
    if not runs:
        print("无运行记录（先执行 specmodule run）")
        return 0
    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=2))
        return 0
    print(f"{'run_id':<24} {'模块':<18} {'phase':<12} {'tick':<6} 更新时间")
    for r in runs:
        updated = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["updated_at"]))
            if r["updated_at"] else "—"
        )
        tick = "—" if r["tick"] is None else str(r["tick"])
        line = (
            f"{r['run_id']:<24} {(r['module'] or '—'):<18} "
            f"{r['phase']:<12} {tick:<6} {updated}"
        )
        if r["error"]:
            line += f"  error={_preview(r['error'])}"
        print(line)
    return 0


def _cmd_delete_run(args: argparse.Namespace) -> int:
    """删除指定 run 的全部产物（数据走共享层 delete_run）。"""
    target = run_db_path(args.run_id).parent
    if not delete_run(args.run_id):
        print(f"无运行记录: {args.run_id}", file=sys.stderr)
        return 1
    print(f"已删除运行: {args.run_id}（{target}）")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """生成模块脚手架：--as-dir 目录形态（pack 同构）或单文件形态 + 项目文件补齐。"""
    try:
        if args.as_dir:
            result = scaffold_dir(
                args.name,
                base_dir=args.dir,
                force=args.force,
                description=args.description or "",
            )
        else:
            result = scaffold(
                args.name,
                base_dir=args.dir,
                force=args.force,
                description=args.description or "",
            )
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    for p in result.created:
        print(f"创建 {p}")
    for p in result.skipped:
        print(f"跳过（已存在） {p}")
    print(f"\n完成：{len(result.created)} 个文件创建，{len(result.skipped)} 个跳过。")
    if args.as_dir:
        print(f"冒烟验收：python -m module_harness.cli run --module {args.name} --mock")
        print(f"发布：specmodule publish {args.name} --from {args.dir}")
    else:
        print(f"冒烟验收：python -m module_harness.cli run --module {args.name} --mock")
    return 0


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


def _cmd_checkpoints(args: argparse.Namespace) -> int:
    """列出可用回退点（tick 快照 + manual 检查点）——resume/rollback 目标清单。

    数据组合走共享层 ``build_checkpoints``（CLI/MCP/Web 复用），渲染是本命令。
    """
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    cl: CheckpointList | None = build_checkpoints(run_id)
    if cl is None:
        print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(checkpoints_to_dict(cl), ensure_ascii=False, indent=2))
        return 0
    if not cl.entries:
        print(f"无可回退检查点: {run_id}", file=sys.stderr)
        return 1
    print(f"可用回退点 (run_id={run_id}):")
    for e in cl.entries:
        if e.kind == "tick":
            fired = ", ".join(e.fired) or "—"
            print(f"  tick {e.tick:<6} fired: {fired}")
        else:
            print(f"  manual: {e.label}  (tick {e.tick})")
    print(
        "\n回退: specmodule resume <目标> --module <名>"
        "（缺省续最新；rollback <目标> 须显式指定目标）"
    )
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """检视/导出指定 tick 的运行时快照（摘要走共享层 load_snapshot_summary）。

    默认：文本摘要（状态 / fired / fireable / 各节点最新输出）——数据组合走
    共享层（CLI/MCP/Web 复用），渲染是本命令；``--json``：stdout 打印完整
    runner 快照 JSON（即 ``runner.restore()`` 输入）；``--out FILE``：写完整
    快照 JSON 到文件（自包含，可 restore 到新 runner，跨进程调试素材）——
    全量导出直读 backend。缺省 tick = 最新。
    """
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    if args.json or args.out:
        # 完整快照 JSON（runner.restore() 输入）：直读 backend
        db_path = _persist_dir(run_id)
        if not db_path.exists():
            print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
            return 1
        backend = SqliteBackend(db_path)
        try:
            ticks = backend.list_snapshots(run_id)
            if not ticks:
                print(
                    f"无可恢复快照: {run_id}（运行未产生任何 tick 快照）",
                    file=sys.stderr,
                )
                return 1
            tick = args.tick if args.tick is not None else max(ticks)
            if tick not in ticks:
                print(
                    f"快照 tick {tick} 不存在（可用: {ticks or '无'}）",
                    file=sys.stderr,
                )
                return 1
            snap = backend.load_snapshot(run_id, tick)
            if snap is None:
                print(f"快照 tick {tick} 读取失败（数据损坏？）", file=sys.stderr)
                return 1
            text = json.dumps(snap, ensure_ascii=False, indent=2)
            if args.json:
                print(text)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
                print(f"快照已导出: {args.out}（{len(text)} 字节）")
            return 0
        finally:
            backend.close()
    # 文本摘要：共享层数据 + 本命令渲染
    try:
        s = load_snapshot_summary(run_id, tick=args.tick)
    except KeyError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    if s is None:
        print(f"无运行记录: {run_id}（先执行 specmodule run）", file=sys.stderr)
        return 1
    print(f"快照 (run_id={run_id}):")
    print(f"  tick: {s['tick']}")
    print(f"  status: {s['status']}")
    if s.get("cancel_reason"):
        print(f"  cancel_reason: {s['cancel_reason']}")
    if s.get("fireable"):
        print(f"  fireable: {', '.join(s['fireable'])}")
    if s["fired"]:
        print(f"  fired: {', '.join(s['fired'])}")
    if s["outputs"]:
        print("  各节点最新输出:")
        for node, out in s["outputs"].items():
            print(f"    {node}: {_preview(out)}")
    return 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    """给指定 tick 快照起命名检查点（数据走共享层 create_checkpoint）。"""
    run_id = args.run_id or _latest_run_id()
    if run_id is None:
        print("无运行记录（先执行 specmodule run）", file=sys.stderr)
        return 1
    try:
        out = create_checkpoint(run_id, args.label, tick=args.tick)
    except KeyError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    msg = f"已创建检查点 {out['label']}（tick {out['tick']}）"
    if out["overwritten"]:
        msg += "（覆盖同名）"
    print(msg)
    print(f"回退: specmodule rollback {out['label']} --module <名>（或 resume {out['label']}）")
    return 0


def _print_visualize_hint(args: argparse.Namespace, src) -> None:
    """visualize 构建失败时按需给出模板提示（旧实现的提示语义保留）。"""
    resolved = src if src is not None else store.resolve_module(args.module)
    if resolved is None or resolved.is_packed:
        return
    entry = discover_modules(resolved.path.parent).get(args.module)
    if entry is None or not entry.templates:
        return
    template_hint = "、".join(entry.templates)
    print(
        "提示: registry 按模板 "
        f"'{args.template or entry.default_template}' 构建，"
        "tasklist 与之不匹配时会出现未注册元件——可用模板: "
        f"{template_hint}；存档/文件的 tasklist 可能来自其他模板，"
        "试试对应 --template（如仍失败可传 --tasklist 直接渲染文件）",
        file=sys.stderr,
    )


def _cmd_visualize(args: argparse.Namespace) -> int:
    """渲染 tasklist 对应图（mermaid）——看"这次流水线长什么样"。

    组合逻辑在共享层 ``query.build_run_graph``（Web 可视化共用，统一 API 原则）；
    CLI 只留参数接线 + mermaid 出口。数据源与错误提示语义与旧实现逐字一致。
    """
    # 显式 --modules-dir：该目录 entry 优先（旧语义，仅此目录；未命中回落统一搜索）
    src = None
    if args.modules_dir != "modules" or (args.modules_dir == "modules"
                                         and not (Path.cwd() / "modules").is_dir()):
        entries = discover_modules(Path(args.modules_dir))
        if args.module in entries:
            src = store.ModuleSource(
                name=args.module, kind="entry",
                path=Path(args.modules_dir) / f"{args.module}.py",
            )
    tasklist = _load_tasklist(args.tasklist) if args.tasklist else None
    run_id = args.run_id or args.module
    try:
        from ..infra.query import build_run_graph

        res = build_run_graph(
            args.module, run_id, tasklist=tasklist, src=src,
        )
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        _print_visualize_hint(args, src)
        return 1
    if res is None:
        print(
            f"无运行记录: {run_id}（先执行 specmodule run，或传 --tasklist 直接渲染）",
            file=sys.stderr,
        )
        return 1
    graph, _ = res
    text = graph.to_mermaid()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"mermaid 已导出: {args.out}（{len(text)} 字节）")
    else:
        print(text)
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


def _cmd_feed(args: argparse.Namespace) -> int:
    """启动零依赖运行 feed（http.server）：浏览器轮询查看运行状态/时间线。"""
    server = RunFeedServer(("127.0.0.1" if args.host == "localhost" else args.host, args.port))
    run_id = args.run_id or server.latest_run_id()
    print(f"SpecModule feed 已启动: http://{args.host}:{args.port}/")
    if run_id is not None:
        print(f"查看运行: http://{args.host}:{args.port}/?run_id={run_id}")
    print("Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 0
    finally:
        server.server_close()
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    """安装模块到 store：本地 pack 目录或 git URL（校验零落盘）。"""
    target = args.source
    if target.startswith(("http://", "https://")) or target.endswith(".git") or "git@" in target:
        src = _clone_to_cache(target)
    else:
        src = Path(target)
        if not src.is_dir():
            print(f"安装源不存在: {src}", file=sys.stderr)
            return 1
    try:
        dest = store.install_pack(src, source=target)
    except ValueError as e:
        print(f"安装失败: {e}", file=sys.stderr)
        return 1
    print(f"已安装: {dest.name} → {dest}")
    print(f"来源: {target}（manifest: {store.manifests_dir() / (dest.name + '.json')}）")
    return 0


def _clone_to_cache(url: str) -> Path:
    """git clone --depth 1 到 cache 临时目录；调用方负责清理（install 后删除）。"""
    import shutil
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(dir=store.cache_dir()))
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(tmp / "src")],
            check=True, capture_output=True,
        )
        return tmp / "src"
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(
            f"git clone 失败: {e.stderr.decode('utf-8', 'replace').strip() or e}"
        ) from e


def _cmd_list(args: argparse.Namespace) -> int:
    """列出全部可用模块（同名多来源全量展示，含优先级）。"""
    mods = store.list_modules()
    if not mods:
        print("无可用模块（搜索路径: cwd/modules + $SPECMODULE_PATH + store/modules + pip）")
        return 0
    rows = []
    for name in sorted(mods):
        for src in mods[name]:
            kind = {"entry": "entry", "packed": "packed", "pip": "pip"}[src.kind]
            rows.append((src.priority, name, src.version, kind, src.description, str(src.path)))
    if args.json:
        print(json.dumps([
            {"name": n, "version": v, "kind": k, "description": d, "path": p}
            for _, n, v, k, d, p in rows
        ], ensure_ascii=False, indent=2))
        return 0
    print(f"{'模块':<20} {'版本':<10} {'形态':<8} 描述")
    for _, name, ver, kind, desc, path in rows:
        print(f"{name:<20} {ver:<10} {kind:<8} {desc or ''}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    """显示模块详情：元数据 + 来源 + 安装信息。"""
    src = store.resolve_module(args.name)
    if src is None:
        print(f"模块 '{args.name}' 未找到——可用: specmodule list 查看全部", file=sys.stderr)
        return 1
    print(f"名称: {src.name}")
    print(f"形态: {src.kind}")
    print(f"描述: {src.description or '—'}")
    print(f"版本: {src.version or '—'}")
    print(f"路径: {src.path}")
    if src.kind in ("packed", "pip"):
        manifest = store.load_manifest(src.name) if src.kind == "packed" else None
        if manifest:
            print(f"来源: {manifest.get('source', '—')}")
            print(f"安装时间: {manifest.get('installed_at', '—')}")
            print(f"文件数: {len(manifest.get('files', {}))}")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """从 store 移除模块（目录 + manifest）。"""
    if not store.uninstall_pack(args.name):
        print(f"模块 '{args.name}' 未安装（store 中不存在）", file=sys.stderr)
        return 1
    print(f"已卸载: {args.name}")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    """一次性交互向导：provider/model/key → 写 store 级 .env + config.json。"""
    import shutil

    from .scaffold import CONFIG_JSON

    home = store.store_home()
    env_path = home / ".env"
    config_path = home / "config.json"
    existing_env = store.parse_dotenv(env_path)
    existing_cfg = {}
    if config_path.exists():
        try:
            existing_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass

    print("SpecModule 配置向导（写入 store 家目录: %s）" % home)
    print("现有配置：", "有" if (existing_env or existing_cfg) else "无")
    if existing_env or existing_cfg:
        answer = input("覆盖既有配置？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消（保留现有配置）")
            return 0

    provider = input("Provider (openai/anthropic/openai-compatible) [openai]: ").strip() or "openai"
    model = input("默认模型 [gpt-4o-mini]: ").strip() or "gpt-4o-mini"
    key_env = input("API key 环境变量名 [OPENAI_API_KEY]: ").strip() or "OPENAI_API_KEY"
    key = input(f"{key_env} 值: ").strip()
    base_url = input("Base URL（可选，留空跳过）: ").strip() or None

    # 写 .env（追加/更新 key 行）
    lines = []
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
    new_line = f"{key_env}={key}"
    lines = [ln for ln in lines if not ln.startswith(f"{key_env}=")]
    lines.append(new_line)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 写 config.json（复用 scaffold 的 CONFIG_JSON 结构）
    cfg = dict(existing_cfg) if not (existing_env or existing_cfg) else {}
    providers = cfg.get("providers", [])
    if not providers:
        providers = [{
            "name": provider,
            "sdktype": provider,
            "api_key_env": key_env,
            "base_url": base_url,
        }]
        cfg["providers"] = providers
    models = cfg.get("models", [])
    if not any(m.get("name") == model for m in models):
        models.append({"name": model, "provider": provider})
        cfg["models"] = models
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已写入: {env_path}")
    print(f"已写入: {config_path}")
    print("现在可直接运行: specmodule run --module <名>（无需项目根配置）")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    """发布模块到 store：目录形态直接校验复制；单文件形态经等价 SubModule 转化。"""
    src = Path(args.from_dir)
    if (src / "module.json").is_file():
        # 目录形态：与 install 同校验（D9）
        try:
            dest = store.install_pack(src, source=args.from_dir, name=args.name)
        except ValueError as e:
            print(f"发布失败: {e}", file=sys.stderr)
            return 1
        print(f"已发布: {dest.name} → {dest}")
        return 0

    # 单文件 entry 形态：以 default_template 的 tasklist 驱动等价 SubModule
    # 导出（D9）。entry 组件注册在 build_registry 闭包内——用 Mock client
    # 构建 registry 后按 tasklist 引用提取，包体自包含（无闭包依赖）。
    entry_file = src / "modules" / f"{args.name}.py"
    if not entry_file.is_file():
        print(
            f"发布源无效: {src}（既不是 pack 目录，也没有 modules/{args.name}.py）",
            file=sys.stderr,
        )
        return 1
    from ..core.builtins import BUILTIN_HARNESS_NAMES
    from .entry import discover_modules
    from ..model.spec import SpecSchema
    from ..model.submodule import SubModule

    entries = discover_modules(src / "modules")
    entry = entries.get(args.name)
    if entry is None:
        print(f"模块 '{args.name}' 未找到（{entry_file} 无 entry 声明）", file=sys.stderr)
        return 1
    if entry.default_template is None:
        print(
            f"单文件形态 publish 失败（{args.name}）：entry 未声明 default_template。"
            "请用目录形态：specmodule init <name> --as-dir",
            file=sys.stderr,
        )
        return 1
    template_name = entry.default_template
    if template_name not in entry.templates:
        print(
            f"单文件形态 publish 失败（{args.name}）：default_template "
            f"'{template_name}' 不在 templates 中",
            file=sys.stderr,
        )
        return 1

    # 构建 registry（Mock 占位，零 LLM）→ 提取组件
    event_bus = EventBus()
    if entry.build_registry is not None:
        registry = entry.build_registry(MockLLMClient(), template_name, event_bus)
    else:
        registry = HarnessRegistry(llm_client=MockLLMClient(), event_bus=event_bus)
    template = entry.templates[template_name]
    tasklist = Tasklist.from_json(template["tasklist"])

    # 按 tasklist 引用提取组件（不含内置 harness）
    harnesses = []
    scripts: dict[str, Any] = {}
    commands = []
    for key, task in tasklist.tasks.items():
        if task.type == "harness" and task.harness not in BUILTIN_HARNESS_NAMES:
            cfg = registry.harness_config(task.harness)
            if cfg is None:
                print(
                    f"单文件形态 publish 失败（{args.name}）：harness "
                    f"'{task.harness}' 未在 registry 注册",
                    file=sys.stderr,
                )
                return 1
            if not any(h.name == cfg.name for h in harnesses):
                harnesses.append(cfg)
        elif task.type == "script":
            fn = registry.get_body(task.script).__wrapped__ \
                if hasattr(registry.get_body(task.script), "__wrapped__") \
                else registry.get_body(task.script)
            scripts.setdefault(task.script, fn)
        elif task.type == "command":
            cc = registry.command_config(task.command)
            if cc is None:
                print(
                    f"单文件形态 publish 失败（{args.name}）：command "
                    f"'{task.command}' 未在 registry 注册",
                    file=sys.stderr,
                )
                return 1
            if not any(c.name == cc.name for c in commands):
                commands.append(cc)

    # 等价 SubModule → pack → install（校验失败诚实报错）
    sub = SubModule()
    sub.name = entry.name
    sub.version = "0.1.0"
    sub.description = entry.description
    sub.spec_schema = SpecSchema(
        input=dict(entry.spec_schema or {}),
        output={},
    )
    sub.tasklist = tasklist
    sub.harnesses = harnesses
    sub.commands = commands
    sub._scripts = scripts
    sub.guards = []  # guard 函数在闭包内不可静态导出——tasklist 引用 guard 的模块走目录形态
    sub.modules = entry.submodules

    import tempfile

    tmp = Path(tempfile.mkdtemp())
    try:
        out = sub.pack(tmp / "pack")
        try:
            dest = store.install_pack(out, source=args.from_dir, name=entry.name)
        except ValueError as e:
            print(f"发布失败: {e}", file=sys.stderr)
            return 1
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    print(f"已发布（单文件转化）: {dest.name} → {dest}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    """更新模块：按 manifest 来源重取 → 哈希比对 → 无差异直接替换；
    有差异列清单交互确认（--yes 覆盖 / --keep 保留本地）。"""
    manifest = store.load_manifest(args.name)
    if manifest is None:
        print(
            f"模块 '{args.name}' 未安装或无 manifest——用 specmodule install 安装",
            file=sys.stderr,
        )
        return 1
    source = manifest.get("source", "")
    if source.startswith(("http://", "https://")) or source.endswith(".git") or "git@" in source:
        try:
            src = _clone_to_cache(source)
        except ValueError as e:
            print(f"更新失败: {e}", file=sys.stderr)
            return 1
    else:
        src = Path(source)
        if not src.is_dir():
            print(f"更新来源不可用: {src}", file=sys.stderr)
            return 1

    diff = store.check_updates(args.name, src)
    src_diff = any(diff[k] for k in ("changed", "added", "removed"))
    local_diff = any(diff[k] for k in ("local_modified", "untracked"))
    if not src_diff:
        # 来源无更新：本地改动保留（不动已装文件），仅提示
        if local_diff:
            print(f"来源无更新；本地改动保留（{', '.join(diff['local_modified'] or diff['untracked'])}）")
        else:
            store.apply_update(args.name, src)  # 内容一致，刷新 installed_at
            print(f"已更新: {args.name}（无内容变化）")
        return 0

    print(f"模块 '{args.name}' 来源检测到差异：")
    for label, key in (
        ("变化", "changed"), ("新增", "added"), ("移除", "removed"),
    ):
        if diff[key]:
            print(f"  [{label}] " + ", ".join(diff[key]))
    if local_diff:
        print("警告：本地有改动（" + ", ".join(
            diff["local_modified"] + diff["untracked"]
        ) + "），覆盖将丢失——建议保留（--keep）或先备份。")
    if args.yes:
        store.apply_update(args.name, src)
        print(f"已更新: {args.name}（覆盖差异）")
        return 0
    if args.keep:
        print(f"已跳过: {args.name}（--keep 保留本地）")
        return 0
    answer = input("覆盖并更新？[y/N] ").strip().lower()
    if answer in ("y", "yes"):
        store.apply_update(args.name, src)
        print(f"已更新: {args.name}（覆盖差异）")
        return 0
    print("已取消（未写入任何文件）")
    return 0


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


def _add_continue_args(p: argparse.ArgumentParser) -> None:
    """resume/rollback 共享参数：接线与 run 一致（spec/模板/verbose/mock），

    仅回退目标位置参数与默认语义不同（resume 缺省续最新，rollback 必填）。
    """
    p.add_argument(
        "--module", required=True, help="模块名（modules/ 中发现；须与先前 run 一致）"
    )
    p.add_argument(
        "--modules-dir", default="modules",
        help="模块目录（默认 modules/，cwd 相对）",
    )
    p.add_argument(
        "--spec", help="内联 JSON spec（重建未执行部分；缺省 entry.default_spec）"
    )
    p.add_argument("--spec-file", help="spec JSON 文件路径")
    p.add_argument("--template", help="模板名（默认 entry.default_template）")
    p.add_argument(
        "--tasklist", help="tasklist JSON 文件路径（跳过翻译，与 --template 互斥）"
    )
    p.add_argument(
        "--run-id", help="运行目录名（默认模块名；须与先前 run 的 run-id 一致）"
    )
    p.add_argument(
        "--verbose", type=int, choices=(1, 2, 3), default=1,
        help="实时显示级别：1=tick+节点+状态（默认），2=+产出预览，3=完整详情块",
    )
    p.add_argument(
        "--max-ticks", type=int, default=100,
        help="tick 上限（恢复后从回退 tick 起计的绝对上限，默认 100）",
    )
    p.add_argument("--mock", action="store_true", help="免 key 假 LLM 冒烟（测试/演示）")


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

    p_resume = sub.add_parser(
        "resume", help="从中断处续跑模块（tick 截断 / Ctrl+C 后，缺省续最新）"
    )
    p_resume.add_argument(
        "rollback", nargs="?",
        help="回退目标：tick 号或 manual:<label>（缺省 = 最新 tick 快照）",
    )
    _add_continue_args(p_resume)
    p_resume.set_defaults(func=_cmd_resume)

    p_rollback = sub.add_parser(
        "rollback", help="回退到指定 tick/manual 检查点并重跑（目标必填）"
    )
    p_rollback.add_argument(
        "rollback",
        help="回退目标：tick 号或 manual:<label>（必填；specmodule checkpoints 查看可用目标）",
    )
    _add_continue_args(p_rollback)
    p_rollback.set_defaults(func=_cmd_rollback)

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

    p_checkpoints = sub.add_parser(
        "checkpoints", help="列出可用回退点（tick 快照 + manual 检查点）"
    )
    p_checkpoints.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_checkpoints.add_argument("--json", action="store_true", help="JSON 输出")
    p_checkpoints.set_defaults(func=_cmd_checkpoints)

    p_snapshot = sub.add_parser("snapshot", help="检视/导出指定 tick 的运行时快照")
    p_snapshot.add_argument(
        "tick", nargs="?", type=int, help="快照 tick（缺省 = 最新）"
    )
    p_snapshot.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_snapshot.add_argument("--json", action="store_true", help="stdout 打印完整快照 JSON")
    p_snapshot.add_argument(
        "--out", help="写完整快照 JSON 到文件（自包含，可 restore 到新 runner）"
    )
    p_snapshot.set_defaults(func=_cmd_snapshot)

    p_checkpoint = sub.add_parser(
        "checkpoint", help="给指定 tick 快照起命名检查点（manual: 永久保留）"
    )
    p_checkpoint.add_argument("label", help="检查点标签（自动补 manual: 前缀）")
    p_checkpoint.add_argument(
        "tick", nargs="?", type=int, help="快照 tick（缺省 = 最新）"
    )
    p_checkpoint.add_argument("--run-id", help="运行 id（默认最近运行）")
    p_checkpoint.set_defaults(func=_cmd_checkpoint)

    for _name, _help in (
        ("cancel", "请求取消运行（协作式：下一 tick 边界生效，phase→cancelled）"),
        ("pause", "请求暂停运行（tick 边界挂起，tick 计数不前进）"),
        ("unpause", "释放暂停，运行继续"),
    ):
        _p = sub.add_parser(_name, help=_help)
        _p.add_argument("--run-id", help="运行 id（默认最近运行）")
        if _name == "cancel":
            _p.add_argument("--reason", help="取消原因（透传 runner.cancel）")
        _p.set_defaults(func=_cmd_control)

    p_runs = sub.add_parser("runs", help="列出全部运行历史（run 枚举）")
    p_runs.add_argument("--json", action="store_true", help="JSON 输出")
    p_runs.set_defaults(func=_cmd_runs)

    p_delete_run = sub.add_parser("delete-run", help="删除指定运行的全部产物")
    p_delete_run.add_argument("run_id", help="运行 id（.specmodule/runs/ 下的目录名）")
    p_delete_run.set_defaults(func=_cmd_delete_run)

    p_visualize = sub.add_parser(
        "visualize", help="渲染 tasklist 对应图（mermaid 导出）"
    )
    p_visualize.add_argument(
        "--module", required=True,
        help="模块名（modules/ 中发现；构建 registry 校验 graph）",
    )
    p_visualize.add_argument(
        "--modules-dir", default="modules",
        help="模块目录（默认 modules/，cwd 相对）",
    )
    p_visualize.add_argument(
        "--tasklist", help="tasklist JSON 文件（直接渲染，不依赖运行记录）"
    )
    p_visualize.add_argument(
        "--template", help="模板名（默认 entry.default_template；仅影响 registry 构建）"
    )
    p_visualize.add_argument("--run-id", help="运行 id（缺省 = 模块同名运行目录；与 --tasklist 互斥）")
    p_visualize.add_argument(
        "--out", help="写 mermaid 文本到文件（缺省打印 stdout）"
    )
    p_visualize.set_defaults(func=_cmd_visualize)

    p_init = sub.add_parser("init", help="生成模块开发脚手架（单文件模块 + 项目文件）")
    p_init.add_argument("name", help="模块名（合法 Python 标识符；同时是文件/--module/entry.name/run_id）")
    p_init.add_argument("--dir", default=".", help="生成位置（默认 cwd）")
    p_init.add_argument(
        "--as-dir", action="store_true",
        help="目录形态（modules/<name>/ pack 同构骨架；默认单文件 modules/<name>.py）",
    )
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的模块文件（仅模块文件）")
    p_init.add_argument("--description", help="模块描述（展示用，不受标识符约束）")
    p_init.set_defaults(func=_cmd_init)

    p_feed = sub.add_parser(
        "feed", help="启动零依赖运行 feed（http.server，浏览器轮询查看）"
    )
    p_feed.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_feed.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    p_feed.add_argument("--run-id", help="运行 id（缺省 = 最近运行）")
    p_feed.set_defaults(func=_cmd_feed)

    p_list = sub.add_parser("list", help="列出全部可用模块（同名多来源全量展示）")
    p_list.add_argument("--json", action="store_true", help="JSON 输出")
    p_list.set_defaults(func=_cmd_list)

    p_info = sub.add_parser("info", help="显示模块详情（元数据 + 来源 + 安装信息）")
    p_info.add_argument("name", help="模块名")
    p_info.set_defaults(func=_cmd_info)

    p_install = sub.add_parser(
        "install", help="安装模块到 store（本地 pack 目录或 git URL，校验零落盘）"
    )
    p_install.add_argument("source", help="本地 pack 目录路径或 git URL")
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="从 store 移除模块（目录 + manifest）")
    p_uninstall.add_argument("name", help="模块名")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_setup = sub.add_parser(
        "setup", help="一次性配置向导：provider/model/key → 写 store 级配置"
    )
    p_setup.set_defaults(func=_cmd_setup)

    p_publish = sub.add_parser(
        "publish", help="发布模块到 store（目录形态校验复制；单文件形态经 SubModule 转化）"
    )
    p_publish.add_argument("name", help="模块名")
    p_publish.add_argument("--from", dest="from_dir", default=".", help="发布源目录（默认 cwd）")
    p_publish.set_defaults(func=_cmd_publish)

    p_update = sub.add_parser(
        "update", help="更新模块（manifest 脏检测；本地改动列清单交互确认）"
    )
    p_update.add_argument("name", help="模块名")
    p_update.add_argument("--yes", action="store_true", help="非交互：有差异直接覆盖")
    p_update.add_argument("--keep", action="store_true", help="非交互：有差异保留本地不更新")
    p_update.set_defaults(func=_cmd_update)

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