# 轻量快照（每 tick O(1)）设计文档

> **范围扩展（2026-08-06）**：轻量快照进一步最小化——持久路径快照剥离
> `run_state.edges/fire_counts/state`（S3 死数据：restore 的 truncate_after
> 本就从 firings 重建）；`resume()` 改 tick 号回退；`list_checkpoints()` 显示
> (tick, fired)。详见 `2026-08-06-redundancy-cleanup-design.md`。

> 日期：2026-08-07 | 状态：已确认，待实现
> 关联：roadmap #5 快照/回滚的存储演进；tickflow 跨仓库同步（Graph 仓库为源）

## 概述

将 tickflow 持久快照从"每 tick 全量（含审计 records，O(t) 大小）"改为"每 tick 轻量（剥离 records，O(1) 恒定大小）"，总存储从 O(n²) 降为 O(n)。同时修正 `resume()` 的回退语义为**精确 tick 号回退**，为后续"历史审阅"能力铺路（tick ↔ 产出对应）。

**核心洞察**：持久路径下快照里的 records 是冗余的——`audit()` 从 backend firings 表查询（state.py:275-286），`restore()` 的 `truncate_after` 从 firings 重建窗口/计数/state（state.py:421-458）。剥离 records 后快照大小 = O(节点数 + 边数) 恒定（marking + edges 窗口每节点 ≤2 条 + state 每节点 1 条 + status/fireable）。

## 现状与问题

| | 现状 | 问题 |
|---|---|---|
| `_persist_tick` | 每 tick 存 `runner.snapshot()` 全量（keep_records=True 时含完整审计 records，O(t) 大小） | 总存储 ΣO(t) = **O(n²)**；长程任务（1000+ tick）几百 MB 起 |
| `auto_checkpoints` 表（Module 层） | 每 tick 再存一份全量快照（环形 20） | 与 snapshots 表**双份重复**（同一快照对象两份拷贝） |
| `resume(rollback_to)` | 回退到 auto:tick:N label 或 manual label | label 与 tick 差 1（hook 语义怪癖）；节点名定位不到"运行后审阅发现的问题 tick" |

## 设计决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 存储方案 | **方案 2：每 tick 存轻量快照**（剥离 records，O(1) 恒定）——不需要 snapshot 节点属性、不需要心跳表、任意 tick 可回退 |
| 2 | 快照语义 | 轻量快照加 `fired: list[str]`（本 tick fire 的节点名）——"tick 内的 node name 列表就是天然语义" |
| 3 | 回退目标 | **tick 号回退**（精确）；手动检查点 label 保留；不再按节点名 |
| 4 | 历史审阅 | 后续功能（tick ↔ 产出对应），本设计只铺存储基础（fired 字段 + firings 表已有完整产出） |
| 5 | 跨仓库 | tickflow 改动在 **Graph 仓库**（源）做，同步到 SpecModule；Module 层适配在 SpecModule |
| 6 | 无存档点图 | 方案 2 下**无此问题**——每 tick 都有轻量快照（persist=True 时），任何图任意 tick 可回退，resume 永不降级 |

---

## 第一部分：tickflow 核心改动（Graph 仓库）

### 改动面（最小）

| 文件 | 改动 |
|------|------|
| `tickflow/state.py` | `to_snapshot_data()` 加 `include_records: bool = True` 参数（默认 True 保持现状） |
| `tickflow/runner.py` | `snapshot(include_records=True)` 加参数透传；`_persist_tick` 存轻量快照（`include_records=False`）+ 填入 `fired` 字段 |
| `tickflow/runner.py` | `_persist_tick` 需要知道本 tick fire 的节点名——调用处（sync/async tick 循环）传入 `fired: list[str]` |
| 其余 | `restore()` 不变（持久路径不读快照 records）；`to_json()/from_json()` 不变（显式完整导出，含 records 重嵌）；fast mode（NullBackend）跳过不变（D7） |

### 轻量快照结构

```python
{
    "tick": tick_count,
    "marking": {...},            # slots + armed_starts（O(边数)）
    "run_state": {
        "edges": {...},          # 每节点窗口 ≤2 条（O(节点数)）
        "fire_counts": {...},
        "state": {...},          # 每节点最新 mutable_state（O(节点数)）
        "keep_records": ...,
        # 无 records（剥离）
    },
    "status": ...,
    "cancel_reason": ...,
    "fireable": [...],           # O(节点数)
    "fired": ["align_check", ...],  # 新增：本 tick fire 的节点名（O(本 tick 节点数)）
}
```

大小 = O(节点数 + 边数)，**不随运行 tick 增长**。

### 语义验证（已核实）

- **restore 到任意 tick 完整可用**：`truncate_after(tick-1)` 从 backend firings 重建窗口/计数/state（state.py:421-458）；records 由 `audit()` 从 backend 查询（state.py:275-286）
- **`fired` 字段**：`_persist_tick` 由调用处传入本 tick 的 `[f.node for f in firings]`；空 tick（无 fireable）为 `[]`，快照照存（轨迹连续性）
- **旧快照兼容**：旧快照（含 records、无 `fired`）restore 不受影响；`fired` 读取用 `.get("fired", [])` 容错
- **`keep_records=False`**：剥离 records 是 no-op（本来就没有）

