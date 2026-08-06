# module_harness/graph_builder.py
"""Tasklist -> tickflow Graph translator.

Translates a :class:`Tasklist` into a :class:`Graph` with namespace-isolated
body registrations.  Each Task's body is registered as ``{module_id}:{key}``
so modules with overlapping task keys do not collide.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from tickflow import Graph, parse as parse_graph
from tickflow.ir import InputPolicy

from .config import HarnessConfig
from .outputfmt import OutputFormat
from .registry import HarnessRegistry
from .spec import TaskDefinition, Tasklist
from .translator import prepare_flow


_CONSTANT_TOKENS = frozenset({"{spec}", "{tasklist}", "{node}"})


def _is_constant_ref(producer: Any) -> bool:
    """是否为注册时解析的常量引用（{spec}/{tasklist}/{node} 或 {spec.xxx}）。"""
    return (
        isinstance(producer, str)
        and (producer in _CONSTANT_TOKENS or producer.startswith("{spec."))
    )


class TasklistTranslator:
    """Translates a :class:`Tasklist` into a parsed :class:`Graph` paired with
    a :class:`HarnessRegistry` that contains all required bodies.

    Usage::

        builder = TasklistTranslator(registry, module_id="my_mod")
        graph, out_reg = builder.build(tasklist)
    """

    def __init__(self, registry: HarnessRegistry, module_id: str) -> None:
        self.reg = registry
        self.module_id = module_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, tasklist: Tasklist, spec: Any | None = None) -> tuple[Graph, HarnessRegistry]:
        """Iterate tasks, register bodies, parse flow, attach body names.

        ``spec``：可选，用于解析 task 中 ``{spec.xxx}`` 字段引用。
        Returns (graph, registry) where *registry* is ``self.reg`` (the same
        object that was passed to the constructor, now populated with the
        isolated body entries).
        """
        spec_dict = spec.to_dict() if spec is not None else {}
        tasklist_dict = {
            "Tasks": {
                key: dataclasses.asdict(task) for key, task in tasklist.tasks.items()
            },
            "Flow": tasklist.flow,
        }

        # 1.  Register every task's body under an isolated name.
        for key, task in tasklist.tasks.items():
            self._register_body(key, task, spec_dict, tasklist_dict)

        # 2.  Prepare the flow text for tickflow's parser (no body/input
        #     declarations -- those are attached programmatically below
        #     because tickflow's DSL does not support ``:`` in body names).
        flow_text = prepare_flow(tasklist.flow)

        # 3.  Parse into a Graph.  The parser validates that guard names
        #     and default placeholder bodies exist in the registry.
        graph = parse_graph(flow_text, registry=self.reg)

        # 4.  Assign the real (colon-scoped) body name to each graph node.
        for key, task in tasklist.tasks.items():
            isolated_name = self._isolated(key)
            graph.nodes[key].body = isolated_name

        # 5.  Wire task inputs to graph node inputs.
        #     ``task.inputs = {field_name: producer}`` — 同时注册 field 名与
        #     producer 名两个 key：body 用 ``view.<producer>.value``（有值），
        #     ``view.<field_name>.value`` 为 Missing（不崩溃，脚本旧写法兼容）。
        #     harness 的 prompt 占位符 ``{field}`` 经 _register_harness 传入的
        #     input_aliases 在运行时解析 producer 输出值。
        #     常量引用（{spec}/{tasklist}/{node}、{spec.xxx}）已由
        #     _register_harness 解析为 spec_inputs，此处跳过。
        for key, task in tasklist.tasks.items():
            if task.inputs:
                for field_name, producer in task.inputs.items():
                    if _is_constant_ref(producer):
                        continue
                    graph.nodes[key].inputs[field_name] = InputPolicy.latest()
                    if producer != field_name:
                        graph.nodes[key].inputs[producer] = InputPolicy.latest()

        return graph, self.reg

    # ------------------------------------------------------------------
    # Body registration
    # ------------------------------------------------------------------

    def _isolated(self, key: str) -> str:
        """Return the namespace-isolated body name for *key*."""
        return f"{self.module_id}:{key}"

    def _register_body(self, key: str, task: TaskDefinition,
                       spec_dict: dict[str, Any],
                       tasklist_dict: dict[str, Any]) -> None:
        """Register one task's body in *self.reg* under an isolated name.

        Delegates to the appropriate helper based on ``task.type``.
        """
        if task.type == "harness":
            self._register_harness(key, task, spec_dict, tasklist_dict)
        elif task.type == "script":
            self._register_script(key, task)
        elif task.type == "command":
            self._register_command(key, task)
        else:
            raise ValueError(f"Task '{key}': unknown type {task.type!r}")

    @staticmethod
    def _resolve_spec_ref(value: str, spec_dict: dict[str, Any]) -> Any:
        """解析 "{spec.xxx}" 引用。非引用原样返回。"""
        if isinstance(value, str) and value.startswith("{spec.") and value.endswith("}"):
            return spec_dict.get(value[len("{spec."):-1])
        return value

    @staticmethod
    def _resolve_constant(token: str, node_key: str,
                          spec_dict: dict[str, Any],
                          tasklist_dict: dict[str, Any]) -> Any:
        """解析常量 token：{spec} → spec JSON，{tasklist} → tasklist JSON，
        {node} → 当前节点 key。未知 token 抛 ValueError。"""
        try:
            if token == "{spec}":
                return json.dumps(spec_dict, ensure_ascii=False)
            if token == "{tasklist}":
                return json.dumps(tasklist_dict, ensure_ascii=False)
        except TypeError as e:
            raise ValueError(
                f"Task '{node_key}': token {token} 含不可 JSON 序列化的值: {e}"
            ) from e
        if token == "{node}":
            return node_key
        raise ValueError(f"未知常量 token: {token}")

    def _register_harness(self, key: str, task: TaskDefinition,
                          spec_dict: dict[str, Any],
                          tasklist_dict: dict[str, Any]) -> None:
        """Copy an existing harness config, apply task-level overrides, and
        register under the isolated name."""
        assert task.harness is not None  # validated by spec
        existing = self.reg.harness_config(task.harness)
        if existing is None:
            raise ValueError(
                f"Task '{key}': harness '{task.harness}' not found.  "
                f"Make sure it was registered via reg.harness()."
            )

        # Build a new config with task-level overrides.
        output_format: OutputFormat | None
        if task.outputformat is not None:
            output_format = OutputFormat(**task.outputformat)
        else:
            output_format = existing.output_format

        api_params = dict(existing.api_params)
        if task.api_params:
            api_params.update(task.api_params)

        cfg = HarnessConfig(
            prompt_core=existing.prompt_core,
            prompt_modes=dict(existing.prompt_modes),
            output_format=output_format,
            notdo=list(task.notdo) if task.notdo is not None else list(existing.notdo),
            model=task.model if task.model is not None else existing.model,
            temperature=(
                task.temperature
                if task.temperature is not None
                else existing.temperature
            ),
            think=task.think if task.think is not None else existing.think,
            api_params=api_params,
        )

        # 解析常量引用：promptmode 的 "{spec.xxx}" 与 inputs 的常量 token
        # （{spec}/{tasklist}/{node} 与 {spec.xxx}）均在注册时解析为字面值
        promptmode = task.promptmode
        if promptmode is not None:
            promptmode = self._resolve_spec_ref(promptmode, spec_dict)

        spec_inputs: dict[str, Any] = {}
        if task.inputs:
            for field_name, producer in task.inputs.items():
                if isinstance(producer, str) and producer in _CONSTANT_TOKENS:
                    spec_inputs[field_name] = self._resolve_constant(
                        producer, key, spec_dict, tasklist_dict
                    )
                elif isinstance(producer, str) and producer.startswith("{spec."):
                    resolved = self._resolve_spec_ref(producer, spec_dict)
                    spec_inputs[field_name] = resolved

        # 跨节点输入别名：非常量引用的 inputs 把 field 名映射到 producer 节点，
        # harness body 运行时据此把 producer 输出渲染进 prompt 的 {field} 占位符。
        input_aliases: dict[str, str] = {}
        if task.inputs:
            for field_name, producer in task.inputs.items():
                if _is_constant_ref(producer):
                    continue
                input_aliases[field_name] = producer

        self.reg.harness(
            self._isolated(key),
            cfg,
            promptmode=promptmode,
            prompt_extra=task.prompt,
            spec_inputs=spec_inputs,
            input_aliases=input_aliases,
        )

    def _register_script(self, key: str, task: TaskDefinition) -> None:
        """Copy an existing script body and register under the isolated name."""
        assert task.script is not None  # validated by spec
        if not self.reg.has_body(task.script):
            raise ValueError(
                f"Task '{key}': script '{task.script}' not found.  "
                f"Make sure it was registered via @reg.script()."
            )
        orig_body = self.reg.get_body(task.script)
        self.reg.body(self._isolated(key), orig_body)

    def _register_command(self, key: str, task: TaskDefinition) -> None:
        """Copy an existing command config, apply task overrides, register under
        the isolated name."""
        assert task.command is not None  # validated by spec
        existing = self.reg.command_config(task.command)
        if existing is None:
            raise ValueError(
                f"Task '{key}': command '{task.command}' not found.  "
                f"Make sure it was registered via reg.command()."
            )

        self.reg.command(
            self._isolated(key),
            existing,
            timeout=task.timeout,
            cwd=task.cwd,
        )
