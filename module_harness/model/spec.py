# module_harness/spec.py
"""Spec 与 Tasklist 数据模型。"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal


class Spec:
    """结构化 spec，用户自由定义字段的键值对集合。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"Spec({self._data!r})"

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass
class TaskDefinition:
    """tasklist 中单个 Task 的定义。与 HarnessConfig 字段对齐。"""

    type: Literal["harness", "script", "command", "submodule"]
    harness: str | None = None
    script: str | None = None
    command: str | None = None          # type="command" 时引用的命令名
    submodule: str | None = None        # type="submodule" 时引用名（父模块 modules 解析）
    outputs: dict[str, str] | None = None  # submodule 输出映射 {节点字段: 子输出字段}；缺省 = 全量
    timeout: float | None = None        # command 超时覆盖（秒）
    cwd: str | None = None              # command 工作目录覆盖
    promptmode: str | None = None
    prompt: str | None = None
    outputformat: dict[str, Any] | None = None
    notdo: list[str] | None = None
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None
    api_params: dict[str, Any] | None = None  # 透传给 LLM SDK 的额外参数
    mode: str | None = None             # "image" = 图像生成节点
    image_size: str | None = None       # 图像尺寸覆盖
    image_dir: str | None = None        # 图像落盘目录覆盖
    validate_retries: int | None = None  # 校验失败重试预算覆盖（None = 沿用注册 config）
    inputs: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskDefinition":
        return cls(
            type=d["type"],
            harness=d.get("harness"),
            script=d.get("script"),
            command=d.get("command"),
            submodule=d.get("submodule"),
            outputs=d.get("outputs"),
            timeout=d.get("timeout"),
            cwd=d.get("cwd"),
            promptmode=d.get("promptmode"),
            prompt=d.get("prompt"),
            outputformat=d.get("outputformat"),
            notdo=d.get("notdo"),
            model=d.get("model"),
            temperature=d.get("temperature"),
            think=d.get("think"),
            api_params=d.get("api_params"),
            mode=d.get("mode"),
            image_size=d.get("image_size"),
            image_dir=d.get("image_dir"),
            validate_retries=d.get("validate_retries"),
            inputs=d.get("inputs"),
        )


@dataclass
class ArtifactDecl:
    """run 产物声明（tasklist 顶层 ``Artifacts`` 项）：模块作者显式声明产出。

    path 是具体 glob 串（相对路径按运行进程 cwd 解析；spec 派生路径由翻译
    脚本自行插值，库不做模板机制）。收集语义见 infra/artifacts.py。
    """

    name: str                    # 展示名（消费端 label，可中文）
    path: str                    # 具体 glob 串；相对路径按运行进程 cwd 解析
    kind: str = "intermediate"   # "deliverable"（交付物）| "intermediate"（中间产物）
    pick: str = "all"            # "all"（全收）| "latest"（匹配集取 mtime 最新一个）

    _KINDS = ("deliverable", "intermediate")
    _PICKS = ("all", "latest")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArtifactDecl":
        if not isinstance(d, dict):
            raise ValueError("Artifacts 项应为 dict")
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Artifacts 项 'name' 应为非空字符串")
        path = d.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Artifacts 项 'path' 应为非空字符串")
        kind = d.get("kind", "intermediate")
        if kind not in cls._KINDS:
            raise ValueError(
                f"Artifacts 项 'kind' 应为 {'/'.join(cls._KINDS)} 之一，得 {kind!r}"
            )
        pick = d.get("pick", "all")
        if pick not in cls._PICKS:
            raise ValueError(
                f"Artifacts 项 'pick' 应为 {'/'.join(cls._PICKS)} 之一，得 {pick!r}"
            )
        return cls(name=name, path=path, kind=kind, pick=pick)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path,
                "kind": self.kind, "pick": self.pick}


@dataclass
class Tasklist:
    """完整的 tasklist：Tasks + Flow + Artifacts（产物声明，可选）。"""

    tasks: dict[str, TaskDefinition]
    flow: str
    artifacts: list[ArtifactDecl] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Tasklist":
        if "Tasks" not in data:
            raise ValueError("tasklist 缺少 'Tasks' 字段")
        if "Flow" not in data:
            raise ValueError("tasklist 缺少 'Flow' 字段")
        tasks = {
            key: TaskDefinition.from_dict(td)
            for key, td in data["Tasks"].items()
        }
        raw_artifacts = data.get("Artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise ValueError("tasklist 'Artifacts' 应为 list")
        artifacts = [ArtifactDecl.from_dict(a) for a in raw_artifacts]
        return cls(tasks=tasks, flow=data["Flow"], artifacts=artifacts)

    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化 dict（与 ``from_json`` 对称）——唯一实现（S4）。"""
        out: dict[str, Any] = {
            "Tasks": {k: dataclasses.asdict(v) for k, v in self.tasks.items()},
            "Flow": self.flow,
        }
        if self.artifacts:
            out["Artifacts"] = [a.to_dict() for a in self.artifacts]
        return out


@dataclass
class TranslationSpec:
    """翻译方式声明。"""

    type: Literal["harness", "script"]
    harness: str | None = None
    script: str | None = None
    prompt: str | None = None
    prompt_core: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TranslationSpec":
        return cls(
            type=d["type"],
            harness=d.get("harness"),
            script=d.get("script"),
            prompt=d.get("prompt"),
            prompt_core=d.get("prompt_core"),
        )


@dataclass
class TasklistTemplate:
    """tasklist 模板 = 翻译声明 + tasklist 骨架。"""

    name: str
    description: str
    translation: TranslationSpec
    tasklist: Tasklist

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TasklistTemplate":
        if "name" not in data:
            raise ValueError("模板缺少 'name' 字段")
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            translation=TranslationSpec.from_dict(data["translation"]),
            tasklist=Tasklist.from_json(data["tasklist"]),
        )


_SCHEMA_TYPES: dict[str, type] = {
    "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict,
}


def _value_matches(value: Any, type_name: str) -> bool:
    """判断值是否满足类型声明。bool 与 int 严格区分。"""
    if type_name == "any":
        return True
    expected = _SCHEMA_TYPES.get(type_name)
    if expected is None:
        return False
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


@dataclass
class SpecSchema:
    """submodule 的 spec 契约：input 校验，output 仅声明。"""

    input: dict[str, str] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)

    def validate(self, spec: dict[str, Any]) -> list[str]:
        """校验 spec 是否满足契约。返回错误列表，空 = 通过。

        声明的字段必须存在且类型匹配；未声明的字段允许存在。
        """
        errors: list[str] = []
        for field_name, type_name in self.input.items():
            if field_name not in spec:
                errors.append(f"缺少字段 '{field_name}'（应为 {type_name}）")
                continue
            if not _value_matches(spec[field_name], type_name):
                errors.append(
                    f"字段 '{field_name}' 类型错误：期望 {type_name}，"
                    f"实际 {type(spec[field_name]).__name__}"
                )
        return errors


class SpecValidationError(Exception):
    """spec 不满足 spec_schema 契约。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("spec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
