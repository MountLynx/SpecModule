# 冗余清理设计（重复存储 + 重复计算一次性消除）

> 日期：2026-08-06 | 状态：已确认，已实现
> 关联：[2026-08-07-lightweight-snapshot-design.md](./2026-08-07-lightweight-snapshot-design.md)
> 本设计把轻量快照文档的判定标准（"重复存储、重复计算是否还有性能/功能上的必要"）应用到全代码库，一次性消除同类冗余。tickflow 直接在本仓库修改（经用户确认），后续随既有 sync 机制同步上游 Graph 仓库。

## 概述

**判定标准**：一项数据/计算，若既无**性能上的必要**（去掉后热路径或关键路径有可感知的开销），也无**功能上的必要**（去掉后丢失能力或破坏契约），即判定为冗余，删除或统一。

排查范围：`tickflow/`（state/runner/engine/persistence/ir/checker）+ `module_harness/`（module/checkpoint/consistency/graph_builder/status/…）+ `llm/`。

**总览**：4 项重复存储（S1–S4）、5 项重复计算（C1–C5）判定为冗余并修复；若干项经评估**保留或跳过**（见"保留/跳过清单"）。

---

## 一、重复存储 — 修复

| # | 冗余 | 位置 | 判定 |
|---|------|------|------|
| **S1** | 每 tick 快照内嵌完整审计 records（每份 O(t)，总量 O(n²)）；同一 records 已逐 tick 写入 firings 表 | `state.py` `to_snapshot_data`（records 分支）+ `runner.py` `_persist_tick` | 无性能必要（restore 从 firings 重建）；无功能必要（持久路径 audit() 也从 backend 查询）→ **剥离** |
| **S2** | `auto_checkpoints` 表 = `snapshots` 表**每 tick 双写同一份快照**（同一 run.sqlite 两个表，`on_tick_end` hook 与 `_persist_tick` 各存一次） | `module.py` `_register_auto_checkpoint` hook + `checkpoint.py` `AutoCheckpointStore` | 无必要 → **删除表 + hook** |
| **S3** | 持久路径快照的 `run_state.edges/fire_counts/state` 在**同 session restore** 下是死数据（`truncate_after` 从 firings 重建）；但快照**自包含契约**（Graph 库 branching / restore-into-new-runner，`test_persistence.py` 钉住）要求快照可 restore 到无 history 的 backend——此时重建无据，必须自带窗口/状态 | `state.py` `to_snapshot_data` + `runner.py` `_persist_tick` | **功能必要（自包含契约）→ 保留**。S3 曾误判为死数据并实现"最小快照"，Graph 自有测试逮到跨 session restore 回归后修正：快照 = **轻量**（剥离 records，O(节点+边)） |
| **S4** | tasklist→dict 转换 3 处独立实现（内容逐行相同） | `checkpoint.py:40-45` `tasklist_to_dict`、`consistency.py:82-88` 内联、`graph_builder.py:63-68` 内联 | 无必要 → **统一为 `Tasklist.to_dict()`**（spec.py 单一事实源） |

## 二、重复计算 — 修复

