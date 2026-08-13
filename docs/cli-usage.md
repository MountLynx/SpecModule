# specmodule CLI 使用文档

> 使用者层面（usage scenario）入口：终端选择模块、传 spec/tasklist、运行并观察 / 审阅工作流。
> 二级用户只写 spec/tasklist，不写 Python；模块入口由开发者在 `modules/<name>.py` 声明。
> 配套设计：`docs/superpowers/specs/2026-08-10-specmodule-cli-design.md`
> 实施计划：`docs/superpowers/plans/2026-08-10-specmodule-cli.md`

## 1. 进入方式

无打包（仓库无 pyproject），与 `python -m tickflow` 一致：

```bash
python -m module_harness.cli --help
```

四个子命令：`init` / `run` / `status` / `review`。

```
usage: specmodule [-h] {init,run,status,review} ...
```

## 2. 命令概览

| 命令 | 作用 | 形态 |
|------|------|------|
| `init` | 生成模块开发脚手架（单文件模块 + 项目文件补齐） | 非交互参数 |
| `run` | 按名选模块 + spec/tasklist，运行并实时显示 | 三级 verbose |
| `status` | 查询某次运行状态（复用 `query_run_status`） | 文本 / JSON |
| `review` | 审阅历史时间线（tick 分组 + 过滤） | 文本 / JSON |

核心数据流：

```
init: CLI → scaffold()（纯函数生成：modules/<name>.py + 项目文件缺啥补啥）
run:  CLI → discover(modules/) → 解析 spec/tasklist → Module(llm_client, event_bus,
        registry, hooks) → asyncio.run() → on_fire 逐 firing 实时打印 → 结束汇总
status: CLI → query_run_status()（既有）→ 文本/JSON
review: CLI → build_timeline()（共享层，MCP/Web 复用）→ filter_* → 文本/JSON
```

查询组合逻辑沉淀在 `module_harness/query.py`（CLI/MCP/Web 三形态共用），CLI 只 import 不重实现。

---

## 3. `run` — 运行模块

```
python -m module_harness.cli run --module <名> [选项]
```

### 参数

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `--module <名>` | 是 | — | 模块名，在 `--modules-dir` 中发现 |
| `--modules-dir <dir>` | — | `modules/`（cwd 相对） | 模块目录；未来 init 实例布局即此目录 |
| `--spec '<JSON>'` | 见下 | — | 内联 JSON spec |
| `--spec-file <path>` | 见下 | — | spec JSON 文件路径 |
| `--template <名>` | — | `entry.default_template` | 模板名 |
| `--tasklist <path>` | — | — | tasklist JSON 文件（跳过翻译，与 `--template` 互斥） |
| `--run-id <id>` | — | 模块名 | 运行目录名（`.specmodule/runs/<run_id>/` 落盘） |
| `--verbose {1,2,3}` | — | `1` | 实时显示级别 |
| `--max-ticks <n>` | — | `100` | tick 上限 |
| `--mock` | — | 关 | 免 key 假 LLM 冒烟（测试/演示） |

### spec 解析优先级

`--spec`（内联 JSON）> `--spec-file`（文件）> `entry.default_spec`（模块声明的默认）> 报错：

```
错误: 缺少 spec——请用 --spec（内联 JSON）或 --spec-file（文件）
```

spec 必须是 JSON **对象**。命令行传内联：

```bash
python -m module_harness.cli run --module academic_writer \
  --spec '{"raw_text":"灵感草稿……"}' --mock
```

或传文件：

```bash
python -m module_harness.cli run --module academic_writer \
  --spec-file example/spec.academic_writer.json --mock
```

### 流程：模板 vs tasklist（二选一）

- `--template <名>`：从模板**翻译** spec → tasklist（框架原生多模板通道）。默认用 `entry.default_template`。
- `--tasklist <path>`：跳过翻译，直接按给定 tasklist JSON（`{Tasks, Flow}`）运行。此时 `--template` 不可用。

两者同时给会报错：

```
错误: --tasklist 与 --template 互斥——只能二选一
```

模板名未注册（不在 `entry.templates`）报错：

```
错误: 模板 '<名>' 未注册——可用: <t1>, <t2>
```

### 三级实时显示（`--verbose`）

由 runner hooks（`on_fire` / `on_tick_start`）驱动，逐 firing 打印：

| 级别 | 输出 | 失败节点附加 |
|------|------|-------------|
| **L1**（默认） | `tick 3  Organize ✓`（tick+节点+状态） | error + 产出预览截断 |
| **L2** | L1 + 全部节点产出预览（约 80 字符截断） | 同 L1 |
| **L3** | 完整块：tick 分隔线 + 输入摘要 + 完整产出 + error | 同 L1 |

L1 示例：

```
tick 0  Organize  ✓
tick 1  Loop1     ✓
```

L3 额外打印：

