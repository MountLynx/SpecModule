# module_harness/tests/test_events.py
from module_harness.events import (
    EventBus,
    HarnessEvent, PromptRendered, LlmCallStarted, LlmToken,
    LlmCallCompleted, OutputValidated, HarnessFailed,
    ScriptEvent, ScriptStarted, ScriptCompleted, ScriptFailed,
)


class TestEventBus:
    def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []

        bus.subscribe(PromptRendered, lambda e: received.append(e))

        evt = PromptRendered(timestamp=1.0, node="A", tick=0, rendered="hello")
        bus.emit(evt)
        assert len(received) == 1
        assert received[0].rendered == "hello"

    def test_multiple_subscribers_same_type(self):
        bus = EventBus()
        results = []

        bus.subscribe(LlmToken, lambda e: results.append(("a", e.chunk)))
        bus.subscribe(LlmToken, lambda e: results.append(("b", e.chunk)))

        bus.emit(LlmToken(timestamp=1.0, node="X", tick=0, chunk="hi"))
        assert len(results) == 2
        assert ("a", "hi") in results
        assert ("b", "hi") in results

    def test_callback_exception_swallowed(self):
        bus = EventBus()
        received = []

        def bad_callback(e):
            raise RuntimeError("boom")

        bus.subscribe(PromptRendered, bad_callback)
        bus.subscribe(PromptRendered, lambda e: received.append(e))

        # Must not raise
        bus.emit(PromptRendered(timestamp=1.0, node="A", tick=0, rendered="ok"))
        assert len(received) == 1

    def test_on_decorator(self):
        bus = EventBus()
        received = []

        @bus.on(LlmCallStarted)
        def handle(e):
            received.append(e.model)

        bus.emit(LlmCallStarted(timestamp=1.0, node="B", tick=0, model="claude", prompt_chars=100))
        assert received == ["claude"]

    def test_null_bus_emit_does_not_raise(self):
        bus = EventBus.null()
        # Must not raise — no subscribers, emit is no-op
        bus.emit(PromptRendered(timestamp=1.0, node="A", tick=0, rendered="x"))

    def test_events_carry_all_fields(self):
        e = OutputValidated(
            timestamp=1.0, node="C", tick=1,
            passed=True, extracted=False, error=None,
        )
        assert e.passed is True
        assert e.extracted is False
        assert e.error is None


class TestEventTypes:
    def test_harness_event_base_fields(self):
        e = PromptRendered(timestamp=1.0, node="A", tick=0, rendered="p")
        assert e.timestamp == 1.0
        assert e.node == "A"
        assert e.tick == 0

    def test_script_event_base_fields(self):
        e = ScriptStarted(timestamp=2.0, node="B", tick=1)
        assert e.timestamp == 2.0
        assert e.node == "B"
        assert e.tick == 1

    def test_harness_failed_fields(self):
        e = HarnessFailed(timestamp=1.0, node="X", tick=0, reason="timeout", failure_type="infrastructure")
        assert e.reason == "timeout"
        assert e.failure_type == "infrastructure"

    def test_llm_token_fields(self):
        e = LlmToken(timestamp=1.0, node="Y", tick=0, chunk="Hello")
        assert e.chunk == "Hello"

    def test_script_completed_output_type(self):
        e = ScriptCompleted(timestamp=1.0, node="Z", tick=2, output_type="dict")
        assert e.output_type == "dict"
