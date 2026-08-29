# 编程 API 参考（库面）

> 面向把 SpecModule 当库嵌入的宿主项目（MCP 服务器 / TUI / Web 后端等）。
> **按消费增量生长**：新 API 进入消费者通道时在此补录（做到哪里写哪里）。
> CLI 面见 [cli-usage.md](cli-usage.md)，声明语法见 [spec-harness-syntax.md](spec-harness-syntax.md)，
> 嵌入教程见 [../guides/embedding.md](../guides/embedding.md)。
>
> 首批内容 = MCP 消费通道（SpecModule_mcp）当前消费面。

## module_harness.query —— 运行产物查询与跨进程控制

查询函数（timeline/checkpoints/snapshot 摘要）**永不抛错**：run 目录不存在 / 无 DB / 读失败 → 返回 `None`，由调用方决定错误呈现。控制操作（`create_checkpoint`）按约定抛 `KeyError` 携带可用清单；目标类无效参数同样以 `KeyError` 呈现（见各行）。

| 函数 | 签名 | 行为 |
|------|------|------|
| `build_timeline` | `(module_id: str, base_dir: Path \| None = None) -> ReviewTimeline \| None` | 读 `run.sqlite` firings 构建历史时间线；无数据 → `None` |
| `timeline_to_dict` | `(timeline: ReviewTimeline) -> dict` | `{module_id, latest_tick, entries: [{tick, node, status, output, error}]}`；entry status ∈ `ok \| failed \| aborted` |
| `filter_tick` | `(timeline: ReviewTimeline, tick: int) -> ReviewTimeline` | 只留指定 tick |
| `filter_node` | `(timeline: ReviewTimeline, node: str) -> ReviewTimeline` | 只留指定节点 |
| `filter_failed` | `(timeline: ReviewTimeline) -> ReviewTimeline` | 只留失败条目 |
| `query_value` | `(module_id: str, path: str, *, base_dir: Path \| None = None) -> QueryValueResult \| None` | dot-path 细粒度查询（见下）；run 不存在 → `None`；空 path → `ValueError` |
| `build_checkpoints` | `(module_id: str, base_dir: Path \| None = None) -> CheckpointList \| None` | 列出全部回退点（tick 快照 + manual 检查点）；`checkpoints_to_dict` 出 `{module_id, checkpoints: [{target, tick, kind, fired, label}]}`，`target` 即 resume 目标 |
| `create_checkpoint` | `(module_id: str, label: str, *, tick: int \| None = None, base_dir: Path \| None = None) -> dict` | 给 tick 快照（缺省最新）命名——复制进 checkpoints 表，覆盖同名；返回 `{label, tick, overwritten}`；无运行/无快照/tick 不存在 → `KeyError` 带可用清单 |
| `load_snapshot_summary` | `(module_id: str, *, tick: int \| None = None, base_dir: Path \| None = None) -> dict \| None` | tick 快照摘要 `{tick, status, fireable, fired, outputs}`（outputs=各节点最新值，时点输出用 review(tick=N)）；db 缺失/读失败 → `None`（查询容错）；无快照/tick 不存在 → `KeyError` |
| `run_db_path` | `(module_id: str, base_dir: Path \| None = None) -> Path` | run.sqlite 路径规则单一来源（`<base>/.specmodule/runs/<id>/run.sqlite`）；`base_dir` 缺省 = cwd（服务器进程 cwd ≠ agent cwd，消费方宜显式传） |
| `build_run_graph` | `(module_name: str, run_id: str \| None = None, *, base_dir: Path \| None = None, template: str \| None = None, tasklist: dict \| Tasklist \| None = None, src: ModuleSource \| None = None) -> tuple[Graph, Tasklist] \| None` | 从运行存档（module_inputs 表）或直接给定的 tasklist 重建 tickflow Graph——可视化共用（CLI visualize / Web 图渲染）；零 LLM（registry 以 MockLLMClient 占位构建）；`tasklist` 给出时跳过存档（直渲染通道）；`src` 为预解析 ModuleSource（缺省 `store.resolve_module` 统一搜索路径解析）；返回 `(Graph, Tasklist)`，无存档且未传 tasklist → `None`；模块未找到/加载/构建失败 → `ValueError`（消息可直接面向用户）；packed/pip 模块无存档时回落模块自带 tasklist |
| `graph_to_dict` | `(graph: Graph, tasklist: Tasklist) -> dict` | tickflow Graph + Tasklist → 前端可视化结构（唯一新数据形状，Web/TUI 共用），见下 |