| # | 冗余 | 位置 | 判定 |
|---|------|------|------|
| **C1** | `fireable` 每 tick 计算两遍：`Runner.tick()` 为 tick_start hooks 计算，`engine.tick()` 对同一 marking 内部重算同一值（async 同） | `runner.py:521` / `async_runner.py:197` vs `engine.py:147` / `async_runner.py:74` | 无必要 → **engine 接受预计算 fireable**（缺省 None 内部计算，直接调用方零影响） |
| **C2** | `resume()` 调 `remap_graph(runner.graph)`——图是同一个对象，restore 已设好 marking，slot 移植/keep_nodes/警告全部 no-op | `module.py:441` | 无必要 → **删除调用** |
| **C3** | checker 对每个 AND-join 候选 × 每个 splitter 重算分支 BFS：`_branches_of(b)` 只依赖 splitter，却在内层循环为每个 m 重算 → O(K·S·(V+E)) | `checker.py:150-162` | 无必要 → **按 splitter 预计算一次**，内层查表 → O(S·(V+E)) |
| **C4** | Graph 邻接查询每次 O(E) 全边扫描；每 tick `_join_satisfied`（全节点）+ Phase A/B（每 firing）重复扫描同一邻接数据 → O(V·E + F·E)/tick | `ir.py:100-108` `producers/out_edges/consumers` + `engine.py`/`async_runner.py` 调用点 | 热路径重复计算 → **惰性索引**（`len(edges)` 版本化，覆盖 parser 边追加；`copy()` 不复制索引惰性重建） |
| **C5** | `prompt.py` 每次 render 重新 `re.compile` 常量正则 | `prompt.py:66` | 无必要（微秒级但零成本可消除）→ **模块级编译一次** |

## 三、保留 / 跳过清单（判定：有性能或功能必要）

| 项 | 保留理由 |
|----|---------|
| firings 表全量审计（逐 tick 落盘） | 审计功能必要（audit/firings_of/A[k] 冷查询的数据源） |
| `to_json`/`from_json` 显式完整导出（重嵌 records） | roundtrip 保真契约，功能必要（显式导出，非每 tick 路径） |
| 快照中的 **marking** | O(1) restore 的性能必要——唯一不从 firings 重建的"程序计数器"（由 firings 全量重放才能推导，代价 O(t·E)） |
| 快照中的 **fireable** | 跨进程 `query_run_status` 无图可推，功能必要 |
| 快照中的 **fired**（新增） | 历史审阅功能必要；且必须给消费方（`list_checkpoints` 显示），避免再造死数据 |
| module_inputs 存档 | resume 兼容性警告 1 功能必要 |
| status.json 阶段文件 | 跨进程阶段查询功能必要 |
| RunState 内存 `_edges` 窗口 / `_state` / `_fire_counts` | `keep_records=False` 下功能必要 + O(1) resolve 性能必要 |
| 内存 `_records`（keep_records=True 且无 backend 时） | 审计功能必要（与 backend 路径互斥，非重复） |
| NodeState `edges_fired`（审计记录内） | 审计功能必要 |

| 项 | 跳过理由 |
|----|---------|
| flow 文本双解析（`TasklistValidator` + `TasklistTranslator`） | 冷路径（每 run 一次），代价微秒级；合并需重构 validator 返回结构，收益不抵复杂度 |
| outputfmt 失败路径多轮 `json.loads`（含 O(n²) 截断） | 仅 LLM 输出非法时触发，罕见路径 |
| CLI 双死锁检查（`_resolve_deadlocks` + `Runner.__init__`） | 冷路径，每命令一次 |
| llm/client 响应解析逻辑多客户端内联重写 | 无运行时开销，属代码重复而非数据/计算冗余；且 chat/complete 已行为漂移，属独立的功能差距问题，不在本设计范围 |
| HarnessRegistry 双结构（`_bodies` + 类型表） | 类型分类与 body 是不同事实，合并有漂移风险，无性能收益 |
| loader `provides`/`all_names` O(N²) 去重 | 冷路径、N 小 |
| submodule 每次 run 重建内置 harness 实例 | 冷路径、每 run 3 个对象 |

---

## 四、修复设计

### D1. 轻量快照（tickflow: `state.py` / `runner.py` / `async_runner.py`）

- `RunState.to_snapshot_data(include_records: bool = True)`：`include_records=False` 剥离 records（S1）
- `Runner.snapshot(include_records=True)` 透传。`_persist_tick` 改为 `_persist_tick(fired: list[str])`，持久路径存**轻量快照**（含 edges/fire_counts/state 保证自包含，S3 修正后无 minimal 参数）：

  ```json
  { "tick": N, "marking": {...},
    "run_state": {"edges": {...}, "fire_counts": {...}, "state": {...},
                  "keep_records": true, /* 无 records */},
    "status": "...", "cancel_reason": ..., "fireable": [...], "fired": [...] }
  ```

  大小 O(节点数 + 边数)，不随运行 tick 增长。
