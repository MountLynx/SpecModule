# example/ppt_writer/module.py
"""ppt_writer — spec → 可机器校验的 .pptx（M2 实践线模块，双模板）。

- **ppt_render**（默认）：完整内容 spec → 归一化（normalize 纯函数）→
  command 渲染节点（python-pptx 子进程，零 LLM）→ Report。翻译器把
  归一化结果写入进程级信封（command 命令字符串是静态的，框架约束——
  数据经信封文件传递，见 normalize.read_envelope 注释）。
- **template_review**：模板制作工作流——dump（.pptx → 结构化描述）→
  硬合规（verify，与渲染器同判据）→ LLM 意见 harness（基于 dump +
  判据清单；Mock 下返回占位意见不阻断）→ guard：硬合规非空 → 输出问题
  清单 + 中止（零写入）；空 → 复制入库 reference/ + manifest 注册
  （不改源草稿）。

spec 契约：
- ppt_render：{pages: [...], sections?: [...], title?, theme?{font?}, output?}
  —— 结构与校验见 normalize.validate_spec（含字段路径的明确错误）
- template_review：{draft_pptx, template_name, layout, kind, master?}
  —— master 可选（缺省 0 = 第一个母版；多母版模板按母版索引寻址版式）；
  翻译器入口校验（同上风格）

entry.spec_schema 缺省为 None（框架级 spec_schema 是模板无关的，双模板
契约不同）——每个模板在翻译器入口做等价校验（同一套含字段路径报错）。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from llm import LLMConfig, create_llm_client

from module_harness.cli.command import CommandConfig
from module_harness.core.config import HarnessConfig
from module_harness.infra.events import EventBus
from module_harness.model.module import Module
from module_harness.core.outputfmt import OutputFormat
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import TaskDefinition, Tasklist
from module_harness.model.translator import TemplateLoader

from . import normalize

# ── 渲染器 command 配置 ──────────────────────────────────────────────
_RENDER_SCRIPT = normalize.MODULE_DIR / "render_deck.py"
_DUMP_SCRIPT = normalize.MODULE_DIR / "dump_template.py"


def _render_command_config() -> CommandConfig:
    """渲染 command：静态命令串（sys.executable + 本进程信封路径）。"""
    return CommandConfig(
        name="render_deck",
        command=(
            f'"{sys.executable}" "{_RENDER_SCRIPT}" '
            f'--envelope "{normalize._envelope_path("render")}"'
        ),
        timeout=120.0,
    )


# ── 意见 harness（制作工作流 LLM 节点；渲染路径零 LLM）──────────────
TEMPLATE_OPINION_CONFIG = HarnessConfig(
    name="template_opinion",
    prompt_core=(
        "你是 PPT 模板质量审阅专家。基于模板的结构化描述（dump）与硬合规"
        "问题清单，给出改进意见（仅文字，不要 JSON 包裹）。\n"
        "判据清单：\n"
        "- 标题占位符应命名为 title（文本类型）；\n"
        "- 要点占位符应命名为 points（文本类型，支持多段）；\n"
        "- 图片占位符应命名为 image（图片类型）；\n"
        "- 图片语义注释占位符应命名为 caption（文本类型）；\n"
        "- 不得含 SmartArt/图表/表格等本模块 v1 不支持的填充对象；\n"
        "- 版式占位区域应容纳对应内容量（要点行数、图片比例）。\n"
        "模板描述（dump JSON）：{dump}\n"
        "硬合规问题清单（空 = 已合规，此时给出可选的打磨建议）：{verify}"
    ),
    output_format=OutputFormat(type="text"),
    notdo=["虚构模板内容", "建议超出占位符/版式修改范围", "替作者直接改模板文件"],
)


# ── 制作工作流 scripts（信封读 spec 数据）────────────────────────────
def dump_draft(view: Any) -> dict[str, Any]:
    """dump script：草稿 .pptx → 结构化描述（意见 harness 输入）。"""
    from .dump_template import dump_pptx  # noqa: PLC0415
    envelope = normalize.read_envelope("review")
    return dump_pptx(envelope["draft_pptx"])


def verify_draft(view: Any) -> dict[str, Any]:
    """硬合规 script：草稿指定母版/版式 vs kind 需求（渲染器同判据）。

    ``warnings`` = 模板形态诊断（页面层手工设计 / 未注册页面清单 /
    修正指引）——advisory，不阻断合规 gate。
    """
    envelope = normalize.read_envelope("review")
    issues = normalize.verify_draft(
        envelope["draft_pptx"], envelope["layout"], envelope["kind"],
        master_index=envelope.get("master", 0),
    )
    return {
        "issues": issues,
        "compliant": not issues,
        "warnings": normalize.diagnose_template(envelope["draft_pptx"]),
    }


def report_reject(view: Any) -> dict[str, Any]:
    """拒绝分支：输出逐项问题清单，不写任何变更。"""
    verify = view.field("verify")
    issues = verify.get("issues", []) if isinstance(verify, dict) else []
    warnings = verify.get("warnings", []) if isinstance(verify, dict) else []
    return {
        "action": "rejected",
        "issues": issues,
        "warnings": warnings,
        "message": (
            "模板不合规，未入库（reference/ 与 manifest 零变更）。"
            "请按上述问题清单在 PowerPoint 中修改后重新运行本工作流。"
        ),
    }


def _safe_file_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("._")
    if not safe:
        raise ValueError(f"非法 spec: template_name '{name}' 无法转为文件名")
    return f"{safe}.pptx"


def register_template(view: Any) -> dict[str, Any]:
    """入库分支：复制草稿 → reference/ + manifest 注册（不改源草稿）。"""
    envelope = normalize.read_envelope("review")
    draft = Path(envelope["draft_pptx"])
    name = envelope["template_name"]
    layout = envelope["layout"]
    kind = envelope["kind"]
    master = envelope.get("master", 0)
    # 提交点二次硬合规（guard 已门控；这里兜底防流程漂移）
    issues = normalize.verify_draft(draft, layout, kind, master_index=master)
    if issues:
        return {"action": "rejected", "issues": issues,
                "message": "提交前复核未通过，未入库。"}
    dest = normalize.REFERENCE_DIR / _safe_file_name(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(draft, dest)  # 复制而非移动：源草稿不被修改
    entry = {"file": dest.name, "layout": layout, "kind": kind}
    if master:
        entry["master"] = master
    normalize.register_manifest(name, entry)
    return {
        "action": "registered",
        "template_name": name,
        "manifest_entry": entry,
        "copied_to": str(dest),
        "warnings": normalize.diagnose_template(draft),
        "message": f"模板已入库: {dest}（manifest[{name}] 已注册）",
    }


def ppt_report(view: Any) -> dict[str, Any]:
    """渲染结果聚合：command stdout JSON 或错误信息。

    按 bind 字段名取值（``view.field("render")``）——字段名即 task.inputs 键。
    """
    render = view.field("render")
    if not isinstance(render, dict):
        return {"status": "error", "error": f"渲染节点输出异常: {render!r}"}
    if render.get("returncode") not in (0, None):
        err = (render.get("stderr") or "").strip() or (render.get("stdout") or "").strip()
        return {"status": "error", "error": err or "渲染命令失败（无输出）"}
    try:
        result = json.loads(render.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"status": "error", "error": f"渲染输出非 JSON: {render.get('stdout')!r}"}
    result["status"] = "ok"
    return result


# ── tasklist ────────────────────────────────────────────────────────
PPT_RENDER_TASKLIST = Tasklist(
    tasks={
        "Render": TaskDefinition(type="command", command="render_deck"),
        "Report": TaskDefinition(
            type="script", script="ppt_report", inputs={"render": "Render"},
        ),
    },
    flow="[Render] --> Report",
)

TEMPLATE_REVIEW_TASKLIST = Tasklist(
    tasks={
        "Dump": TaskDefinition(type="script", script="dump_draft"),
        "Verify": TaskDefinition(type="script", script="verify_draft"),
        "Opinion": TaskDefinition(
            type="harness",
            harness="template_opinion",
            inputs={"dump": "Dump", "verify": "Verify"},
        ),
        "Reject": TaskDefinition(
            type="script", script="report_reject", inputs={"verify": "Verify"},
        ),
        "Register": TaskDefinition(type="script", script="register_template"),
    },
    flow=(
        "[Dump] --> Verify\n"
        "Verify --> Opinion\n"
        "Opinion --|has_issues|--> Reject\n"
        "Opinion --|clean|--> Register"
    ),
)


# ── 翻译器（script 类型，确定性）：spec → 信封 + tasklist ────────────
def tl_ppt_render(view: Any) -> dict[str, Any]:
    """渲染翻译器：spec → normalize → 信封（pages/output/font）→ tasklist。"""
    spec = view.field("spec")  # 翻译器合成视图具名字段（v.named 直达）
    pages = normalize.normalize(spec)  # 非法 spec 在此抛含字段路径的错误
    normalize.write_envelope("render", {
        "pages": pages,
        "output": spec.get("output") or "ppt_writer_output.pptx",
        "font": (spec.get("theme") or {}).get("font") or normalize.DEFAULT_FONT,
    })
    return PPT_RENDER_TASKLIST.to_dict()


def tl_template_review(view: Any) -> dict[str, Any]:
    """制作工作流翻译器：校验 review spec → 信封 → tasklist。"""
    spec = view.field("spec")  # 翻译器合成视图具名字段（v.named 直达）
    for field, ftype, label in (
        ("draft_pptx", str, "字符串（草稿 .pptx 路径）"),
        ("template_name", str, "字符串（逻辑模板名）"),
        ("kind", str, f"字符串（可选: {', '.join(normalize.KIND_REQUIREMENTS)}）"),
    ):
        if spec.get(field) is None:
            raise ValueError(f"非法 spec: 缺少字段 '{field}'（{label}）")
        if not isinstance(spec[field], ftype):
            raise ValueError(f"非法 spec: '{field}' 应为 {label}，实际 {type(spec[field]).__name__}")
    if spec["kind"] not in normalize.KIND_REQUIREMENTS:
        raise ValueError(
            f"非法 spec: kind '{spec['kind']}' 未知"
            f"（可选: {', '.join(normalize.KIND_REQUIREMENTS)}）"
        )
    if not isinstance(spec.get("layout"), int) or isinstance(spec.get("layout"), bool):
        raise ValueError("非法 spec: 'layout' 应为整数版式索引")
    master = spec.get("master", 0)
    if not isinstance(master, int) or isinstance(master, bool) or master < 0:
        raise ValueError("非法 spec: 'master' 应为非负整数母版索引（可选，缺省 0 = 第一个母版）")
    normalize.write_envelope("review", {
        "draft_pptx": spec["draft_pptx"],
        "template_name": spec["template_name"],
        "layout": spec["layout"],
        "kind": spec["kind"],
        "master": master,
    })
    return TEMPLATE_REVIEW_TASKLIST.to_dict()


# ── 模板声明 ────────────────────────────────────────────────────────
PPT_RENDER_TEMPLATE: dict[str, Any] = {
    "name": "ppt_render",
    "description": (
        "完整内容 spec（每页 title/content/layout）→ 归一化 → 渲染 .pptx"
        "（command 节点 + python-pptx，零 LLM，可机器校验）"
    ),
    "translation": {"type": "script", "script": "tl_ppt_render"},
    "tasklist": PPT_RENDER_TASKLIST.to_dict(),
}

TEMPLATE_REVIEW_TEMPLATE: dict[str, Any] = {
    "name": "template_review",
    "description": (
        "模板制作工作流：草稿 .pptx → dump → 硬合规 → LLM 意见 → "
        "合规入库 reference/ + manifest 注册（不合规输出问题清单、零写入）"
    ),
    "translation": {"type": "script", "script": "tl_template_review"},
    "tasklist": TEMPLATE_REVIEW_TASKLIST.to_dict(),
}

# 内置 3 页示例 spec（零配置冒烟：title + content + thanks）
DEFAULT_SPEC: dict[str, Any] = {
    "title": "SpecModule M2 演示",
    "output": "ppt_writer_output.pptx",
    "sections": [
        {"id": "intro", "title": "引言", "defaults": {"layout": "content"}},
    ],
    "pages": [
        {"index": 1, "section": "intro", "title": "SpecModule：spec → 产物", "layout": "title"},
        {
            "index": 2,
            "section": "intro",
            "title": "核心能力",
            "content": {
                "points": [
                    "spec 驱动的页面归一化",
                    "command 节点确定性渲染",
                    "模板制作工作流",
                ],
            },
        },
        {"index": 3, "title": "谢谢", "layout": "thanks"},
    ],
}


# ── registry 构建 ───────────────────────────────────────────────────
def _has_issues(view: Any) -> bool:
    """guard（边 Opinion--|has_issues|-->Reject）：src=Opinion 经其具名
    bind 字段 verify（task.inputs 键 → Verify producer）读校验输出。"""
    verify = view.field("verify")
    issues = verify.get("issues", []) if isinstance(verify, dict) else []
    return bool(issues)


def _clean(view: Any) -> bool:
    return not _has_issues(view)


def _build_registry(
    llm_client: Any,
    event_bus: EventBus | None = None,
) -> HarnessRegistry:
    """注册双模板全部组件（harness / scripts / command / guards）。

    ``event_bus`` 缺省 None → EventBus.null()。渲染路径零 LLM（Mock 冒烟
    可用）；template_opinion harness 是制作工作流唯一 LLM 节点。
    """
    reg = HarnessRegistry(
        llm_client=llm_client, event_bus=event_bus or EventBus.null()
    )
    reg.harness(TEMPLATE_OPINION_CONFIG.name, TEMPLATE_OPINION_CONFIG)
    reg.command("render_deck", _render_command_config())
    reg.script("tl_ppt_render")(tl_ppt_render)
    reg.script("tl_template_review")(tl_template_review)
    reg.script("dump_draft")(dump_draft)
    reg.script("verify_draft")(verify_draft)
    reg.script("report_reject")(report_reject)
    reg.script("register_template")(register_template)
    reg.script("ppt_report")(ppt_report)
    reg.guard("has_issues", _has_issues)
    reg.guard("clean", _clean)
    return reg


# ── 编程 API（测试 / 嵌入用）────────────────────────────────────────
def _run_template(
    spec: dict[str, Any],
    template_name: str,
    *,
    llm_client: Any = None,
    max_ticks: int = 100,
    persist: bool = True,
    status_file: bool | None = None,
):
    if llm_client is None:
        llm_client = create_llm_client(LLMConfig.from_env())
    template = (
        PPT_RENDER_TEMPLATE if template_name == "ppt_render" else TEMPLATE_REVIEW_TEMPLATE
    )
    loader = TemplateLoader()
    loader.register(template_name, template)
    mod = Module(
        spec=spec,
        template_name=template_name,
        template_loader=loader,
        llm_client=llm_client,
        registry=_build_registry(llm_client),
        review_harness=None,
        persist=persist,
        status_file=bool(persist) if status_file is None else status_file,
    )
    return mod.run(max_ticks=max_ticks)


def run_render(
    spec: dict[str, Any],
    *,
    llm_client: Any = None,
    max_ticks: int = 100,
    persist: bool = True,
):
    """构造并运行 ppt_render（渲染零 LLM；mock/测试可传 MockLLMClient）。"""
    return _run_template(spec, "ppt_render", llm_client=llm_client,
                         max_ticks=max_ticks, persist=persist)


def run_template_review(
    spec: dict[str, Any],
    *,
    llm_client: Any = None,
    max_ticks: int = 100,
    persist: bool = True,
):
    """构造并运行 template_review 制作工作流。"""
    return _run_template(spec, "template_review", llm_client=llm_client,
                         max_ticks=max_ticks, persist=persist)


# ── 模块入口（CLI discover_modules 发现）─────────────────────────────
def _registry_for(llm_client: Any, template_name: str, event_bus: EventBus) -> Any:
    return _build_registry(llm_client, event_bus)