```
═══ tick 0 ═══ fireable: Organize
── tick 0  Organize  [ok]
    output : "整理后的英文文段……"
```

### 结束汇总

运行结束后打印：模块名、run_id、节点 firing 总数、每节点最新输出摘要：

```
运行完成: module=academic_writer run_id=academic_writer
共 6 次节点 firing
节点最新输出摘要:
  Organize: "mock output"
  ...
```

### 落盘

每次运行写入 `<cwd>/.specmodule/runs/<run_id>/`：

```
.specmodule/runs/<run_id>/
  run.sqlite    # firings + 快照（tick 级）
  status.json   # 阶段级状态（跨进程查询通道）
```

同模块多次运行累积（run_id 默认 = 模块名）。

### `--mock` 冒烟

免 key / 免网络：内置通用假客户端（`MockLLMClient`）。`json_object` 输出返回宽松合法 JSON，`text` 返回占位文本。

> ⚠️ **局限**：mock 返回的是**通用占位内容**，不匹配具体模块的产出 schema（如 M1 的 `{text, notes}`）。它验证的是**流水线接线**（节点是否按图触发、hooks/落盘/review 是否工作），**不验证内容质量**。真实产出需配 API key 运行。

### Ctrl+C

运行中 `Ctrl+C`：打印已执行 firing 数与提示，退出码 **2**。运行数据已落盘，可用 `status`/`review` 查询（本子集不提供续跑）。

---

## 4. `status` — 查询运行状态

```
python -m module_harness.cli status [--run-id <id>] [--json]
```

- `--run-id` 缺省 = 最近运行（`.specmodule/runs/` 中 mtime 最新的子目录）。
- 复用 `query_run_status`：读 `status.json`（阶段）+ `run.sqlite`（tick 级信息）；DB 读失败降级 phase-only。

文本输出：

```
模块 academic_writer: phase=done tick=7
runner: idle
```

`--json` 输出 `ModuleStatus` 结构化字段（`module_id/phase/status/tick/fireable/fired/outputs/node_states/error/updated_at`）。

无运行记录：

```
无运行记录: <id>（先执行 specmodule run）
```

---

## 5. `review` — 审阅历史时间线

```
python -m module_harness.cli review [--run-id <id>] [--tick N] [--node <名>] [--failed] [--json]
```

默认按 tick 分组文本时间线：

```
tick 0: Organize ✓
tick 1: Loop1 ✓
tick 2: Polish ✓
...
最新 tick: 5
```

失败节点行高亮 + error 详情：

```
tick 3: Finalize ✗
  ✗ Finalize: 输出格式校验失败……
```

### 过滤

| 参数 | 作用 |
|------|------|
| `--tick N` | 只看指定 tick（每节点完整产出） |
| `--node <名>` | 只看某节点全部 firing（含 loop 多轮） |
| `--failed` | 只看失败/中止节点（定位问题 tick 核心路径） |

> 过滤后 `最新 tick` 显示**过滤子集**的最新 tick（非全局），与所见内容一致。

### JSON 出口

`--json` 原样输出 `timeline_to_dict`（MCP/Web 直接消费同一函数）：

```json
{
  "module_id": "academic_writer",
  "latest_tick": 5,
  "entries": [
    {"tick": 0, "node": "Organize", "status": "ok", "output": "...", "error": null},
    ...
  ]
}
```

去重语义：同 `(tick, node)` 保留首条（与 tickflow `audit()` 一致，兼容 restore 重放）。

---

## 6. 退出码

| 码 | 含义 |
|----|------|
| `0` | 成功 |
| `1` | 错误（模块未找到 / spec 缺失 / schema 违规 / 模板未找到 / LLM 未配置 / 参数互斥 / 无运行记录 / tasklist 校验失败） |
| `2` | `Ctrl+C`（运行中中断） |

错误信息走 stderr，统一 `SystemExit` + 非零退出码。

### 常见错误速查

| 场景 | 报错 | 处理 |
|------|------|------|
| 模块未找到 | `模块 '<名>' 未找到……` + 打印可用模块列表 | 检查 `--module` 名 |
| LLM 未配置 | `LLM 未配置 API key……` | 配 `config.json` + `.env`，或加 `--mock` |
| spec 违反 schema | `spec 校验失败: - 缺少字段 'raw_text'……` | 补全字段 |
| tasklist 校验失败 | `tasklist 校验失败: - ...` | 修复 tasklist |

---

## 7. 模块入口合约（开发者）

一个模块一个 py 文件：`modules/<name>.py`，文件内声明模块级 `entry` 变量（`ModuleEntry`）。CLI 经 `discover_modules()` 导入发现。

