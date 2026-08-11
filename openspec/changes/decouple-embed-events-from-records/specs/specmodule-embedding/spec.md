## Purpose

嵌入（embedded）可观测性契约：宿主软件把 SpecModule 作为工具套件嵌入时，事件投递与
`keep_records`/`persist` 解耦——选择性订阅而不拖上审计与落盘。

## ADDED Requirements

### Requirement: 宿主传入 event_bus 时始终投递事件

宿主调用 `SubModule.run(audit=...)` 时，只要在构造传入 `event_bus`，事件即投递——与
`audit`/`keep_records`/`persist` 取值无关。`run` 的返回值仍只含最终输出，事件经 bus 现场送达。

#### Scenario: 嵌入模式仍可订阅失败事件
- **WHEN** 宿主以 `event_bus` 构造 submodule，并调用 `run(spec, audit=False)`（保持
  `keep_records=False`、`persist=False`）
- **THEN** 订阅的 `HarnessFailed`/`OutputValidated` 等事件可达，且 `keep_records` 仍为
  `False`（不产生审计轨迹、不落盘）

#### Scenario: 未传 bus 的嵌入零开销不变
- **WHEN** 宿主不传 `event_bus`，调用 `run(spec, audit=False)`
- **THEN** 内部使用 `EventBus.null()`（emit 无操作），事件无消费者、无额外开销

### Requirement: audit 语义收窄为只管审计记录

`SubModule.run` 的 `audit` 参数只控制 `keep_records`（RunState 审计轨迹），不再连带关闭
或开启事件投递。名称保留以维持向后兼容（roadmap 的 API 稳定化目标）。

#### Scenario: audit=True 且传 bus
- **WHEN** 宿主传 `event_bus` 并 `run(spec, audit=True)`
- **THEN** 事件投递并开启 `keep_records`（全量审计轨迹），行为与改动前一致

#### Scenario: 直接 Module 嵌入行为对齐
- **WHEN** 宿主直接使用 `Module`（非 SubModule）并传 `event_bus`、`keep_records=False`
- **THEN** bus 事件可达且 records 关闭——与改动后 SubModule 行为一致（两入口契约统一）