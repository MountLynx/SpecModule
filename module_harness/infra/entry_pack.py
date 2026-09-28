# module_harness/infra/entry_pack.py
"""entry → pack 物化共享层：单文件 ModuleEntry 转自包含发布目录。

publish（CLI 单文件形态）与 Web convert 共用的唯一转化实现——纯物化零副作用：
只写临时目录，不碰 store（安装/entry 退位策略归调用方）。组件提取按 tasklist
引用驱动：harness/command 取配置 JSON，script/guard 取函数源码（注册名 ≠ 函数名
时文件尾补别名行——loader 按 stem 取函数），submodule 走类式 SubModule.pack()
整包导出。产物必须自包含可装载：提取失败抛 ValueError（消息可直接面向用户），
不静默出坏包。

诚实边界（warnings 交调用方透传）：translation 通道不保留（packed 无此契约，
按模板静态 tasklist 转化）；getsource 只取函数体文本，引用模块级常量/辅助函数
的 body 物化后装载通过、运行期才炸；default_spec 样例值不保留（packed manifest
无此契约键）。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.builtins import BUILTIN_HARNESS_NAMES
from ..core.registry import HarnessRegistry
from ..infra.events import EventBus
from ..model.spec import Tasklist

if TYPE_CHECKING:
    from ..cli.entry import ModuleEntry

log = logging.getLogger(__name__)

# Flow 边的 guard 引用：`Review --|has_issues|--> Fix`
_GUARD_REF = re.compile(r"--\|([A-Za-z_][A-Za-z0-9_]*)\|")


@dataclass
class EntryPackResult:
    """entry_to_pack 产出：pack 目录 + 转化报告（调用方透传用户）。"""

    pack_dir: Path
    template_name: str
    dropped_templates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _source_def_name(src: str) -> str | None:
    """源码文本首个顶层 def 名（AST 解析——``__name__`` 可被运行期改写，
    别名判定必须看源码真实 def 名，如 ``_make_loop_guards`` 形态）。"""
    import ast

    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _write_fn_source(
    fn: Any, reg_name: str, pack: Path, kind: str, warnings: list[str]
) -> None:
    """注册函数 → ``<kind>/<reg_name>.py``：getsource + __future__ 头 +
    源码 def 名 ≠ 注册名时补别名行（loader exec 后按 stem 取函数）。"""
    fn = inspect.unwrap(fn)
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError) as e:
        raise ValueError(
            f"{kind} '{reg_name}' 源码不可静态导出（{e}）——"
            "闭包依赖/内置函数无法物化为自包含文件"
        ) from e
    text = "from __future__ import annotations\n\n" + src
    def_name = _source_def_name(src)
    if def_name is not None and def_name != reg_name:
        text += f"\n\n{reg_name} = {def_name}\n"
        warnings.append(
            f"{kind} '{reg_name}' 注册名与源码函数名 {def_name} 不一致——已补别名行")
    d = pack / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{reg_name}.py").write_text(text, encoding="utf-8")


def entry_to_pack(
    entry: ModuleEntry,
    *,
    template_name: str | None = None,
    out_dir: Path | None = None,
) -> EntryPackResult:
    """ModuleEntry → 自包含 pack 目录（纯物化，不碰 store）。

    ``template_name`` 缺省回落 ``entry.default_template``（未声明 → ValueError）；
    其余模板进 ``dropped_templates``。tasklist 经 ``from_json → to_dict`` 规一化
    （无效模板早期失败）。registry 用 Mock client 构建（零 LLM）——只取配置与
    函数源码，不执行任何 body。``out_dir`` 缺省自建临时目录（调用方负责清理
    ``result.pack_dir``）。
    """
    import tempfile

    tname = template_name or entry.default_template
    if tname is None:
        raise ValueError(
            f"entry '{entry.name}' 未声明 default_template——转化需显式指定模板")
    if tname not in entry.templates:
        raise ValueError(
            f"模板 '{tname}' 未注册——可用: {', '.join(sorted(entry.templates))}")
    tpl = entry.templates[tname]
    dropped = sorted(set(entry.templates) - {tname})
    warnings: list[str] = []
    if dropped:
        warnings.append(
            f"未转化的模板: {', '.join(dropped)}（packed 单 tasklist，仅取 '{tname}'）")
    try:
        tasklist = Tasklist.from_json(tpl["tasklist"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"模板 '{tname}' 的 tasklist 无效: {e}") from e
    if tpl.get("translation"):
        warnings.append(
            "模板翻译通道未保留——按模板静态 tasklist 转化（packed 无 translation 契约）")
    if entry.default_spec:
        warnings.append("entry 级 default_spec 样例值不保留（packed manifest 无此契约键）")

    from llm.mock import MockLLMClient

    client = MockLLMClient()
    if entry.build_registry is not None:
        registry = entry.build_registry(client, tname, EventBus.null())
    else:
        registry = HarnessRegistry(llm_client=client, event_bus=EventBus.null())

    harness_files: dict[str, dict[str, Any]] = {}
    command_files: dict[str, dict[str, Any]] = {}
    script_bodies: dict[str, Any] = {}
    for key, task in tasklist.tasks.items():
        if task.type == "harness":
            name = task.harness or ""
            if name in BUILTIN_HARNESS_NAMES:
                continue
            cfg = registry.harness_config(name)
            if cfg is None:
                raise ValueError(
                    f"task '{key}' 引用的 harness '{name}' 未在 entry registry 注册")
            if cfg.name != name:
                raise ValueError(
                    f"harness 注册名 '{name}' 与配置名 '{cfg.name}' 不一致——"
                    "tasklist 引用与装载键必须相同")
            harness_files[name] = cfg.to_dict()
        elif task.type == "command":
            name = task.command or ""
            cc = registry.command_config(name)
            if cc is None:
                raise ValueError(
                    f"task '{key}' 引用的 command '{name}' 未在 entry registry 注册")
            if cc.name != name:
                raise ValueError(
                    f"command 注册名 '{name}' 与配置名 '{cc.name}' 不一致——"
                    "tasklist 引用与装载键必须相同")
            command_files[name] = cc.to_dict()
        elif task.type == "script":
            name = task.script or ""
            try:
                script_bodies[name] = registry.get_body(name)
            except KeyError as e:
                raise ValueError(
                    f"task '{key}' 引用的 script '{name}' 未在 entry registry 注册") from e
        elif task.type == "submodule":
            name = task.submodule or ""
            if name not in entry.submodules:
                raise ValueError(
                    f"task '{key}' 引用的 submodule '{name}' 不在 entry.submodules 中")

    flow_guards = set(_GUARD_REF.findall(tasklist.flow))
    missing_guards = sorted(flow_guards - set(registry.guard_names()))
    if missing_guards:
        raise ValueError(
            f"Flow 引用的 guard 未在 entry registry 注册: {', '.join(missing_guards)}")

    pack = (Path(out_dir) if out_dir is not None
            else Path(tempfile.mkdtemp(prefix="specmodule_entry_")))
    pack.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": entry.name,
        "version": "0.1.0",
        "description": entry.description,
        "submodule": False,
        "spec_schema": {"input": dict(entry.spec_schema or {}), "output": {}},
        "requires": [],
        "modules": sorted(entry.submodules),
        "tasklist": tasklist.to_dict(),
    }
    (pack / "module.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (pack / "harnesses").mkdir(parents=True, exist_ok=True)
    for fname, data in harness_files.items():
        (pack / "harnesses" / f"{fname}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (pack / "commands").mkdir(parents=True, exist_ok=True)
    for fname, data in command_files.items():
        (pack / "commands" / f"{fname}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, fn in script_bodies.items():
        _write_fn_source(fn, name, pack, "scripts", warnings)
    for gname in sorted(flow_guards):
        _write_fn_source(registry.get_guard(gname), gname, pack, "guards", warnings)
    for key in sorted(entry.submodules):
        sub_dir = pack / "submodules" / key
        sub_dir.mkdir(parents=True, exist_ok=True)
        try:
            entry.submodules[key]().pack(sub_dir)
        except Exception as e:
            raise ValueError(f"submodule '{key}' 打包失败: {e}") from e
    return EntryPackResult(
        pack_dir=pack, template_name=tname,
        dropped_templates=dropped, warnings=warnings,
    )