`query_value` 寻址语法：顶层标量 `phase` / `status` / `tick` / `fireable` / `fired` / `error` / `updated_at`；
输出 `outputs.<node>.<key...>`（节点最新输出内部键）；可变状态 `state.<node>.<key...>`（含 `_llm_raw`
等调试字段）；整数段 = list 下标。`QueryValueResult: {tick, value, found, available}`——路径未命中
`found=False` 且 `available` 给出该前缀下的可用键。

`graph_to_dict` 输出形状：`{"nodes": [{"id", "label", "type", "is_start", "join", "inputs"}],
"edges": [{"from", "to", "guard"}], "starts": [...]}`。`type`/`inputs` 取 tasklist 原始声明
（Graph 节点 inputs 有 field/producer 双键污染，不直接透出）；Graph 节点无对应 task 时
`type="unknown"` 降级（存档与代码漂移时不阻断渲染）。注意 `build_run_graph` 的模块解析错误走
`ValueError`（非查询容错通道——它是构建型操作，消费方映射 4xx）。

## module_harness.status —— 运行状态

| 函数 | 签名 | 行为 |
|------|------|------|
| `query_run_status` | `(module_id: str, base_dir: Path \| None = None) -> ModuleStatus \| None` | 读 `status.json`（+ DB 若有）合成静态快照；目录不存在 → `None`；失败 run 只有 status.json 也返回 |

`ModuleStatus` 字段：`module_id`、`phase`（`idle → translating → reviewing → building → ready →
running → done | aborted | cancelled`）、`status`（tickflow RunStatus，无 DB 时 None）、`tick`、
`fireable`、`fired`、`outputs`（node → 最新输出）、`node_states`（node → 可变状态）、`error`、`updated_at`。

`base_dir` 默认 cwd——跨进程消费（服务器形态）必须显式传。

## module_harness.store —— 模块发现与解析

| 函数 | 签名 | 行为 |
|------|------|------|
| `list_modules` | `(search: list[Path] \| None = None, include_pip: bool = True) -> dict[str, list[ModuleSource]]` | 枚举全部可用模块：entry 单文件 / packed 目录（`module.json`）/ pip entry points 三类来源合并；同名多来源**全量展示**（列表按 priority 升序，首项即解析命中项）；`search=None` 用 `search_paths()` |
| `resolve_module` | `(name: str, search: list[Path] \| None = None) -> ModuleSource \| None` | 按名解析：搜索序第一个命中（PATH 惯例，不静默改名）；未命中 → `None` |
| `search_paths` | `() -> list[Path]` | 搜索链 `cwd/modules + $SPECMODULE_PATH（os.pathsep 分隔）+ <store>/modules`，只含存在的目录 |
| `store_home` | `() -> Path` | `SPECMODULE_HOME` 环境变量或 `~/.specmodule`（惰性创建，幂等） |

`ModuleSource` 字段：`name`、`kind`（`entry | packed | pip`）、`path`（entry 文件 / pack 目录）、
`description`、`version`、`priority`（搜索路径序，0 最高；pip 排最后）、`pip_dist`。

entry 发现失败（缺 `entry` 变量 / 导入异常）逐文件跳过并 log，不阻断整体；pip 枚举失败整源跳过。

## module_harness.entry —— 入口声明

