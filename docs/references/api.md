# 编程 API 参考（库面）

> 面向把 SpecModule 当库嵌入的宿主项目（MCP 服务器 / TUI / Web 后端等）。
> **按消费增量生长**：新 API 进入消费者通道时在此补录（做到哪里写哪里）。
> CLI 面见 [cli-usage.md](cli-usage.md)，声明语法见 [spec-harness-syntax.md](spec-harness-syntax.md)，
> 嵌入教程见 [../guides/embedding.md](../guides/embedding.md)。
>
> 首批内容 = MCP 消费通道（SpecModule_mcp）当前消费面。

## module_harness.infra.query —— 运行产物查询与跨进程控制

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
| `read_module_inputs` | `(module_id: str, base_dir: Path \| None = None) -> dict \| None` | 读运行输入存档 `{spec, tasklist}`（module_inputs 表，本次 run 使用的输入）；消费场景：resume/rollback 前预填上次输入（换 spec/tasklist 重传的编辑起点）；db 缺失/无存档/读失败 → `None` |
| `check_resume_compat_from_run` | `(module_name: str, run_id: str, *, new_tasklist: dict \| Tasklist \| None = None, target: int \| str \| None = None, base_dir: Path \| None = None) -> dict \| None` | 恢复预检：从运行产物组合兼容性校验材料（目标快照解析 + executed_nodes + 旧输入存档 + Mock registry 建图）跑 `check_resume_compat`——不 spawn、不写状态；`new_tasklist`/`target` 缺省用归档值（纯续跑预检）；目标解析失败不抛错、作为 `hard_errors[0]` 返回（附可用清单）；run.sqlite 缺失/读失败 → `None`；tasklist 非法/建图失败 → `ValueError`（消息可面向用户）。Web resume/preflight 端点消费；CLI `resume --dry-run` 需要时薄加 |
| `run_db_path` | `(module_id: str, base_dir: Path \| None = None) -> Path` | run.sqlite 路径规则单一来源（`<base>/.specmodule/runs/<id>/run.sqlite`）；`base_dir` 缺省 = cwd（服务器进程 cwd ≠ agent cwd，消费方宜显式传） |
| `build_run_graph` | `(module_name: str, run_id: str \| None = None, *, base_dir: Path \| None = None, template: str \| None = None, tasklist: dict \| Tasklist \| None = None, src: ModuleSource \| None = None) -> tuple[Graph, Tasklist] \| None` | 从运行存档（module_inputs 表）或直接给定的 tasklist 重建 tickflow Graph——可视化共用（CLI visualize / Web 图渲染）；零 LLM（registry 以 MockLLMClient 占位构建）；`tasklist` 给出时跳过存档（直渲染通道）；`src` 为预解析 ModuleSource（缺省 `store.resolve_module` 统一搜索路径解析）；返回 `(Graph, Tasklist)`，无存档且未传 tasklist → `None`；模块未找到/加载/构建失败 → `ValueError`（消息可直接面向用户）；packed/pip 模块无存档时回落模块自带 tasklist |
| `graph_to_dict` | `(graph: Graph, tasklist: Tasklist) -> dict` | tickflow Graph + Tasklist → 前端可视化结构（唯一新数据形状，Web/TUI 共用），见下 |
| `read_stream` | `(run_id: str, *, offset: int = 0, base_dir: Path \| None = None) -> dict \| None` | 增量读 `stream.log`（LLM 流式观测共享读端）：按完整行切分、每条记录附行首字节偏移 `off`；末尾不完整行不消费（`next_offset` 停在其行首）；坏行跳过；`offset > file_size` 钳回 0 自愈；缺失 → `None`。返回 `{"records", "next_offset", "file_size"}`；"只显示最近执行"的锚定策略由消费方据 `off` 定位最后一条 `run_start` |
| `list_runs` | `(base_dir: Path \| None = None) -> list[dict]` | 枚举全部运行（run 历史列表共享层，CLI `runs` / Web 共用）：扫 `<base>/.specmodule/runs/*/`，每 run 轻量读 status.json + sqlite 存在性；返回按 `updated_at` 降序的 `[{run_id, module, phase, tick, error, updated_at, has_sqlite}]`；status.json 缺失/损坏的 run 以 `phase="unknown"` 收入**不跳过**（删除入口要对坏目录可用，updated_at 记 0.0 沉底）；`tick` 优先取 status.json `tick` 键（前瞻兼容），否则有 sqlite 时 `latest_tick` 单条查询近似（**不解析快照**），再读不到 → None；查询永不抛错，runs 根不存在 → `[]` |
| `delete_run` | `(run_id: str, base_dir: Path \| None = None) -> bool` | 删除 run 目录整树（status.json + run.sqlite/WAL 侧车 + stream.log），run 历史单条删除共享层（CLI `delete-run` / Web 共用）；防路径穿越：`run_id` 为空/`.`/`..`/含路径分隔符（`/` `\`）/非 basename 形态（盘符、绝对路径——pathlib join 会被整路径替换）→ `False`；目录不存在 → `False`（调用方映射 404）；删除期 OSError 原样抛（本函数是操作不是查询）；运行中进程库侧不可知，活性防护（先 cancel/terminate）是消费端职责 |

CLI 对应子命令：`specmodule runs [--json]`（`list_runs` 列表展示）、`specmodule delete-run <run_id>`（删除并打印移除的目录；不存在报错退出非零）——参数语义见 [cli-usage.md](cli-usage.md)。

`query_value` 寻址语法：顶层标量 `phase` / `status` / `tick` / `fireable` / `fired` / `error` / `updated_at`；
输出 `outputs.<node>.<key...>`（节点最新输出内部键）；可变状态 `state.<node>.<key...>`（含 `_llm_raw`
等调试字段）；整数段 = list 下标。`QueryValueResult: {tick, value, found, available}`——路径未命中
`found=False` 且 `available` 给出该前缀下的可用键。

`graph_to_dict` 输出形状：`{"nodes": [{"id", "label", "type", "is_start", "join", "inputs"}],
"edges": [{"from", "to", "guard"}], "starts": [...]}`。`type`/`inputs` 取 tasklist 原始声明
（Graph 节点 inputs 有 field/producer 双键污染，不直接透出）；Graph 节点无对应 task 时
`type="unknown"` 降级（存档与代码漂移时不阻断渲染）。注意 `build_run_graph` 的模块解析错误走
`ValueError`（非查询容错通道——它是构建型操作，消费方映射 4xx）。

## module_harness.infra.status —— 运行状态

| 函数 | 签名 | 行为 |
|------|------|------|
| `query_run_status` | `(module_id: str, base_dir: Path \| None = None) -> ModuleStatus \| None` | 读 `status.json`（+ DB 若有）合成静态快照；目录不存在 → `None`；失败 run 只有 status.json 也返回 |

`ModuleStatus` 字段：`module_id`、`module`（源模块名，status.json `"module"` 键；旧格式无此键/直构未传 → None，消费端回落 run_id 启发式）、`phase`（`idle → translating → reviewing → building → ready →
running → done | aborted | cancelled | truncated`）、`status`（tickflow RunStatus，无 DB 时 None）、`tick`、
`fireable`、`fired`、`outputs`（node → 最新输出）、`node_states`（node → 可变状态）、`error`、`updated_at`。

`base_dir` 默认 cwd——跨进程消费（服务器形态）必须显式传。

## module_harness.infra.control —— 跨进程运行控制（控制文件协议）

status.json 的反向通道：status.json 把运行状态带出运行进程，control.json 把控制请求
（cancel/pause/unpause）带进运行进程。文件即协议——任何消费端（CLI/Web/TUI）可写，
运行进程在 **tick 边界协作式消费**（Module 默认注册 hook，`control=False` 关闭）。
不触碰 status.json 单写者规则。

协议：`<base_dir>/.specmodule/runs/<run_id>/control.json`，单发一次性请求
`{"action": "cancel" | "pause" | "unpause", "reason": str|null, "requested_at": float}`；
**消费即删**（delete-on-consume，防重放）；pause 挂起期间保留文件（文件本身就是
"暂停中"状态，监控方 `read_control` 可读）；**新执行清场**——`Module.run()/resume()`
开始时删除残留请求（崩溃残留的 pause 不拖住下一次运行）。

| 函数 | 签名 | 行为 |
|------|------|------|
| `control_path` | `(module_id: str, base_dir: Path \| None = None) -> Path` | 控制文件路径规则单一来源 |
| `read_control` | `(module_id: str, base_dir: Path \| None = None) -> dict \| None` | 容错读当前请求；无请求/损坏/action 非法 → `None`；监控方据此显示"暂停中" |
| `request_control` | `(module_id: str, action: str, *, reason: str \| None = None, base_dir: Path \| None = None) -> dict` | 原子写请求（tmp + os.replace）；返回写入的请求；action 非法 → `ValueError` |
| `clear_control` | `(module_id: str, *, base_dir: Path \| None = None) -> None` | 删除控制文件（消费/清场）；幂等 |
| `control_tick_start` | `(runner, module_id, *, base_dir: Path \| None = None, poll: float = 0.5)` | 工厂：注册到 `runner.on_tick_start` 的 async 回调，处理 pause 挂起（至 unpause/cancel；挂起中见到 cancel 不消费——留给 tick_end） |
| `control_tick_end` | `(runner, module_id, *, base_dir: Path \| None = None)` | 工厂：注册到 `runner.on_tick_end` 的 async 回调，cancel 的唯一消费点——引擎每 tick 末尾无条件重写 `runner.status`，tick_start 期设的 CANCELLED 会被冲掉（E2E 实测缺陷），tick_end 在赋值之后运行故终态能活到下轮 terminal 检查 |

生效语义：cancel 有一 tick 延迟——请求写在 tick N 内，N（或 N+1）结束时消费，
当前 tick 内已开始的 firing 跑完、下一 tick 前停；pause 挂起中即将 fire 的 tick
不启动、tick 计数不前进（max_ticks 不消耗）。

## module_harness.infra.store —— 模块发现与解析

| 函数 | 签名 | 行为 |
|------|------|------|
| `list_modules` | `(search: list[Path] \| None = None, include_pip: bool = True) -> dict[str, list[ModuleSource]]` | 枚举全部可用模块：entry 单文件 / packed 目录（`module.json`）/ pip entry points 三类来源合并；同名多来源**全量展示**（列表按 priority 升序，首项即解析命中项）；`search=None` 用 `search_paths()` |
| `resolve_module` | `(name: str, search: list[Path] \| None = None) -> ModuleSource \| None` | 按名解析：搜索序第一个命中（PATH 惯例，不静默改名）；未命中 → `None` |
| `resolve_module_full` | `(name: str, search: list[Path] \| None = None) -> ResolvedModule \| None` | 按名解析**并加载**模块详情（详情面/运行解析共享归一层）：解析同 `resolve_module`，命中后按形态加载——entry → `discover_modules` 取 ModuleEntry，packed/pip → ModuleLoader 轻量加载为 SubModule（不实例化 LLM client）；未找到 → `None`（调用方映射 404）；packed 加载失败/entry 入口解析失败 → `ValueError`（消息可直接面向用户，调用方映射 400） |
| `detail_to_dict` | `(resolved: ResolvedModule) -> dict` | 模块详情 JSON 出口：`{name, kind, path, version, description, default_template, templates: [名...], default_spec, spec_schema, submodules: [名...]}`（templates/submodules 排序出名，输出稳定；path 字符串化；default_spec/spec_schema 原样透传供前端填表/校验） |
| `search_paths` | `(base_dir: Path \| None = None) -> list[Path]` | 搜索链 `[base_dir or cwd]/modules + $SPECMODULE_PATH（os.pathsep 分隔）+ <store>/modules`，只含存在的目录；`base_dir` = 发现锚定根——服务器进程 cwd ≠ 运行根，跨进程消费显式传（效果：server 模块视图 ≡ spawn 子进程 CLI 视图），None = cwd 向后兼容；优先序不变 |
| `store_home` | `() -> Path` | `SPECMODULE_HOME` 环境变量或 `~/.specmodule`（惰性创建，幂等） |

`ModuleSource` 字段：`name`、`kind`（`entry | packed | pip`）、`path`（entry 文件 / pack 目录）、
`description`、`version`、`priority`（搜索路径序，0 最高；pip 排最后）、`pip_dist`。

`ResolvedModule` 字段：`name`、`source`（ModuleSource）；`entry`（entry 形态时的 ModuleEntry）与
`submodule`（packed/pip 形态时加载后的 SubModule）按形态二选一；归一属性 `kind` / `description` /
`default_template` / `default_spec` / `spec_schema`（packed 归一为 `spec_schema.input`）/ `templates` /
`submodules` 屏蔽两形态差异，消费端不感知来源。

entry 发现失败（缺 `entry` 变量 / 导入异常）逐文件跳过并 log，不阻断整体；pip 枚举失败整源跳过。

## module_harness.cli.entry —— 入口声明

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
    module: str | None,             # 源模块名（溯源）：写 status.json "module" 键；entry.build_module / SubModule.run 自动传
    base_dir: Path | None,          # 落盘根（默认 cwd）；跨进程消费显式传
    registry: HarnessRegistry | None,
    persist: bool = True,           # False = NullBackend 全内存零落盘
    status_file: bool = True,       # False = 不写 status.json
    control: bool = True,           # False = 不注册控制文件 hook（禁用跨进程 cancel/pause）
    stream_log: bool = True,        # False = 不写 stream.log（LLM 流式观测通道）
    keep_records: bool = True,
    ...
)
```

