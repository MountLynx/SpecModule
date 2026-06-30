"""翻译器：TasklistValidator + TemplateLoader + Translator。"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

from tickflow import Failure, parse as parse_graph
from tickflow.views import DictView, Resolved

from .spec import Spec, Tasklist, TasklistTemplate, TaskDefinition
from .registry import HarnessRegistry


class TasklistValidator:
    """校验 tasklist 的结构合法性与引用完整性。"""

    @staticmethod
    def validate(tasklist: Tasklist, registry: HarnessRegistry) -> list[str]:
        """返回问题列表，空列表 = 合法。"""
        errors: list[str] = []

        for key, task in tasklist.tasks.items():
            errors.extend(TasklistValidator._check_task(key, task, registry))

        errors.extend(TasklistValidator._check_flow(tasklist))
        return errors

    @staticmethod
    def _check_task(key: str, task: TaskDefinition, registry: HarnessRegistry) -> list[str]:
        errors: list[str] = []

        if task.type == "harness":
            if not task.harness:
                errors.append(f"Task '{key}': type='harness' 但缺少 'harness' 字段")
            elif not registry.is_harness(task.harness) and not registry.has_body(task.harness):
                errors.append(f"Task '{key}': harness '{task.harness}' 未在 registry 中注册")
        elif task.type == "script":
            if not task.script:
                errors.append(f"Task '{key}': type='script' 但缺少 'script' 字段")
            elif not registry.is_script(task.script) and not registry.has_body(task.script):
                errors.append(f"Task '{key}': script '{task.script}' 未在 registry 中注册")
        else:
            errors.append(f"Task '{key}': 未知 type '{task.type}'")

        return errors

    @staticmethod
    def _check_flow(tasklist: Tasklist) -> list[str]:
        errors: list[str] = []
        task_keys = set(tasklist.tasks.keys())

        # 解析 flow 得到 node 名（简单正则提取 mermaid 中的节点）
        # 匹配 A --> B, [A]-->B, A--|g|-->B 等
        node_names: set[str] = set()
        flow = tasklist.flow
        # 匹配 start marker [X]
        for m in re.finditer(r'\[(\w+)\]', flow):
            node_names.add(m.group(1))
        # 匹配 X--|...|-->Y 和 X-->Y
        for m in re.finditer(r'(\w+)\s*(?:--\|?\w*\|?)?-->', flow):
            node_names.add(m.group(1))
        for m in re.finditer(r'-->\s*(\w+)', flow):
            node_names.add(m.group(1))

        # 检查 flow 中出现但不在 tasks 中的节点
        for node in node_names:
            if node not in task_keys:
                errors.append(f"Flow 中引用了未定义的节点 '{node}'")

        # 检查 tasks 中定义了但不在 flow 中的孤立节点
        for key in task_keys:
            if key not in node_names:
                errors.append(f"Task '{key}' 在 Flow 中未被引用（孤立节点）")

        # 尝试 tickflow parse 检测语法问题
        try:
            parse_graph(flow)
        except Exception as e:
            errors.append(f"Flow 解析失败: {e}")

        return errors


class TemplateLoader:
    """加载与管理 tasklist 模板。"""

    def __init__(self) -> None:
        self._templates: dict[str, TasklistTemplate] = {}

    def register(self, name: str, data: dict[str, Any]) -> None:
        """注册一个模板（代码调用或文件加载）。data 直接过 from_json。"""
        self._templates[name] = TasklistTemplate.from_json(data)

    def get(self, name: str) -> TasklistTemplate | None:
        return self._templates.get(name)

    def list_names(self) -> list[str]:
        return list(self._templates.keys())

    def load_directory(self, path: str | Path) -> int:
        """从目录加载 .json 模板文件。返回加载数量。"""
        p = Path(path)
        count = 0
        if p.is_dir():
            for f in sorted(p.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self.register(data["name"], data)
                    count += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass  # 跳过无效文件
        return count

    def load_builtins(self) -> int:
        """加载内置模板（module_harness/templates/builtin/）。"""
        builtin_dir = Path(__file__).parent / "templates" / "builtin"
        return self.load_directory(builtin_dir)


class Translator:
    """spec -> tasklist 翻译器。直接调 body/script，不走 tickflow runner。"""

    def __init__(self, registry: HarnessRegistry) -> None:
        self.reg = registry

    async def translate(self, spec: Spec, template: TasklistTemplate) -> Tasklist:
        """执行翻译并返回校验通过的 Tasklist。"""
        ts = template.translation

        if ts.type == "script":
            tasks_dict = await self._call_script_translator(ts.script, spec)
        elif ts.type == "harness":
            tasks_dict = await self._call_harness_translator(ts.harness, ts.prompt, spec)
        else:
            raise ValueError(f"不支持的 translation type: {ts.type}")

        # 构建 tasklist 并校验
        tasklist = Tasklist(tasks={
            key: TaskDefinition.from_dict(td) if isinstance(td, dict) else td
            for key, td in tasks_dict.items()
        }, flow=template.tasklist.flow)

        errors = TasklistValidator.validate(tasklist, self.reg)
        if errors:
            raise ValueError(f"翻译结果校验失败:\n" + "\n".join(f"  - {e}" for e in errors))

        return tasklist

    async def _call_script_translator(self, script_name: str, spec: Spec) -> dict:
        """直接调用已注册的 script 函数。"""
        body = self.reg.get_body(script_name)
        view = DictView(
            {"spec": Resolved(value=spec.to_dict(), k=None)},
            node="__translator__",
        )
        result = body(view)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _call_harness_translator(self, harness_name: str, prompt_extra: str, spec: Spec) -> dict:
        """调用 harness body（异步 LLM），parse 返回的 JSON 为 task dict。"""
        body = self.reg.get_body(harness_name)
        view = DictView(
            {"spec": Resolved(value=spec.to_dict(), k=None)},
            node="__translator__",
        )
        result = await body(view)

        if isinstance(result, Failure):
            raise ValueError(f"翻译 harness 返回 Failure: {result.error}")

        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError as e:
                raise ValueError(f"翻译结果不是合法 JSON: {e}") from e

        if isinstance(result, dict):
            return result

        raise ValueError(f"翻译结果类型异常: {type(result).__name__}")
