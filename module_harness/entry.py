# module_harness/entry.py
"""模块入口合约：ModuleEntry + 目录发现（roadmap Phase 0，CLI 使用）。

一个 module 一个 py 文件（``modules/<name>.py``），文件内声明模块级
``entry`` 变量。未来 ``init`` 脚手架可据此生成实例骨架
（scripts/harnesses/submodules/modules 分目录）。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .events import EventBus
from .module import Module
from .registry import HarnessRegistry
from .spec import Tasklist
from .translator import TemplateLoader

log = logging.getLogger(__name__)


@dataclass
class ModuleEntry:
    """模块入口声明：模板 + submodule + registry 构建 + 默认 spec/schema。"""

    name: str
    description: str
    templates: dict[str, dict]                                   # {模板名: TasklistTemplate JSON}
    submodules: dict[str, type] = field(default_factory=dict)    # {tasklist 名: SubModule 类}
    build_registry: Callable[[Any, str, EventBus], HarnessRegistry] | None = None
    default_spec: dict[str, Any] | None = None
    default_template: str | None = None
    spec_schema: dict[str, str] | None = None                    # {字段: 类型名}
    review_harness: str | None = "spec_tasklist_review"

    def __post_init__(self) -> None:
        if self.default_template is not None and self.default_template not in self.templates:
            raise ValueError(f"default_template '{self.default_template}' 不在 templates 中")

    def build_module(
        self,
        spec: dict[str, Any],
        *,
        template_name: str | None = None,
        tasklist: Tasklist | None = None,
        llm_client: Any,
        module_id: str | None = None,
        base_dir: Path | None = None,
        event_bus: EventBus | None = None,
        hooks: dict | None = None,
        review: bool = True,
    ) -> Module:
        """统一接线：entry 数据 → Module（CLI/MCP 共用，消除接线重复）。

        template/tasklist 二选一（Module 不变量）；tasklist 给出时
        template_name 归一化为 None（与 CLI 语义一致）；template_name 缺省
        回落 ``default_template``（与 CLI ``args.template or default_template``
        语义一致）；未注册模板 → ValueError 携带可用清单。review=False →
        review_harness=None（存档续跑等无需重审场景）；缺省用 entry.review_harness。
        """
        if tasklist is not None:
            template_name = None
        elif template_name is None:
            template_name = self.default_template
        if template_name is not None and template_name not in self.templates:
            raise ValueError(
                f"模板 '{template_name}' 未注册——可用: {', '.join(sorted(self.templates))}"
            )
        if event_bus is None:
            event_bus = EventBus()
        if self.build_registry is not None:
            registry = self.build_registry(llm_client, template_name, event_bus)
        else:
            registry = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)
        loader = TemplateLoader()
        for name, data in self.templates.items():
            loader.register(name, data)
        return Module(
            spec=spec,
            template_name=template_name,
            tasklist=tasklist,
            llm_client=llm_client,
            event_bus=event_bus,
            template_loader=loader,
            module_id=module_id,
            base_dir=base_dir,
            registry=registry,
            review_harness=self.review_harness if review else None,
            modules=self.submodules,
            hooks=hooks,
        )


def discover_modules(modules_dir: Path | str) -> dict[str, ModuleEntry]:
    """扫描 ``modules_dir/*.py``，导入后收集模块级 ``entry`` 变量。

    缺 ``entry`` 或类型不符的文件跳过并 log 警告；同名冲突后者覆盖 + 警告；
    文件导入抛异常跳过 + log exception（不阻断整体发现）。
    """
    out: dict[str, ModuleEntry] = {}
    d = Path(modules_dir)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"specmodule_module_{p.stem}", p
        )
        if spec is None or spec.loader is None:
            log.warning("无法加载模块入口文件（跳过）: %s", p)
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            log.exception("模块入口加载失败（跳过）: %s", p)
            continue
        entry = getattr(mod, "entry", None)
        if not isinstance(entry, ModuleEntry):
            log.warning("文件 %s 缺少 entry 变量（ModuleEntry）——跳过", p)
            continue
        if entry.name in out:
            log.warning("模块名 '%s' 重复（%s 覆盖）", entry.name, p)
        out[entry.name] = entry
    return out