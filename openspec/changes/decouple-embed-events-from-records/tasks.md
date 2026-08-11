## 1. 解耦改动

- [x] 1.1 `submodule.py:_build_registry`：`bus = self._event_bus if audit else EventBus.null()`
      改为 `bus = self._event_bus or EventBus.null()`；`audit` 继续映射 `keep_records=audit`。
- [x] 1.2 更新 `SubModule.run` docstring：`audit` 语义收窄为只管 `keep_records`；宿主传入
      `event_bus` 时事件始终投递（与 records/persist 解耦）。

## 2. 测试契约

- [x] 2.1 改写 `test_submodule.py:test_embedded_mode_no_events` → 断言 `audit=False` + 传
      bus 时事件可达，且 `keep_records` 为 False（新契约：事件开、records 关）。
- [x] 2.2 保留 `test_audit_mode_emits_events`（`audit=True` 收事件）验证不回归。
- [x] 2.3 补 `HarnessFailed` 订阅断言：mock LLM 抛 `LLMError`，嵌入模式可收到失败事件。

## 3. 验证与文档

- [x] 3.1 跑 `python -m pytest module_harness/tests/test_submodule.py -q` 全绿。
- [x] 3.2 跑 `python -m pytest module_harness/tests/ -q` 全绿（无跨测试回归）。
- [x] 3.3 roadmap 更新"嵌入模式"表述，区分宿主整框架嵌入 vs submodule 黑盒节点嵌入。