---

## 第二部分：Module 层适配（SpecModule）

### `AutoCheckpointStore` 瘦身 → `ModuleInputStore`

- 删除 `auto_checkpoints` 表及其 save/load/list/环形保留逻辑
- **保留 `module_inputs` 表**（spec/tasklist 存档，兼容性校验警告 1 依赖它对比新旧 tasklist）
- 类改名为 `ModuleInputStore`（职责单一），`checkpoint.py` 中引用同步清理；`module_harness/__init__.py` 导出更新

### `run()` 的 hook 退役

- `_register_auto_checkpoint()` 删除——tickflow `_persist_tick` 自己每 tick 存轻量快照，Module 层不再重复写
- `module_inputs` 归档保留：提取为独立私有方法 `_archive_module_inputs()`（写 spec/tasklist 存档），`run()` 与 `resume()` 都调用（run 归档原始输入，resume 覆盖为新输入）

### `resume(rollback_to)` 语义变化

```python
async def resume(self, rollback_to: int | str, max_ticks: int = 100)
```

| rollback_to | 解析 | 恢复 |
|-------------|------|------|
| tick 号（`357` 或 `"357"`） | 直接使用 | `SqliteBackend.load_snapshot(module_id, tick)` → `runner.restore(snap)` |
| manual label（`"manual:xxx"`） | backend checkpoints 表 | `load_checkpoint` → restore（保留） |
| 其他（非数字非 manual 前缀） | — | `KeyError`，附可用 tick 范围 + manual label 列表 |

- 节点名不再作为回退目标（运行后审阅发现问题在 xx tick → 精确 tick 回退）
- 兼容性校验（`check_resume_compat`）不变——marking_slots/executed_nodes 仍从快照取
- fast mode 守卫保留：`RuntimeError("resume 需要 persist=True")`

### `list_checkpoints()`

- snapshots 表 tick 列表，每个显示 `(tick, fired 节点列表)`——历史审阅的雏形
- manual checkpoints（backend 表）合并
- 环形 20 概念消失（方案 2 每 tick 快照，无需活动窗口）

### 进程内 API 不变

- `snapshot()/restore()` 不变（`runner.snapshot()` 默认 `include_records=True` 保持完整，进程内调试用）
- `checkpoint(label)/rollback_to(label)` 透传不变（manual 路径）
- `query_run_status` 不变即可工作（轻量快照字段齐全 + `fired` 显示）

---

## 错误处理矩阵

| 场景 | 行为 |
|------|------|
| `resume("abc")`（非数字、非 manual label） | `KeyError`，附可用 tick 范围 + manual label 列表 |
| `resume(999)` 超出快照 tick 范围 | `KeyError`，附快照 tick 范围 |
| fast mode（persist=False） | `RuntimeError("resume 需要 persist=True")`（现状守卫保留） |
| 旧快照（无 `fired` 字段） | `.get("fired", [])` 容错；restore 不受影响 |
| 空 tick（无 fireable） | `fired: []`，快照照存 |
| 快照写失败 | 现状容错（log 不阻断）不变 |

---

## 测试计划

### Graph 仓库（tickflow 侧）

- `to_snapshot_data(include_records=False)`：无 records、其余字段（edges/fire_counts/state/marking）完整
- `_persist_tick` 落盘快照 data 列无 records（直接查 DB 验证）+ `fired` 字段正确填入
- **restore 到任意 tick 完整恢复**（轻量快照 restore → 窗口/计数/state 从 firings 重建 → 续跑正确）
- `audit()` 持久路径仍返回完整审计（不因快照无 records 而退化）
- `to_json/from_json` 仍含 records（显式导出不变）
- fast mode 跳过不变；空 tick `fired: []`

### SpecModule（Module 层）

- `resume(tick)` 直接恢复；`resume("357")` 字符串解析；`resume("manual:xxx")` 手动路径
- `resume("abc")`/`resume(999)` → KeyError 附可用信息
- `list_checkpoints()` 显示 `(tick, fired 列表)` + manual 合并
- run() 不再写 auto_checkpoints（旧测试改写）；module_inputs 归档保留（警告 1 仍工作）
- `query_run_status` 显示 `fired`；既有 smoke 回归

---

## 同步与实现顺序（跨仓库）

1. **Graph 仓库**：tickflow 改动（state.py + runner.py）+ Graph 自身 tests 全绿 → commit
2. **同步到 SpecModule**：文件复制（现状机制）→ sync commit
3. **SpecModule**：Module 层适配（`ModuleInputStore` 瘦身 / resume / list_checkpoints / 测试改写）→ 全量回归
4. 文档更新：roadmap #5 描述更新（自动检查点 → 轻量快照）、旧 spec（2026-08-06-snapshot-rollback-design.md）标注 auto_checkpoints 退役、AGENTS.md 表约定

## 后续功能（不在本设计范围）

- **历史审阅**：把历史产出（firings：每 tick 每节点的 inputs/output/status/error）与 tick 对应，用户运行后审阅定位问题 tick。本设计已铺基础：`fired` 字段（tick ↔ 节点轨迹）+ firings 表（完整产出）