执行：`await module.run(max_ticks=100)` —— 翻译 → 构建 → 运行一步跑完，返回 tickflow
firings 列表。协程跑完整 run；**进程内取消 = 取消该 asyncio task**（phase 落
`cancelled`）；**跨进程取消/暂停走 control 文件通道**（见 module_harness.infra.control——
默认启用，运行进程在 tick 边界协作消费）；`max_ticks` 是唯一运行上限（每次 LLM 调用
超时 60s）。`max_ticks` 耗尽 → phase 落 **`truncated`**（终态，`error` 记 `max_ticks=N 截断（可 resume 续跑）`）——
监控方拿到可续跑的确定性信号，无需静默启发式。落盘产物 `<base_dir>/.specmodule/runs/<run_id>/{run.sqlite, status.json}`，
跨进程可查（status.json 含 `"module"` 键 = 源模块名，供 run 历史按模块归档；旧 run 无此键）。
单写者约束：同一 `run_id` 并发写需调用方串行化。
LLM 流式输出经 EventBus 订阅落盘 `stream.log`（JSONL，append-only：`run_start` 为每次执行
边界，后接 `call_start`/`token`/`call_end`/`call_error`；`ts` 为 wall-clock；
`EventBus.null()` 场景仅 `run_start`）；增量读走 `query.read_stream`。

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
