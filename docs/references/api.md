# 编程 API 参考（库面）

> 面向把 SpecModule 当库嵌入的宿主项目（MCP 服务器 / TUI / Web 后端等）。
> **按消费增量生长**：新 API 进入消费者通道时在此补录（做到哪里写哪里）。
> CLI 面见 [cli-usage.md](cli-usage.md)，声明语法见 [spec-harness-syntax.md](spec-harness-syntax.md)，
> 嵌入教程见 [../guides/embedding.md](../guides/embedding.md)。
>
> 首批内容 = MCP 消费通道（SpecModule_mcp）当前消费面。

## module_harness.query —— 运行产物查询（只读）

查询函数**永不抛错**：run 目录不存在 / 无 DB / 无数据 → 返回 `None`，由调用方决定错误呈现。

| 函数 | 签名 | 行为 |
|------|------|------|
| `build_timeline` | `(module_id: str, base_dir: Path \| None = None) -> ReviewTimeline \| None` | 读 `run.sqlite` firings 构建历史时间线；无数据 → `None` |
| `timeline_to_dict` | `(timeline: ReviewTimeline) -> dict` | `{module_id, latest_tick, entries: [{tick, node, status, output, error}]}`；entry status ∈ `ok \| failed \| aborted` |
| `filter_tick` | `(timeline: ReviewTimeline, tick: int) -> ReviewTimeline` | 只留指定 tick |
| `filter_node` | `(timeline: ReviewTimeline, node: str) -> ReviewTimeline` | 只留指定节点 |
| `filter_failed` | `(timeline: ReviewTimeline) -> ReviewTimeline` | 只留失败条目 |
| `query_value` | `(module_id: str, path: str, *, base_dir: Path \| None = None) -> QueryValueResult \| None` | dot-path 细粒度查询（见下）；run 不存在 → `None`；空 path → `ValueError` |

`query_value` 寻址语法：顶层标量 `phase` / `status` / `tick` / `fireable` / `fired` / `error` / `updated_at`；
输出 `outputs.<node>.<key...>`（节点最新输出内部键）；可变状态 `state.<node>.<key...>`（含 `_llm_raw`
等调试字段）；整数段 = list 下标。`QueryValueResult: {tick, value, found, available}`——路径未命中
`found=False` 且 `available` 给出该前缀下的可用键。

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

## llm 引导（llm 包）

| 符号 | 签名 | 行为 |
|------|------|------|
| `create_llm_client` | `(config: LLMConfig)` | 按 `config.provider` 返回 `AnthropicClient` / `OpenAIClient`（`openai-compatible` 同） |
| `LLMConfig.from_env` | `(project_root=None, store_root=None, **overrides) -> LLMConfig` | 配置回退链：环境变量 → project_root → store_root 下的 `config.json` + `rules.txt` + `.env`；key 只经 `api_key_env` 指名的环境变量 / `.env`，config.json 不存密钥 |
| `MockLLMClient` | 类（`await complete(**kw) -> LLMResponse`） | 免 key 冒烟：`json_object` 输出返回宽松合法 JSON，text 输出为占位文本 |

客户端创建一次复用（per-connection / per-process），不要 per-call 创建。
