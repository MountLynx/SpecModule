# module_harness/graph_builder.py
"""Tasklist -> tickflow Graph translator.

Translates a :class:`Tasklist` into a :class:`Graph` with namespace-isolated
body registrations.  Each Task's body is registered as ``{module_id}:{key}``
so modules with overlapping task keys do not collide.
"""

from __future__ import annotations

from typing import Any

from tickflow import Graph, parse as parse_graph
from tickflow.ir import InputPolicy

from .config import HarnessConfig
from .outputfmt import OutputFormat
from .registry import HarnessRegistry
from .spec import TaskDefinition, Tasklist
from .translator import prepare_flow


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

    def build(self, tasklist: Tasklist) -> tuple[Graph, HarnessRegistry]:
        """Iterate tasks, register bodies, parse flow, attach body names.

        Returns (graph, registry) where *registry* is ``self.reg`` (the same
        object that was passed to the constructor, now populated with the
        isolated body entries).
        """
        # 1.  Register every task's body under an isolated name.
        for key, task in tasklist.tasks.items():
            self._register_body(key, task)

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
        #     Each (field_name -> producer_name) entry in task.inputs becomes
        #     node.inputs[field_name] = InputPolicy.latest(), so the body can
        #     access ``view.field_name.value`` and receive the producer's value.
        for key, task in tasklist.tasks.items():
            if task.inputs:
                for field_name in task.inputs:
                    graph.nodes[key].inputs[field_name] = InputPolicy.latest()

        return graph, self.reg

    # ------------------------------------------------------------------
    # Body registration
    # ------------------------------------------------------------------

    def _isolated(self, key: str) -> str:
        """Return the namespace-isolated body name for *key*."""
        return f"{self.module_id}:{key}"

    def _register_body(self, key: str, task: TaskDefinition) -> None:
        """Register one task's body in *self.reg* under an isolated name.

        Delegates to the appropriate helper based on ``task.type``.
        """
        if task.type == "harness":
            self._register_harness(key, task)
        elif task.type == "script":
            self._register_script(key, task)
        else:
            raise ValueError(f"Task '{key}': unknown type {task.type!r}")

    def _register_harness(self, key: str, task: TaskDefinition) -> None:
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
        )

        self.reg.harness(
            self._isolated(key),
            cfg,
            promptmode=task.promptmode,
            prompt_extra=task.prompt,
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
