# module_harness/spec.py
"""Spec 与 Tasklist 数据模型。"""

from __future__ import annotations

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

    type: Literal["harness", "script"]
    harness: str | None = None
    script: str | None = None
    promptmode: str | None = None
    prompt: str | None = None
    outputformat: dict[str, Any] | None = None
    notdo: list[str] | None = None
    model: str | None = None
    temperature: float | None = None
    think: bool | dict | None = None
    inputs: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskDefinition":
        return cls(
            type=d["type"],
            harness=d.get("harness"),
            script=d.get("script"),
            promptmode=d.get("promptmode"),
            prompt=d.get("prompt"),
            outputformat=d.get("outputformat"),
            notdo=d.get("notdo"),
            model=d.get("model"),
            temperature=d.get("temperature"),
            think=d.get("think"),
            inputs=d.get("inputs"),
        )


@dataclass
class Tasklist:
    """完整的 tasklist：Tasks + Flow。"""

    tasks: dict[str, TaskDefinition]
    flow: str

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
        return cls(tasks=tasks, flow=data["Flow"])


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
