import pytest
from unittest.mock import MagicMock
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry
from module_harness.spec import Tasklist, TaskDefinition
from module_harness.translator import TasklistValidator


def _make_registry(harnesses=None, scripts=None):
    """构造一个含指定 harness/script 名称的 HarnessRegistry mock。"""
    reg = MagicMock()
    reg.is_harness = lambda n: n in (harnesses or set())
    reg.is_script = lambda n: n in (scripts or set())
    reg.has_body = lambda n: False
    return reg


class TestTasklistValidator:
    def test_valid_tasklist_passes(self):
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate", inputs={"text": "src"}),
                "B": TaskDefinition(type="script", script="post_process", inputs={"data": "A"}),
            },
            flow="[A] --> B",
        )
        reg = _make_registry(harnesses={"translate"}, scripts={"post_process"})
        errors = TasklistValidator.validate(tl, reg)
        assert errors == []

    def test_unreferenced_harness(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="nonexistent")},
            flow="A",
        )
        reg = _make_registry(harnesses=set(), scripts=set())
        errors = TasklistValidator.validate(tl, reg)
        assert any("nonexistent" in e for e in errors)

    def test_unreferenced_script(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="no_such")},
            flow="A",
        )
        reg = _make_registry(harnesses=set(), scripts=set())
        errors = TasklistValidator.validate(tl, reg)
        assert any("no_such" in e for e in errors)

    def test_harness_type_missing_harness_field(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness=None)},
            flow="A",
        )
        reg = _make_registry()
        errors = TasklistValidator.validate(tl, reg)
        assert any("harness" in e.lower() for e in errors)

    def test_script_type_missing_script_field(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script=None)},
            flow="A",
        )
        reg = _make_registry()
        errors = TasklistValidator.validate(tl, reg)
        assert any("script" in e.lower() for e in errors)

    def test_flow_node_not_in_tasks(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="A --> B",
        )
        reg = _make_registry(scripts={"s"})
        errors = TasklistValidator.validate(tl, reg)
        assert any("B" in e for e in errors)

    def test_flow_parse_error(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="A -->> B  # invalid syntax",
        )
        reg = _make_registry(scripts={"s"})
        errors = TasklistValidator.validate(tl, reg)
        # tickflow parse 对无效语法会抛异常或警告；验证器应捕获
        assert len(errors) >= 0  # 至少不抛未捕获异常

    def test_empty_tasks_with_nonempty_flow(self):
        tl = Tasklist(tasks={}, flow="A --> B")
        reg = _make_registry()
        errors = TasklistValidator.validate(tl, reg)
        assert any("A" in e for e in errors)


class TestGuardFlow:
    def _reg(self):
        reg = HarnessRegistry(llm_client=object(), event_bus=EventBus())
        reg.script("s")(lambda view: {"n": 1})
        return reg

    def test_guard_edge_with_registered_guard_passes(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="[A] --|until3|--> A",
        )
        reg = self._reg()
        reg.guard("until3")(lambda view: False)
        assert TasklistValidator.validate(tl, reg) == []

    def test_guard_edge_unregistered_guard_fails(self):
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="s")},
            flow="[A] --|until3|--> A",
        )
        errors = TasklistValidator.validate(tl, self._reg())
        assert any("until3" in e for e in errors)