| 符号 | 形态 | 说明 |
|------|------|------|
| `discover_modules` | `(modules_dir: Path \| str) -> dict[str, ModuleEntry]` | 扫描 `*.py`（`_` 前缀跳过），收集模块级 `entry = ModuleEntry(...)` 变量；**键 = `entry.name`**（非文件名）；目录不存在 → 空 dict |
| `ModuleEntry` | dataclass | 见下 |
| `ModuleEntry.build_module` | 方法 | 统一接线 `(spec, *, template_name, tasklist, llm_client, module_id, base_dir, event_bus, hooks, review=True) -> Module`：loader 注册 + registry 构建 + Module 构造一步到位（CLI/MCP 共用，消除接线重复）；tasklist 优先——给出时 template 置 None；template_name 缺省回落 `default_template`；未注册模板 → `ValueError` 带**排序**可用清单；`review=False` → `review_harness=None`（存档续跑免重审） |

`ModuleEntry` 字段：`name`、`description`、`templates`（`{模板名: TasklistTemplate JSON}`）、
`submodules`（`{tasklist 名: SubModule 类}`）、`build_registry`（registry 构建器，可选）、
`default_spec`、`default_template`、`spec_schema`（`{字段: 类型名}`，可选校验）、
`review_harness`（默认 `"spec_tasklist_review"`）。

## Module 运行（module_harness.Module）

构造（`template_name` 与 `tasklist` **互斥**，都传或都不传 → `ValueError`；`llm_client` 必传）：

```python
Module(
    spec: dict,                     # 自由 dict（spec 通道输入）
    *,
    template_name: str | None,      # 模板通道（翻译器路径）
    tasklist: Tasklist | None,      # 内联 tasklist 通道（覆盖翻译）
    llm_client,                     # REQUIRED——见 llm 引导
    event_bus: EventBus | None,     # 事件订阅（不传零开销）
    template_loader: TemplateLoader | None,
    module_id: str | None,          # 缺省 mod_<8hex>；SubModule 为 <name>_<6hex>
    base_dir: Path | None,          # 落盘根（默认 cwd）；跨进程消费显式传
    registry: HarnessRegistry | None,
    persist: bool = True,           # False = NullBackend 全内存零落盘
    status_file: bool = True,       # False = 不写 status.json
    keep_records: bool = True,
    ...
)
```

执行：`await module.run(max_ticks=100)` —— 翻译 → 构建 → 运行一步跑完，返回 tickflow
firings 列表。**协程跑完整 run，无超时/取消 API**；`max_ticks` 是唯一运行上限（每次 LLM 调用
超时 60s）。落盘产物 `<base_dir>/.specmodule/runs/<run_id>/{run.sqlite, status.json}`，跨进程可查。
单写者约束：同一 `run_id` 并发写需调用方串行化。

续跑：`await module.resume(rollback_to=None, max_ticks=100)` —— `rollback_to` 为 tick 号 /
`"manual:<label>"` / `None`（缺省 = 最新 tick 快照）；目标不存在 → `KeyError` 带可用清单，
且失败时 `status.json` 落 `aborted` + error（不丢信息）；兼容性硬错误 → `ResumeError`；
`max_ticks` 为绝对 tick 上限（restore 于 tick 95 则默认只剩 5）。

## llm 引导（llm 包）

| 符号 | 签名 | 行为 |
|------|------|------|
| `create_llm_client` | `(config: LLMConfig)` | 按 `config.provider` 返回 `AnthropicClient` / `OpenAIClient`（`openai-compatible` 同） |
| `LLMConfig.from_env` | `(project_root=None, store_root=None, **overrides) -> LLMConfig` | 配置回退链：环境变量 → project_root → store_root 下的 `config.json` + `rules.txt` + `.env`；key 只经 `api_key_env` 指名的环境变量 / `.env`，config.json 不存密钥 |
| `MockLLMClient` | 类（`await complete(**kw) -> LLMResponse`） | 免 key 冒烟：`json_object` 输出返回宽松合法 JSON，text 输出为占位文本 |

客户端创建一次复用（per-connection / per-process），不要 per-call 创建。