- `Runner.tick()` / `AsyncRunner.tick()` 调用 `_persist_tick([f.node for f in firings])`；空 tick（无 fireable）为 `[]`，快照照存（轨迹连续性，与轻量快照文档一致）。
- **restore 零改动**：同 session 时 `truncate_after` 持久分支从 firings 重建；跨 session/新 runner 时快照自带 edges/state 兜底（自包含契约）。
- **内存路径（NullBackend）不变**：`_persist_tick` 不存快照；`checkpoint()`/`to_json` 仍走全量 `snapshot()`（无法从空后端重建，功能必要）。
- 旧快照（含 records、无 fired）restore 兼容：`fired` 用 `.get("fired", [])` 容错。

### D2. 消费方迁移（tickflow: `persistence.py` + module_harness: `module.py` / `status.py`）

- **`resume(rollback_to: int | str)`**：
  - tick 号（`357` 或 `"357"`）→ `SqliteBackend.load_snapshot(module_id, tick)` → restore
  - `"manual:xxx"` → checkpoints 表 `load_checkpoint`（保留）
  - 其他 → `KeyError`，附可用 tick 范围 + manual label 列表
  - （auto 表删除后 `auto:tick:N` label 消亡，tick 回退是唯一出路——与轻量快照文档语义一致）
- **resume 的 `executed_nodes`**：快照 `run_state.edges` keys → `list_firings(module_id)` 去重节点（tick ≤ 快照 tick）。冷路径一次，O(总数)。
- **`list_checkpoints()`**：返回 `list[tuple[int, str | list[str], str]]`，按 tick 升序：
  - snapshots 表条目：`(tick, fired 节点列表, "tick")`（逐 tick `load_snapshot` 读 `fired`——历史审阅雏形，`fired` 的唯一消费方；条目量大时可后续加轻量查询，本设计不做）
  - manual checkpoints 条目：`(tick, label, "manual")`（沿用既有 `list_checkpoints`）
  - 环形 20 概念消失。
- **`query_run_status`**：`status/tick/fireable/fired` 从轻量快照读；`outputs/node_states` 改从 firings 读——新增 `SqliteBackend.latest_firings(session_id) -> list[dict]`：每节点最后一 firing（`(tick, node)` 去重 keep-first，语义与 `firings_of` 一致），O(会话内 firings)（`idx_firings_node` 索引限定 session 范围）。

### D3. auto_checkpoints 退役（module_harness: `checkpoint.py` / `module.py` / `__init__.py`）

- `AutoCheckpointStore` → `ModuleInputStore`（只保留 `module_inputs` 表与 save/load_module_inputs；删除 save/load/list/_prune/环形逻辑）
- `_register_auto_checkpoint()` → `_archive_module_inputs()`：run() 在构建 runner 后归档一次；resume() 在**兼容性校验通过、restore 之后**归档一次（覆盖为新输入——现状语义保持：resume 的警告 1 必须先读旧存档再覆盖）
- `Module.close()` 同步更新（关 ModuleInputStore 连接）；`__init__.py` 导出 `ModuleInputStore`

### D4. fireable 透传（tickflow: `engine.py` / `async_runner.py`）

- `tick(..., fireable: list[str] | None = None)`：提供则跳过内部重算，缺省 None 行为不变（直接调用 tick() 的测试/外部调用零影响）
- `Runner.tick()` / `AsyncRunner.tick()` 传入已为 hooks 计算的 fireable（C1）

### D5. 删 C2 no-op（module_harness: `module.py`）

- 删除 `runner.remap_graph(runner.graph)` 调用与 docstring 中"remap_graph 移植 marking"的描述

