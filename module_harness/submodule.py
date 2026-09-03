# module_harness/submodule.py
"""SubModule — 类式 submodule 定义 + 嵌入/完整运行 + pack 导出。"""

from __future__ import annotations

import inspect
import json
import textwrap
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Literal

from llm import LLMConfig, create_llm_client

from .builtins import register_builtin_harnesses
from .command import CommandConfig
from .config import HarnessConfig
from .events import EventBus
from .module import Module
from .registry import HarnessRegistry
from .spec import SpecSchema, SpecValidationError, Tasklist


def script(name: str):
    """类内 script 标记装饰器：标记函数，__init_subclass__ 时收集。

    脚本是类体内普通函数（不绑定 self），与 @reg.script 语义一致。
    注册名必须与函数名一致（函数名 = 注册名 = 打包文件名）。
    """

    def deco(fn: Callable) -> Callable:
        if name != fn.__name__:
            raise ValueError(
                f"script 注册名 '{name}' 与函数名 '{fn.__name__}' 不一致"
            )
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
    guards: list[tuple[str, Callable]] = []   # [(名字, 函数)]，名字 = 注册名 = 打包文件名
    modules: dict[str, type["SubModule"]] = {}  # submodule 节点引用表 {tasklist 名: 类}
    tasklist: Tasklist | None = None
    mode: Literal["persist", "fast"] = "persist"
    # 发布者声明轻量特性："fast" = 快速模式（NullBackend 全内存，零落盘零 I/O，
    # D11）；默认 "persist" 落盘到 .specmodule/runs/<run_id>/（D9）。
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
        # 列表类属性按子类复制，防止子类就地修改污染父类注册
        for attr in ("harnesses", "commands", "requires", "guards"):
            if attr not in cls.__dict__:
                setattr(cls, attr, list(getattr(cls, attr)))
        # dict 类属性同理由：按子类复制，防止子类就地修改污染父类注册
        if "modules" not in cls.__dict__:
            setattr(cls, "modules", dict(getattr(cls, "modules")))

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

    def _build_registry(
        self,
        audit: bool,
        harness_overrides: dict[str, Any] | None = None,
        *,
        llm_client: Any = None,
        event_bus: EventBus | None = None,
    ) -> HarnessRegistry:
        # 事件投递与 keep_records/persist 解耦：宿主传了 event_bus 就始终投递
        # （与 audit 无关）；未传则静默 EventBus.null()（嵌入零开销）。audit 只
        # 在 run() 里映射 keep_records。
        bus = event_bus if event_bus is not None else (self._event_bus or EventBus.null())
        client = llm_client if llm_client is not None else self._ensure_client()
        reg = HarnessRegistry(llm_client=client, event_bus=bus)
        for hc in self.harnesses:
            if not hc.name:
                raise ValueError(f"harnesses 配置缺少 name: {hc}")
            cfg = (
                self._apply_harness_overrides(hc, harness_overrides)
                if harness_overrides else hc
            )
            reg.harness(cfg.name, cfg)
        for cc in self.commands:
            if not cc.name:
                raise ValueError(f"commands 配置缺少 name: {cc}")
            reg.command(cc.name, cc)
        for sname, fn in self._scripts.items():
            reg.script(sname)(fn)
        for gname, gfn in self.guards:
            reg.guard(gname, gfn)
        register_builtin_harnesses(reg)
        return reg

    @staticmethod
    def _apply_harness_overrides(
        hc: HarnessConfig, overrides: dict[str, Any]
    ) -> HarnessConfig:
        """批量应用 LLM 覆盖（model/temperature/think/api_params）到单个 harness。"""
        api_params = dict(hc.api_params)
        if overrides.get("api_params"):
            api_params.update(overrides["api_params"])
        return HarnessConfig(
            name=hc.name,
            prompt_core=hc.prompt_core,
            prompt_modes=dict(hc.prompt_modes),
            output_format=hc.output_format,
            notdo=list(hc.notdo),
            model=overrides.get("model", hc.model),
            temperature=overrides.get("temperature", hc.temperature),
            think=overrides.get("think", hc.think),
            api_params=api_params,
        )

    async def run(
        self,
        spec: dict[str, Any],
        *,
        tasklist: Tasklist | dict[str, Any] | None = None,
        audit: bool = False,
        max_ticks: int = 100,
        harness_overrides: dict[str, Any] | None = None,
        persist: bool | None = None,
        llm_client: Any = None,
        event_bus: EventBus | None = None,
        hooks: dict | None = None,
    ) -> list[Any]:
        """执行 submodule。

        - tasklist=None：用自身固定 tasklist，不触发一致性审核（发布前已验证）
        - 传入自定义 tasklist：与 Module 一致，校验 + 一致性审核
        - harness_overrides：{model/temperature/think/api_params} 覆盖，
          构建 registry 时应用到 submodule 自身的全部 harness（不含内置
          harness）（submodule 节点 LLM 配置传播）
        - audit=False（默认）：嵌入模式，keep_records=False；除非 mode="fast"，
          嵌入模式同样落盘（D11）
        - audit=True：keep_records 全开（全量审计轨迹）
        - 事件投递与 records/persist 解耦：构造传入 event_bus 时事件始终投递
          （与 audit 取值无关）；未传则静默 EventBus.null()（嵌入零开销）。宿主
          需失败原因等现场反馈时，传 event_bus 选择性订阅即可，无需开启审计
        - persist：False = 快速模式（NullBackend 全内存 + 无 status.json +
          无 stream.log，零落盘零 I/O）；None = 按 mode 决定（"fast" → False，
          否则 True）
        - llm_client/event_bus：覆盖实例级注入（宿主进程传入）；None 用实例值
        - hooks：runner hooks 透传（观察通道，与 Module hooks 同语义）
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
        use_persist = persist if persist is not None else (self.mode != "fast")
        use_client = llm_client if llm_client is not None else self._ensure_client()
        use_bus = event_bus if event_bus is not None else self._event_bus
        reg = self._build_registry(audit, harness_overrides, llm_client=use_client, event_bus=use_bus)
        module = Module(
            spec=spec,
            tasklist=use_tasklist,
            llm_client=use_client,
            event_bus=use_bus,
            module_id=self._module_id(),
            module=self.name,  # 溯源：status.json "module" 键（与 entry 路径一致）
            registry=reg,
            review_harness=review,
            keep_records=audit,
            persist=use_persist,
            status_file=use_persist,
            # fast 模式 = 零残留模式：三个落盘通道（run.sqlite /
            # status.json / stream.log）由 mode 一并关闭。stream_log 的
            # 默认 True 是刻意的（CLI 拉起的子进程零接线即可流式观测），
            # 故只在具名模式侧统一关——直接构造 Module 无"模式"概念，
            # 每个通道由调用方逐个点名。
            stream_log=use_persist,
            modules=self.modules,
            hooks=hooks,
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
        (p / "guards").mkdir(exist_ok=True)
        (p / "submodules").mkdir(exist_ok=True)
        manifest = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "submodule": True,
            "spec_schema": asdict(self.spec_schema),
            "requires": list(self.requires),
            "modules": list(self.modules),
            "tasklist": self.tasklist.to_dict(),
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
        for gname, gfn in self.guards:
            if gname != gfn.__name__:
                raise ValueError(
                    f"guard 注册名 '{gname}' 与函数名 '{gfn.__name__}' 不一致"
                    "（注册名 = 打包文件名 = 加载键，与 @script 同约定）"
                )
            src = textwrap.dedent(inspect.getsource(gfn))
            header = "from __future__ import annotations\n\n"
            (p / "guards" / f"{gname}.py").write_text(header + src, encoding="utf-8")
        for mname, mcls in self.modules.items():
            mcls().pack(p / "submodules" / mname)
        return p