```python
# modules/<name>.py
from module_harness.entry import ModuleEntry

entry = ModuleEntry(
    name="my_module",                  # CLI --module 用
    description="……",                  # 展示
    templates={...},                   # {模板名: TasklistTemplate JSON}
    submodules={...},                  # {tasklist 名: SubModule 类}
    build_registry=...,                # (llm_client, template_name, event_bus) -> registry
    default_spec={...},                # 无 --spec/--spec-file 时的兜底
    default_template="...",            # 无 --template 时的兜底（须在 templates 中）
    spec_schema={...},                 # {字段: 类型名} 可选校验
    review_harness="spec_tasklist_review",  # 一致性审核 harness；固定流程可置 None
)
```

发现规则：扫描 `modules_dir/*.py`；缺 `entry` 或类型不符的文件跳过并 log 警告；同名冲突后者覆盖；导入抛异常的文件跳过（不阻断整体发现）；`_` 前缀文件跳过。

`build_registry` 契约要点：第三个参数 `event_bus`——作者必须把外部 bus 接入，否则 CLI 收不到 harness 事件。

---

## 8. 完整示例（M1 论文优化）

```bash
# 免 key 冒烟（跑通流水线，内容为占位）
python -m module_harness.cli run --module academic_writer \
  --spec-file example/spec.academic_writer.json --mock --verbose 2

# 真实 LLM
python -m module_harness.cli run --module academic_writer \
  --spec-file example/spec.academic_writer.json --verbose 2

# 详细模式（事实审阅 loop 内联展开，全程可审计）
python -m module_harness.cli run --module academic_writer \
  --template academic_writer_detailed --spec-file example/spec.academic_writer.json

# 审阅
python -m module_harness.cli review --run-id academic_writer        # 时间线
python -m module_harness.cli review --failed                        # 只看失败
python -m module_harness.cli review --tick 3                        # 单 tick 详情
python -m module_harness.cli review --json                          # 结构化出口
```

`--modules-dir` 说明：示例模块在 `example/modules/`，需显式指定：

```bash
python -m module_harness.cli run --module academic_writer \
  --modules-dir example/modules --spec-file example/spec.academic_writer.json --mock
```

---

## 9. `init` — 生成模块开发脚手架

```
python -m module_harness.cli init <name> [--dir PATH] [--force] [--description "..."]
```

生成单文件 python 原生模块骨架（`modules/<name>.py`）+ 项目文件缺啥补啥（幂等）。

### 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `<name>` | 是 | 模块名（合法 Python 标识符；同时是文件、`--module`、`entry.name`、默认 run_id——四处一致） |
| `--dir <path>` | — | 生成位置（默认 cwd） |
| `--force` | — | 覆盖已存在的模块文件（仅模块文件） |
| `--description <str>` | — | 模块描述（展示用，不受标识符约束） |

### 生成布局

```
<project>/
├─ modules/<name>.py    单文件模块骨架（harness/script/模板/registry/入口 五区块）
├─ config.json          provider/model 注册表（占位，真实运行前需填写）
├─ .env.example         API key 占位（复制为 .env 填密钥）
├─ .gitignore           （排除 .env / __pycache__ / .specmodule）
├─ spec.example.json    示例 spec
└─ README.md            用法 + config.json / .env 分工说明
```

### 默认模板（立即冒烟）

默认模板 `hello` 为 **harness → script 流水线**：harness 节点读入 spec 的 `message` 字段
（prompt 占位符由 `{spec.message}` inputs 填充），script 节点消费其输出并回显。一个文件同时
展示 harness 声明、script 组件、多节点 flow 三种契约。

```bash
# 免 key 冒烟（验证流水线接线，非内容质量）
python -m module_harness.cli run --module <name> --mock
```

### 冲突 / 幂等语义

- 模块名非法（含空格/连字符/中文等）：报错退出码 1，**零文件生成**。
- `modules/<name>.py` 已存在且未传 `--force`：报错退出码 1。
- 项目文件（config.json 等）已存在一律**跳过不覆盖**；`--force` 仅覆盖模块文件。

### 配置分工

- `config.json`：非敏感注册表（providers 连接信息 + `api_key_env` 指向的**变量名**）。
- `.env`：密钥实际值（gitignored，不进版本库）。`config.json` 的 `api_key_env` 与 `.env`
  变量名必须对齐。

---

## 10. 范围 / 后续迭代

本子集（Phase 0）**不含**（已记 roadmap，后续迭代）：

- 截断/暂停续跑（Ctrl+C 保存状态 → `resume`）
- `snapshot` / `rollback` CLI 命令（能力已就位：`Module.snapshot/restore/checkpoint/rollback_to`，仅缺命令形态）
- `visualize`（mermaid 导出）
- `init` 声明式形态（scripts/harnesses/submodules/modules 分目录 + loader 改造）与消费者 module 管理指令——python 原生单文件形态已在此实现
- AGENT（MCP/ACP）与 Web 形态——直接消费 `query.py` 共享层