# module_harness/loader.py
"""ModuleLoader — 加载发布目录为 SubModule 实例（第二层用户入口）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm import LLMConfig, create_llm_client

from .builtins import BUILTIN_HARNESS_NAMES
from .command import CommandConfig
from .config import HarnessConfig
from .events import EventBus
from .spec import SpecSchema, Tasklist
from .submodule import SubModule


class ModuleRequirementError(Exception):
    """requires 声明的名字无法在「内置集 ∪ provides」中解析。"""

    def __init__(self, missing: list[str], available: list[str]) -> None:
        self.missing = missing
        self.available = available
        super().__init__(
            "requires 无法解析: " + ", ".join(missing)
            + "\n可用: " + ", ".join(available)
        )


class ModuleManifestError(Exception):
    """module.json 缺失、损坏或缺少必需字段。"""


class ModuleLoader:
    """第二层用户入口：加载发布目录，返回可运行的 SubModule 实例。

    llm_client 优先（测试/注入用）；否则由 llm_config（None → from_env）创建。
    """

    def __init__(
        self,
        llm_config: LLMConfig | None = None,
        *,
        llm_client: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._llm_config = llm_config
        self._event_bus = event_bus

    def _ensure_client(self) -> Any:
        """llm_client 优先；否则由 llm_config（None → from_env）惰性创建。"""
        if self._llm_client is None:
            if self._llm_config is None:
                self._llm_config = LLMConfig.from_env()
            self._llm_client = create_llm_client(self._llm_config)
        return self._llm_client

    def load(self, path: str | Path) -> SubModule:
        """解析 module.json → 注册 provides → 校验 requires → 返回 SubModule。"""
        p = Path(path)
        manifest_path = p / "module.json"
        if not manifest_path.is_file():
            raise ModuleManifestError(f"缺少 module.json: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ModuleManifestError(f"module.json 解析失败: {e}") from e
        if not isinstance(manifest, dict):
            raise ModuleManifestError("module.json 顶层必须是对象")

        name = manifest.get("name")
        tasklist_data = manifest.get("tasklist")
        if not name:
            raise ModuleManifestError("module.json 缺少 'name'")
        if tasklist_data is None:
            raise ModuleManifestError("module.json 缺少 'tasklist'")
        try:
            tasklist = Tasklist.from_json(tasklist_data)
        except (ValueError, TypeError) as e:
            raise ModuleManifestError(f"tasklist 无效: {e}") from e

        harnesses = self._load_harnesses(p)
        commands = self._load_commands(p)
        scripts = self._load_scripts(p)

        schema_data = manifest.get("spec_schema", {}) or {}
        spec_schema = SpecSchema(
            input=schema_data.get("input", {}) or {},
            output=schema_data.get("output", {}) or {},
        )
        requires_raw = manifest.get("requires", []) or []
        if not isinstance(requires_raw, list) or not all(
                isinstance(r, str) for r in requires_raw):
            raise ModuleManifestError("requires 必须是字符串列表")
        requires = list(requires_raw)

        provides = {h.name for h in harnesses} | {c.name for c in commands} | set(scripts)
        all_names = [h.name for h in harnesses] + [c.name for c in commands] + list(scripts)
        dups = {n for n in all_names if all_names.count(n) > 1}
        if dups:
            raise ModuleManifestError("provides 名称重复: " + ", ".join(sorted(dups)))
        missing = [r for r in requires if r not in BUILTIN_HARNESS_NAMES and r not in provides]
        if missing:
            raise ModuleRequirementError(missing, sorted(BUILTIN_HARNESS_NAMES | provides))

        cls = type(name, (SubModule,), {
            "name": name,
            "version": manifest.get("version", "0.1.0"),
            "description": manifest.get("description", ""),
            "spec_schema": spec_schema,
            "requires": requires,
            "tasklist": tasklist,
            "harnesses": harnesses,
            "commands": commands,
            "_scripts": scripts,
        })
        return cls(llm_client=self._ensure_client(), event_bus=self._event_bus)

    def _load_harnesses(self, p: Path) -> list[HarnessConfig]:
        result: list[HarnessConfig] = []
        for f in sorted((p / "harnesses").glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cfg = HarnessConfig.from_dict(data)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                raise ModuleManifestError(f"{f} 无效: {e}") from e
            if not cfg.name:
                raise ModuleManifestError(f"{f} 缺少 'name'")
            result.append(cfg)
        return result

    def _load_commands(self, p: Path) -> list[CommandConfig]:
        result: list[CommandConfig] = []
        for f in sorted((p / "commands").glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cfg = CommandConfig.from_dict(data)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                raise ModuleManifestError(f"{f} 无效: {e}") from e
            if not cfg.name:
                raise ModuleManifestError(f"{f} 缺少 'name'")
            result.append(cfg)
        return result

    def _load_scripts(self, p: Path) -> dict[str, Any]:
        """加载 scripts/*.py 为可调用函数（exec 执行——加载目录视为可信代码）。"""
        result: dict[str, Any] = {}
        for f in sorted((p / "scripts").glob("*.py")):
            ns: dict[str, Any] = {}
            try:
                exec(compile(f.read_text(encoding="utf-8"), str(f), "exec"), ns)
            except Exception as e:  # 脚本自身报错视为清单错误
                raise ModuleManifestError(f"{f} 加载失败: {e}") from e
            fn = ns.get(f.stem)
            if not callable(fn):
                raise ModuleManifestError(f"{f} 未定义函数 {f.stem}")
            result[f.stem] = fn
        return result
