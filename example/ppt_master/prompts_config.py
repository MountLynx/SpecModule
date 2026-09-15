"""5 类 LLM 节点的 HarnessConfig + 参考包组装。

Layer 1（prompt_core）= 角色纪律 + {占位符}；Layer 2 v1 预留（prompt_modes
恒空，不用 promptmode 即无 KeyError 面）；Layer 3（prompt_extra）= 参考包
（prompts/ 素材拼接，调用时读文件）。参考包是确定性拼装，非 prompt 工程。
"""

from __future__ import annotations

from pathlib import Path

from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

REQUIRED_ASSETS = (
    "strategist.md", "plan-core.md", "executor-base.md",
    "shared-standards-core.md", "semantic-svg.md",
    "preset-shape-vocabulary.md", "executor-notes.md",
    # image-base：图像风格参考，v1 不入包（图像节点直用行 prompt）
    "image-base.md",
    "design_spec_reference.md", "spec_lock_reference.md",
)


def _asset(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _pack(*names: str) -> str:
    return "\n\n".join(f"## 参考：{n}\n\n{_asset(n)}" for n in names)


def plan_prompt_pack() -> str:
    return _pack("strategist.md", "plan-core.md",
                 "design_spec_reference.md", "spec_lock_reference.md")


def page_prompt_pack() -> str:
    return _pack("executor-base.md", "shared-standards-core.md",
                 "semantic-svg.md", "preset-shape-vocabulary.md")


def repair_prompt_pack() -> str:
    return _pack("executor-base.md", "shared-standards-core.md")


def notes_prompt_pack() -> str:
    return _pack("executor-notes.md")


_PLAN_CORE = (
    "你是演示文稿规划者（Strategist）。依据已确认契约（spec）与源事实，"
    "一次性产出完整 design_spec.md 与 spec_lock.md（语法见参考包），"
    "并以 JSON 收据返回。硬规则：页册神圣——不得增删页、改序、改标题；"
    "spec 已确认字段为 Literal/Semantic 要求逐字保留；空缺字段你裁量并在"
    "收据 provenance 标 planner-decided。\n"
    # 收据括号串为 schema 提示；PromptRenderer 仅替换 \w+ 键，逗号组原样透传
    "收据 JSON：{status, roster_ids, design_spec_md, spec_lock_md, "
    "image_rows, icon_pool, notes_enabled}\n"
    "占位符 —— 契约：{contract}；页册：{roster}；源摘要：{source_digest}"
)

_PAGE_CORE = (
    "你是演示文稿页面作者（Executor）。依据页面任务书产出**一个完整 SVG 页**"
    "（1280x720 viewBox），只输出 SVG 本体，无任何包裹文字。\n"
    "占位符 —— 页块：{page}；执行锁：{lock}；校准表：{calibration}"
)

_REPAIR_CORE = (
    "你是页面修复者。依据质量门报告修复指定页，重写完整 SVG，只输出 SVG 本体。"
    "修在拥有层：页内错修页；同类问题跨页复现（方法级偏差）则在修复说明中指出"
    "应改校准/规则。\n占位符 —— 页块：{page}；锁：{lock}；校准表：{calibration}；"
    "问题清单：{issues}"
)

_NOTES_CORE = (
    "你是讲者备注作者。基于最终页 SVG 逐页写备注，输出 total.md 全文"
    "（含每页 `# <页id>` 小节）。占位符 —— 页册：{roster}；页 SVG 摘要：{pages_digest}"
)

_RESEARCH_CORE = (
    "你是事实研究员。只针对既述信息缺口研究并输出 JSON："
    "{research_md, facts}。facts 为 [{id, claim, url}]。不得编造可核查断言。"
    "占位符 —— 主题：{topic}；缺口：{gaps}"
)


def plan_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_plan", prompt_core=_PLAN_CORE,
        output_format=OutputFormat(type="json_object"),
        notdo=["增删或重排页册", "改页标题", "虚构可核查事实"],
    )


def page_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_page", prompt_core=_PAGE_CORE,
        output_format=OutputFormat(type="text"),
        notdo=["输出非 SVG 内容", "引入锁外新色/新字体"],
    )


def repair_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_repair", prompt_core=_REPAIR_CORE,
        output_format=OutputFormat(type="text"),
        notdo=["改页册结构", "输出非 SVG 内容"],
    )


def notes_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_notes", prompt_core=_NOTES_CORE,
        output_format=OutputFormat(type="text"),
    )


def research_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_research", prompt_core=_RESEARCH_CORE,
        output_format=OutputFormat(type="json_object"),
    )


def image_config(image_dir: str) -> HarnessConfig:
    return HarnessConfig(
        name="ppt_image", prompt_core="{image_prompt}", mode="image",
        image_dir=image_dir,
    )
