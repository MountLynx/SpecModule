"""组装：registry 完整性（翻译器引用的每个 script/command 都已注册）+ 守卫。

三向漂移钉子（docstring 命令表为唯一契约文本）：
  1. 任务名/超时 ↔ 表       — Task 7（test_ppt_master_translator.py）
  2. 注册 config tool/args ↔ 表 — 本文件（Task 8）
  3. 翻译任务引用 ↔ registry   — 本文件 test_registry_covers_translator_tasks
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from example.ppt_master import workspace
from example.ppt_master.module import GENERATE_TEMPLATE, _build_registry
from example.ppt_master.spec_schema import validate_ppt_spec
from example.ppt_master.test_support import sample_spec
from example.ppt_master.translator import build_generate_tasklist
from module_harness.cli.entry import discover_modules
from module_harness.model.spec import TaskDefinition, Tasklist
from module_harness.model.translator import TasklistValidator

_COMMAND_NAMES = (
    "ppt_early_gate", "ppt_final_gate", "ppt_split_notes",
    "ppt_finalize", "ppt_export", "ppt_export_nonotes",
)


def _validated_tasklist(spec: dict[str, Any], reg: Any) -> list[str]:
    """spec → 展开 → Tasklist → validator 错误列表（空 = 合法）。"""
    tasks, flow = build_generate_tasklist(spec)
    tasklist = Tasklist(
        tasks={k: TaskDefinition.from_dict(v) for k, v in tasks.items()},
        flow=flow,
    )
    return TasklistValidator.validate(tasklist, reg)


def test_registry_covers_translator_tasks(tmp_path, monkeypatch):
    """sample_spec（10 页 + 图像 + notes，覆盖最多节点的形状）翻译出的每个
    script/command 引用必须已在 registry 注册——validator 与翻译校验同闸。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    reg = _build_registry(llm_client=object())
    errors = _validated_tasklist(sample_spec(), reg)
    assert not errors, errors


def test_registry_covers_topic_branch(tmp_path, monkeypatch):
    """topic 源分支：Research/research_node 引用同样过 validator 闸。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    reg = _build_registry(llm_client=object())
    spec = {
        "project": "support-topic",
        "source": {"kind": "topic", "topic": "量子计算"},
        "roster": [{"id": "p01", "title": "页1"}, {"id": "p02", "title": "页2"}],
    }
    assert "research_node" in {
        t["script"] for t in build_generate_tasklist(spec)[0].values()
        if t["type"] == "script"
    }, "topic 源应引 research_node"
    errors = _validated_tasklist(spec, reg)
    assert not errors, errors


def test_template_declares_script_translation():
    assert GENERATE_TEMPLATE["translation"] == {
        "type": "script", "script": "tl_generate"}
    assert GENERATE_TEMPLATE["name"] == "generate"


def test_registry_registers_translator_script(tmp_path, monkeypatch):
    """翻译入口 tl_generate 必须注册——Module.run 翻译期 get_body 取用。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    reg = _build_registry(llm_client=object())
    assert reg.is_script("tl_generate"), "翻译脚本 tl_generate 未注册"


def _docstring_command_table() -> dict[str, tuple[str, str, float]]:
    """解析 translator.py 模块 docstring 命令表 → {名: (tool, args, 超时秒)}。

    与 translator 测试的 _command_table() 同法（Task 7 钉任务名/超时），
    本解析器扩展到 tool/args 列——注册 config 漂移第三向钉死。
    """
    from example.ppt_master import translator

    table: dict[str, tuple[str, str, float]] = {}
    for line in (translator.__doc__ or "").splitlines():
        m = re.fullmatch(r"\s+(ppt_\w+)\s+(.*?)\s+(\d+)s\s*", line)
        if m:
            tool, _, args = m.group(2).partition(" ")
            table[m.group(1)] = (tool, args, float(m.group(3)))
    assert len(table) >= 6, f"docstring 命令表解析不足 6 行（表格式变了？）: {table}"
    return table


def _split_registered_command(cmd: str) -> tuple[str, str]:
    """注册命令串 → (tool, args)。形如 ``… --tool <tool>[ -- <args>]``。"""
    after = cmd.split("--tool ", 1)[1]
    tool, _, args = after.partition(" -- ")
    return tool.strip(), args.strip()


def test_registered_commands_match_docstring_table(tmp_path, monkeypatch):
    """六个注册命令的 tool/args/timeout ↔ docstring 命令表逐项一致（防漂移）。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    reg = _build_registry(llm_client=object())
    table = _docstring_command_table()
    assert set(_COMMAND_NAMES) == set(table), (
        f"注册命令名集合与 docstring 命令表不一致: "
        f"{set(_COMMAND_NAMES) ^ set(table)}"
    )
    for name in _COMMAND_NAMES:
        cfg = reg.command_config(name)
        assert cfg is not None, f"命令 '{name}' 未在 registry 注册"
        got_tool, got_args = _split_registered_command(cfg.command)
        assert (got_tool, got_args, cfg.timeout) == table[name], (
            f"命令 '{name}' 注册 config 与 docstring 表漂移: "
            f"got=({got_tool!r}, {got_args!r}, {cfg.timeout}) "
            f"want={table[name]}"
        )
        assert "run_tool.py" in cfg.command, f"'{name}' 未走 run_tool.py 入口"
        assert str(tmp_path) in cfg.command, f"'{name}' 信封路径未随 _ENVELOPE_DIR"


def test_entry_default_spec_is_valid_reagent(tmp_path, monkeypatch):
    """入口 default_spec 是 webview「spec 参考」/CLI 无 spec 通道的试剂：
    契约校验 + 翻译层双闸全通（缺省回填后形状即 translator 所依赖）。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    entries = discover_modules(Path(__file__).parent / "modules")
    spec = copy.deepcopy(entries["ppt_master"].default_spec)
    assert spec is not None, "ppt_master 入口未声明 default_spec"
    validate_ppt_spec(spec)  # 回填缺省（原地进行，故上一步 deepcopy）
    reg = _build_registry(llm_client=object())
    errors = _validated_tasklist(spec, reg)
    assert not errors, errors
