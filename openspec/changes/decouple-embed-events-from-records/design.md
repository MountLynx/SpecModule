## Context

`SubModule.run` 的 `_build_registry` 用 `bus = self._event_bus if audit else EventBus.null()`
把事件投递与 `keep_records` 焊死在同一个 `audit` 布尔上。而事件是 module_harness 在
harness/script/command body 里**现场 emit** 的（`harness.py` 的 `bus.emit(...)`），纯内存、
近零成本，不读 `RunState._records`——所以"事件开、records 关"在物理上始终成立，只是 API
无法表达。tickflow 本身已正确解耦三轴（`keep_records` / `NullBackend` / hooks 均独立）；
`Module.__init__` 也早已 `event_bus or EventBus.null()`（永远尊重传入 bus）。耦合只存在于
`SubModule._build_registry` 这一处封装回归。

## Goals / Non-Goals

**Goals:**
- 宿主结构 `event_bus` 时，事件投递与 `audit`/`keep_records`/`persist` 解耦。
- `audit` 语义收窄为只管 `keep_records`，名称保留（向后兼容）。
- SubModule 行为与 Module 层既有行为对齐。
- 不传 bus 的嵌入零开销路径逐字节不变。

**Non-Goals:**
- 不加新参数、不改 `run` 签名（无 break）。
- 不改 tickflow（其 `keep_records`/`NullBackend`/hooks 已正确解耦）。
- 不引入 tickflow 事件总线（EventBus 属 module_harness，README 明确划出范围）。
- 不重命名 `audit` 参数。

## Decisions

### D1: 唯一源码改动 = `_build_registry` 一行
`bus = self._event_bus if audit else EventBus.null()` → `bus = self._event_bus or EventBus.null()`。
`audit` 继续喂 `keep_records=audit`。语义从"一条布尔关两样"变"传 bus 就投递、audit 只管 records"。
理由：改动面最窄，且与 Module 层既有契约一致；不引入新概念。

### D2: 不新增参数、不重命名 audit
roadmap 有 API 稳定化目标，`run(audit=...)` 已被测试与 example 引用。解耦不改签名，只改
`audit` 的语义宽窄（从"关全部"收窄为"只关 records"）。避免破坏性 API 变更。

### D3: 测试契约反转
`test_embedded_mode_no_events`（断言 `audit=False` 时宿主 bus 收不到事件，`assert got == []`）
固化了被外部评论挑战的旧契约，必须改写为"事件可达但 `keep_records=False`"。保留
`test_audit_mode_emits_events`（`audit=True` 收事件）验证另一路径不回归。补一条
`HarnessFailed` 订阅断言（mock LLM 抛 `LLMError`），覆盖"翻译为什么失败"场景。

### D4: 文档区分两个 embedding 含义
roadmap"嵌入模式（不进审计/快照/回滚）"针对的是 **submodule 作为黑盒节点**（父图驱动、
终点输出），与宿主**整框架嵌入**是不同场景。改动服务后者；前者保留原语义（子节点内部
无审查意义，不为其开事件）。

## Risks / Trade-offs

- **行为变化面**：唯一变化是"传了 bus 却被静默丢弃"这一处旧契约。语义上更合理（宿主显式
  传 bus = 主动选择观察），且纯增量（无新增记录/落盘）。风险低。
- **`audit` 命名略失**：现在只映射 records，名字仍贴切（审计轨迹），但未来若有人理解
  "audit=事件开关"会困惑。以 docstring 显式说明收窄语义对冲。
- **测试依赖**：`test_submodule.py` 的嵌入事件契约测试需同步改，否则 CI 红。属预期变更。
- **tickflow 不动**：无通用价值改动，不触发 AGENTS.md"同步回上游"路径。