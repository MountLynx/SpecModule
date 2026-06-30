"""翻译器：TasklistValidator + TemplateLoader + Translator。"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

from tickflow import Failure, parse as parse_graph
from tickflow.views import DictView, Resolved

from .config import HarnessConfig
from .harness import Harness
from .spec import Spec, Tasklist, TasklistTemplate, TaskDefinition
from .registry import HarnessRegistry

# Regex to find the first node name in a flow line.
_FIRST_NODE = re.compile(r"^[ \t]*\[?(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def prepare_flow(flow: str) -> str:
    """Ensure the flow text is valid tickflow DSL.

    The tasklist ``flow`` field uses a tickflow-like syntax but allows
    bare task names as the whole flow (meaning that task is both the
    start and the only node).  This function:
      * wraps a bare name as ``[name]``, and
      * wraps the first token in ``[...]`` when no start marker exists
        (only one start is added for the simplest case).
    """
    stripped = flow.strip()

    # Single bare name -> start node.
    if "--> " not in stripped and "-->[" not in stripped and stripped.startswith("[") is False:
        if _FIRST_NODE.fullmatch(stripped):
            return f"[{stripped}]"

    # Multi-node flow with no ``[start]`` marker at all.
    if "[" not in stripped:
        m = _FIRST_NODE.match(stripped)
        if m:
            first = m.group("name")
            idx = m.end("name")
            return f"[{first}]{stripped[idx:]}"

    return stripped


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

        # 尝试 tickflow parse 检测语法问题（与 graph_builder 使用相同的 prepare_flow 预处理）
        try:
            parse_graph(prepare_flow(flow))
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
            tasks_dict = await self._call_harness_translator(ts.harness, ts.prompt, spec, prompt_core=ts.prompt_core)
        else:
            raise ValueError(f"不支持的 translation type: {ts.type}")

        # 检测 LLM 返回的包装格式 {"Tasks": {...}, "Flow": "..."}
        flow = template.tasklist.flow
        if isinstance(tasks_dict, dict) and "Tasks" in tasks_dict:
            flow = tasks_dict.get("Flow", flow)
            tasks_dict = tasks_dict["Tasks"]

        # 构建 tasklist 并校验
        tasklist = Tasklist(tasks={
            key: TaskDefinition.from_dict(td) if isinstance(td, dict) else td
            for key, td in tasks_dict.items()
        }, flow=flow)

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

    async def _call_harness_translator(
        self,
        harness_name: str,
        prompt_extra: str,
        spec: Spec,
        prompt_core: str | None = None,
    ) -> dict:
        """调用 harness body（异步 LLM），parse 返回的 JSON 为 task dict。

        如果提供 *prompt_core*，则覆盖原 harness 配置的核心提示词模板。
        """
        if prompt_core is not None:
            existing = self.reg.harness_config(harness_name)
            if existing is not None:
                overridden = HarnessConfig(
                    prompt_core=prompt_core,
                    prompt_modes=dict(existing.prompt_modes),
                    output_format=existing.output_format,
                    notdo=list(existing.notdo),
                    model=existing.model,
                    temperature=existing.temperature,
                    think=existing.think,
                )
                h = Harness(overridden, self.reg._llm_client, self.reg._event_bus)
                body = h.build_body()
            else:
                raise ValueError(
                    f"Translation harness '{harness_name}' not found in registry"
                )
        else:
            body = self.reg.get_body(harness_name)

        view_data: dict[str, Resolved] = {"spec": Resolved(value=spec.to_dict(), k=None)}
        if prompt_extra:
            view_data["prompt_extra"] = Resolved(value=prompt_extra, k=None)
        view = DictView(view_data, node="__translator__")
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