### D6. tasklist 序列化统一（module_harness: `spec.py` / `checkpoint.py` / `consistency.py` / `graph_builder.py` / `module.py`）

- `Tasklist.to_dict()` 加在 `spec.py`（`{"Tasks": {asdict...}, "Flow": ...}`）
- `checkpoint.tasklist_to_dict` 保留为薄封装（导出兼容，`module.py` 继续引用）；`consistency.py`、`graph_builder.py` 改用 `Tasklist.to_dict()`

### D7. checker 分支缓存（tickflow: `checker.py`）

- `check()` 外层先 `branches_by_splitter = {b: _branches_of(graph, b) for b in graph.nodes if graph.is_xor_splitter(b)}`（顺带缓存 splitter 判定，避免每个 (m, b) 组合 O(E) 重扫 `is_xor_splitter`）
- 内层循环查表；`DeadlockSuggestion.branches` 内容不变（结果等价）

### D8. Graph 惰性邻接索引（tickflow: `ir.py`）

- `Graph` 增加 `_adj: dict | None = None`（@dataclass field，repr=False）+ 版本 `_adj_edges_len: int = -1`
- `producers/out_edges/consumers/is_xor_splitter` 首调用或 `len(self.edges) != self._adj_edges_len` 时重建索引（`src→[Edge]`、`dst→[Edge]`）；覆盖 parser 边追加场景（parser 在 parse 中途调 `producers`）
- `copy()` 不复制索引（惰性重建）；`to_dict/to_mermaid` 行为不变
- 结果与全扫**严格等价**（同一 edges 列表的派生），仅省重复扫描

### C5 正则提升（module_harness: `prompt.py`）

- `_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")` 模块级，`_substitute` 引用

---

## 五、测试计划

### tickflow 侧（本仓库无独立 tickflow tests，经 module_harness 测试覆盖）

- 轻量快照落盘：`_persist_tick` 后直查 DB，snapshots.data 无 records（含 edges/fire_counts/state 与 `fired` 正确值）
- **restore 等价性**：轻量快照 restore（同 session：从 firings 重建；跨 session/新 runner：快照自带窗口/状态兜底）→ 续跑正确；与全量快照 restore 结果一致
- 旧快照（含 edges/state/records）restore 兼容
- fireable 透传：engine.tick(fireable=...) 与不传结果一致
- checker 缓存：`check()` 输出与改动前逐项相等（golden 断言）
- ir 索引：producers/out_edges/consumers 与全扫等价（含 parser 中途追加场景）

### module_harness 侧

- `resume(tick)` / `resume("357")` / `resume("manual:xxx")`；`resume("abc")`/`resume(999)` → KeyError 附可用信息
- `list_checkpoints()` 显示 `(tick, fired 列表)` + manual 合并
- `query_run_status` 从 firings 读 outputs/node_states（含 restore 重放去重场景）
- run() 不再写 auto_checkpoints（旧测试改写）；module_inputs 归档保留（警告 1 仍工作）
- `Tasklist.to_dict()` 与旧 `tasklist_to_dict` 输出一致
- 既有 smoke 回归（test_module / test_integration / smoke/*）

## 六、实现顺序

1. tickflow：ir 索引（D8）→ checker 缓存（D7）→ engine fireable（D4）→ state/runner 轻量快照（D1）+ `latest_firings`（D2）
2. module_harness：prompt 正则（C5）→ Tasklist.to_dict（D6）→ auto_checkpoints 退役（D3）→ resume/list_checkpoints/query_run_status 迁移（D2）
3. 测试改写 + 新增 → 全量回归（`python -m pytest module_harness/tests/ -q`）
4. 文档：roadmap #5 描述更新（自动检查点 → 轻量快照）、旧 spec 标注 auto_checkpoints 退役、本设计标记已实现
5. 提交；tickflow 改动清单备注随 sync 机制同步上游 Graph 仓库
