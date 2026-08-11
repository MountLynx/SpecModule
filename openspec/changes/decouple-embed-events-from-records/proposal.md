## Why

嵌入模式下（`SubModule.run(audit=False)`），宿主的可观测性被 `_build_registry` 的
`bus = self._event_bus if audit else EventBus.null()` 一刀切关掉：事件投递与
`keep_records` 被同一个 `audit` 布尔耦合。需要给用户反馈的宿主软件（IDE 插件、Web
后端）拿不到 `HarnessFailed`/`OutputValidated` 来报错，只能关掉一切或被迫开启全量审计。
评论指出正确用法是"选择性订阅"，但现有 API 无法表达"事件开、records 关"的中间态。

## What Changes

- **解耦事件投递与 keep_records**：`SubModule._build_registry` 改为
  `bus = self._event_bus or EventBus.null()` —— 宿主显式传入 `event_bus` 时始终投递事件，
  与 `audit`/`keep_records`/`persist` 无关；未传则仍为静默 `EventBus.null()`（嵌入零开销不变）。
- **`audit` 语义收窄为只管 records**：`audit` 继续映射 `keep_records=audit`，不再连带关闭事件投递。
- **对齐 Module 层既有行为**：`Module.__init__` 本就 `event_bus or EventBus.null()`（永远尊重传入
  bus）；本改动把 SubModule 拉回一致，非新特性。
- **测试契约反转**：`test_embedded_mode_no_events`（断言 `audit=False` 时宿主 bus 收不到事件）
  改为断言"事件可达但 `keep_records=False`"；补 `HarnessFailed` 可订阅断言。
- **文档同步**：`SubModule.run` docstring 与 roadmap 的"嵌入模式"表述，区分两个 embedding 含义
  （宿主整框架嵌入 vs submodule 黑盒节点嵌入）。

非破坏性：唯一行为变化是"传了 bus 却被静默丢弃"这一处旧契约；不传 bus 的嵌入路径逐字节不变。

## Capabilities

### New Capabilities
- `specmodule-embedding`: 嵌入可观测性契约——宿主传入 `event_bus` 时事件投递与
  `keep_records`/`persist` 解耦，支持"选择性订阅"而不拖上审计与落盘。

### Modified Capabilities
无（`specs/` 目录当前为空，无既有 capability）。