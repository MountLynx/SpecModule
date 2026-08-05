# module_harness/submodule.py
"""SubModule — 类式 submodule 定义 + 嵌入/完整运行 + pack 导出。"""

from __future__ import annotations

import inspect
import json
import textwrap
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from llm import LLMConfig, create_llm_client

from .builtins import register_builtin_harnesses
from .command import CommandConfig
from .config import HarnessConfig
from .events import EventBus
from .module import Module
from .registry import HarnessRegistry
from .spec import SpecSchema, Tasklist


class SpecValidationError(Exception):
    """spec 不满足 spec_schema 契约。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("spec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))


def script(name: str):
    """类内 script 标记装饰器：标记函数，__init_subclass__ 时收集。

    脚本是类体内普通函数（不绑定 self），与 @reg.script 语义一致。
    """

    def deco(fn: Callable) -> Callable:
        fn._submodule_script_name = name  # type: ignore[attr-defined]
        return fn

    return deco


class SubModule:
    """类式 submodule 定义。类属性 = 注册信息，@script 收集脚本。

    run() 内部组合 Module：注册 provides → 构造 Module → 运行。
    pack() 导出发布目录（module.json + harnesses/ + scripts/ + commands/）。
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    spec_schema: SpecSchema = SpecSchema()
    harnesses: list[HarnessConfig] = []
    commands: list[CommandConfig] = []
    requires: list[str] = []
    tasklist: Tasklist | None = None
    _scripts: dict[str, Callable] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        inherited = dict(getattr(cls, "_scripts", {}))
        collected = {
            n: fn for n, fn in cls.__dict__.items()
            if callable(fn) and getattr(fn, "_submodule_script_name", None)
        }
        cls._scripts = inherited
        cls._scripts.update(collected)

    def __init__(
        self,
        llm_client: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._event_bus = event_bus

    def _ensure_client(self) -> Any:
        """直接类使用（未注入 client）时从 env 懒创建。"""
        if self._llm_client is None:
            self._llm_client = create_llm_client(LLMConfig.from_env())
        return self._llm_client

    def _module_id(self) -> str:
        return f"{self.name}_{uuid.uuid4().hex[:6]}"

    def _build_registry(self, audit: bool) -> HarnessRegistry:
        bus = self._event_bus if audit else EventBus.null()
        reg = HarnessRegistry(llm_client=self._ensure_client(), event_bus=bus)
        for hc in self.harnesses:
            if not hc.name:
                raise ValueError(f"harnesses 配置缺少 name: {hc}")
            reg.harness(hc.name, hc)
        for cc in self.commands:
            if not cc.name:
                raise ValueError(f"commands 配置缺少 name: {cc}")
            reg.command(cc.name, cc)
        for sname, fn in self._scripts.items():
            reg.script(sname)(fn)
        register_builtin_harnesses(reg)
        return reg

    async def run(
        self,
        spec: dict[str, Any],
        *,
        tasklist: Tasklist | dict[str, Any] | None = None,
        audit: bool = False,
        max_ticks: int = 100,
    ):
        """执行 submodule。

        - tasklist=None：用自身固定 tasklist，不触发一致性审核（发布前已验证）
        - 传入自定义 tasklist：与 Module 一致，校验 + 一致性审核
        - audit=False（默认）：嵌入模式，EventBus.null() + keep_records=False
        """
        errors = self.spec_schema.validate(spec)
        if errors:
            raise SpecValidationError(errors)
        if self.tasklist is None and tasklist is None:
            raise ValueError(f"submodule '{self.name}' 未定义 tasklist")
        use_tasklist = self.tasklist if tasklist is None else tasklist
        if isinstance(use_tasklist, dict):
            use_tasklist = Tasklist.from_json(use_tasklist)
        review = None if tasklist is None else "spec_tasklist_review"
        reg = self._build_registry(audit)
        module = Module(
            spec=spec,
            tasklist=use_tasklist,
            llm_client=self._ensure_client(),
            event_bus=self._event_bus,
            module_id=self._module_id(),
            registry=reg,
            review_harness=review,
            keep_records=audit,
        )
        return await module.run(max_ticks=max_ticks)

    def pack(self, out_dir: str | Path) -> Path:
        """导出发布目录：module.json + harnesses/ + scripts/ + commands/。

        scripts/*.py = 函数源码 + 必要 import（含 @script 装饰器行），
        加载时 exec 后按函数名取注册，pack/load round-trip 无签名改写。
        """
        if not self.name:
            raise ValueError("submodule 缺少 name，无法打包")
        if self.tasklist is None:
            raise ValueError(f"submodule '{self.name}' 未定义 tasklist，无法打包")
        p = Path(out_dir)
        (p / "harnesses").mkdir(parents=True, exist_ok=True)
        (p / "scripts").mkdir(exist_ok=True)
        (p / "commands").mkdir(exist_ok=True)
        manifest = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "submodule": True,
            "spec_schema": asdict(self.spec_schema),
            "requires": list(self.requires),
            "tasklist": {
                "Tasks": {k: asdict(t) for k, t in self.tasklist.tasks.items()},
                "Flow": self.tasklist.flow,
            },
        }
        (p / "module.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for hc in self.harnesses:
            if not hc.name:
                raise ValueError(f"harnesses 配置缺少 name: {hc}")
            (p / "harnesses" / f"{hc.name}.json").write_text(
                json.dumps(hc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for cc in self.commands:
            if not cc.name:
                raise ValueError(f"commands 配置缺少 name: {cc}")
            (p / "commands" / f"{cc.name}.json").write_text(
                json.dumps(cc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for sname, fn in self._scripts.items():
            src = textwrap.dedent(inspect.getsource(fn))
            header = "from __future__ import annotations\nfrom module_harness.submodule import script\n\n"
            (p / "scripts" / f"{sname}.py").write_text(header + src, encoding="utf-8")
        return p
