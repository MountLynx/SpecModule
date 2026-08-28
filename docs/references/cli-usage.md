# specmodule CLI 使用文档

> 使用者层面（usage scenario）入口：终端选择模块、传 spec/tasklist、运行并观察 / 审阅工作流。
> 二级用户只写 spec/tasklist，不写 Python；模块入口由开发者在 `modules/<name>.py` 声明。
> 配套设计：`docs/dev/superpowers/specs/2026-08-10-specmodule-cli-design.md`
> 实施计划：`docs/dev/superpowers/plans/2026-08-10-specmodule-cli.md`

## 1. 进入方式

`pip install specmodule` 后直接使用 `specmodule` 命令（console script）；仓库源码开发时用模块等价入口：

```bash
pip install specmodule              # 安装（CLI 随库分发）
python -m module_harness.cli --help # 源码 / 开发环境等价入口
```

十八个子命令：`init` / `run` / `status` / `review` / `resume` / `checkpoint` / `checkpoints` / `snapshot` / `rollback` / `visualize` / `feed` / `list` / `info` / `install` / `uninstall` / `setup` / `publish` / `update`。

```
usage: specmodule [-h] {init,run,status,review,resume,checkpoint,checkpoints,snapshot,rollback,visualize,feed,list,info,install,uninstall,setup,publish,update} ...
```

## 2. 命令概览

| 命令 | 作用 | 形态 |
|------|------|------|
| `init` | 生成模块开发脚手架（单文件 `--as-dir` 目录形态 + 项目文件补齐） | 非交互参数 |
| `run` | 按名选模块 + spec/tasklist，运行并实时显示 | 三级 verbose |
| `status` | 查询某次运行状态（复用 `query_run_status`） | 文本 / JSON |
| `review` | 审阅历史时间线（tick 分组 + 过滤） | 文本 / JSON |
| `resume` | 从中断处续跑模块（tick 截断 / Ctrl+C 后，缺省续最新） | 三级 verbose |
| `checkpoints` | 列出可用回退点（tick 快照 + manual 检查点） | 文本 / JSON |
| `snapshot` | 检视/导出指定 tick 的运行时快照 | 文本 / JSON / 文件 |
| `rollback` | 回退到指定 tick/manual 检查点并重跑（目标必填） | 三级 verbose |
| `checkpoint` | 给指定 tick 快照起命名检查点（`manual:` 永久保留） | 非交互参数 |
| `visualize` | 渲染 tasklist 对应图（mermaid 导出） | 文本 / 文件 |
| `feed` | 零依赖运行 feed（http.server，浏览器轮询查看） | 服务 |
| `list` | 列出全部可用模块（同名多来源全量展示） | 文本 / JSON |
| `info` | 显示模块详情（元数据 + 来源 + 安装信息） | 文本 |
| `install` | 安装模块到 store（本地 pack 目录或 git URL，校验零落盘） | 非交互参数 |
| `uninstall` | 从 store 移除模块（目录 + manifest） | 非交互参数 |
| `setup` | 一次性配置向导：provider/model/key → 写 store 级配置 | 交互 |
| `publish` | 发布模块到 store（目录形态校验复制；单文件形态经 SubModule 转化） | 非交互参数 |
| `update` | 更新模块（manifest 脏检测；本地改动列清单交互确认） | 交互 / `--yes` / `--keep` |

核心数据流：

```
init: CLI → scaffold()/scaffold_dir()（纯函数生成：modules/<name>.py 或
        modules/<name>/ 目录骨架 + 项目文件缺啥补啥）
run:  CLI → 统一解析（store.resolve_module：cwd/modules + $SPECMODULE_PATH +
        store/modules + pip）→ entry 走 discover_modules / packed 走 ModuleLoader
        → 解析 spec/tasklist → Module/SubModule → asyncio.run() → 实时打印 → 汇总
status: CLI → query_run_status()（既有）→ 文本/JSON
review: CLI → build_timeline()（共享层，MCP/Web 复用）→ filter_* → 文本/JSON
resume: CLI → 统一解析 → Module.resume(rollback_to)（目标解析 → 兼容性校验 → 续跑）
checkpoints: CLI → build_checkpoints()（共享层，MCP/Web 复用）→ 文本/JSON
snapshot: CLI → SqliteBackend.list_snapshots/load_snapshot（指定 tick）→ 摘要/JSON/导出文件
rollback: CLI → 同 resume 接线，仅目标必填（防误续最新）
checkpoint: CLI → load_snapshot(tick) → save_checkpoint(label)（纯数据操作，跨进程）
visualize: CLI → 统一解析 → tasklist（--tasklist 或 module_inputs 存档）→
        TasklistTranslator 重建 Graph → to_mermaid()（纯静态，零执行）
feed: CLI → RunFeedServer（ThreadingHTTPServer）→ 查询层组合 JSON（status/timeline/
        checkpoints）→ 页面原生 JS 轮询
install: CLI → validate_pack_dir（manifest + requires 校验，零 client）→ 复制进
        store/modules → 写 manifests/<name>.json（来源/版本/每文件 sha256/时间）
update: CLI → 按 manifest 来源重取 → check_updates 哈希比对 → 无差异直接替换 /
        有差异列清单交互确认（--yes 覆盖 / --keep 保留）
setup: CLI → input() 向导 → 写 store 级 .env + config.json（复用 scaffold 结构）
```

查询组合逻辑沉淀在 `module_harness/query.py` 与 `module_harness/store.py`
（CLI/MCP/Web 三形态共用），CLI 只 import 不重实现。

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

运行中 `Ctrl+C`：打印已执行 firing 数与提示，退出码 **2**。运行数据已落盘，可用 `status`/`review` 查询，并可用 `resume`（续最新）或 `rollback`（回退更早 tick）继续。

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
| `1` | 错误（模块未找到 / spec 缺失 / schema 违规 / 模板未找到 / LLM 未配置 / 参数互斥 / 无运行记录 / tasklist 校验失败 / 回退目标不存在 / 兼容性拒绝） |
| `2` | `Ctrl+C`（运行中中断）；argparse 参数错误（如 `rollback` 缺目标） |

错误信息走 stderr，统一 `SystemExit` + 非零退出码。

### 常见错误速查

| 场景 | 报错 | 处理 |
|------|------|------|
| 模块未找到 | `模块 '<名>' 未找到……` + 打印可用模块列表 | 检查 `--module` 名 |
| LLM 未配置 | `LLM 未配置 API key……` | 配 `config.json` + `.env`，或加 `--mock` |
| spec 违反 schema | `spec 校验失败: - 缺少字段 'raw_text'……` | 补全字段 |
| tasklist 校验失败 | `tasklist 校验失败: - ...` | 修复 tasklist |
| resume/rollback 无运行记录 | `无运行记录: <id>（先执行 specmodule run）` | 先执行 `run` |
| resume/rollback 目标不存在 | `回退目标 '99' 不存在（可用 tick: [...]；manual: …）` + checkpoints 引导 | `checkpoints` 看回退点，换有效目标 |
| resume/rollback 兼容性拒绝 | `Task 'A': inputs 引用 'Ghost' 不在新图中` | 修正 tasklist/模板，或回退到更早 tick |
| snapshot tick 不存在 | `快照 tick 99 不存在（可用: [1, 2]）` | 先 `checkpoints` 看可用 tick |

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

### M2 实践线：ppt_writer（spec → .pptx，双模板）

`ppt_writer` 把描述每页内容/布局的 spec 渲染成可机器校验的 .pptx（渲染零
LLM，`--mock` 可跑），并提供模板制作工作流（审查 → 入库 `reference/` +
manifest）。模板资产（`example/ppt_writer/reference/`：manifest.json +
入库的 .pptx 模板）随模块目录走，渲染器按**模块文件相对路径**定位，不依赖
启动目录。

```bash
# 免 key 冒烟（默认 3 页 spec，产出 ppt_writer_output.pptx）
python -m module_harness.cli run --module ppt_writer --mock \
  --modules-dir example/modules

# 指定 spec（output/theme/sections/pages；page > section 默认 > 模板兜底）
python -m module_harness.cli run --module ppt_writer --mock \
  --modules-dir example/modules --spec-file my_deck.json

# 模板制作工作流：草稿 → dump → 硬合规 → LLM 意见 → 合规入库
#（--mock 下意见节点为占位文本不阻断；不合规输出逐项问题清单、零写入）
python -m module_harness.cli run --module ppt_writer --template template_review \
  --mock --modules-dir example/modules \
  --spec '{"draft_pptx": "draft.pptx", "template_name": "content", "layout": 1, "kind": "content"}'

# 审阅 / 查询
python -m module_harness.cli review --run-id ppt_writer
python -m module_harness.cli status --run-id ppt_writer --json
```

spec 契约与占位符命名约定见 `example/ppt_writer/README.md`。

---

## 9. `resume` — 从中断处续跑

```
python -m module_harness.cli resume [<rollback>] --module <名> [选项]
```

从先前中断的运行（Ctrl+C / `--max-ticks` 截断）处继续执行，不重跑已完成部分。
接线镜像 `run`：同样的模块发现、spec 解析、模板/tasklist 二选一、三级 verbose
显示与结束汇总；最后一步调库侧 `Module.resume`（目标解析 → 新图重建 →
兼容性校验 → restore → 续跑）。

### 参数

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `<rollback>` | — | 最新 tick 快照 | 回退目标：tick 号（如 `1`）或 `manual:<label>` |
| `--module <名>` | 是 | — | 模块名（须与先前 run 一致） |
| `--modules-dir <dir>` | — | `modules/` | 模块目录 |
| `--spec '<JSON>'` | 见下 | — | 内联 JSON spec（重建未执行部分） |
| `--spec-file <path>` | 见下 | — | spec JSON 文件路径 |
| `--template <名>` | — | `entry.default_template` | 模板名 |
| `--tasklist <path>` | — | — | tasklist JSON 文件（跳过翻译，与 `--template` 互斥） |
| `--run-id <id>` | — | 模块名 | 运行目录名（须与先前 run 一致） |
| `--max-ticks <n>` | — | `100` | tick 上限；续跑后从回退 tick 起计的**绝对**上限 |
| `--verbose {1,2,3}` | — | `1` | 实时显示级别（同 `run`） |
| `--mock` | — | 关 | 免 key 假 LLM 冒烟（验证续跑接线） |

spec 解析优先级与 `run` 一致（`--spec` > `--spec-file` > `entry.default_spec`）。

### 回退目标语义

- **缺省：最新 tick 快照**——Ctrl+C 中断后一条命令即可从断点续跑。
- **tick 号**：快照编号 N 在 tick N-1 **结束后**落盘（如 flow 在 tick 0 执行
  A 后被截断，可用快照即为 tick 1，代表"A 已完成"的推进状态）。
- **`manual:<label>`**：从 API 层 `Module.checkpoint(label)` 创建的命名检查点恢复。
- **目标不存在**：报错并列出该运行可用 tick/manual 清单（退出码 1），并提示
  `specmodule checkpoints --run-id <id>` 查看回退点全景。
- **无运行记录**：`无运行记录: <id>（先执行 specmodule run）`（退出码 1）。

### 续跑语义

- 未执行部分按**当前**参数重建：可传新 `--spec`（只影响尚未执行的节点）；
  模板/tasklist 改动必须与已执行部分兼容——`check_resume_compat` 硬错误
  （如 inputs 引用图中不存在的节点）直接拒绝（退出码 1），**既有快照不被触碰**；
  非阻断的结构改动以警告提示（已执行节点修改不生效）。
- 续跑记录写入**同一** run.sqlite：`status` / `review` 可查询中断前后完整历史。

### 示例

```bash
# Ctrl+C 中断后，一条命令从断点续跑
python -m module_harness.cli resume --module academic_writer --mock

# 回到更早的 tick 重跑（如换 promptmode 前回退）
python -m module_harness.cli resume 3 --module academic_writer \
  --spec-file example/spec.academic_writer.json --verbose 2
```

### Ctrl+C

续跑中再次 Ctrl+C：已执行 firing 保留，退出码 **2**，数据落盘可查（同 `run`）。

---

## 10. `checkpoint` — 创建命名检查点

```
python -m module_harness.cli checkpoint <label> [<tick>] [--run-id <id>]
```

给指定 tick 的运行时快照起**人类标签**，存入 checkpoints 表（`manual:` 永久
保留，不随自动快照淘汰）。之后 `resume <label>` / `rollback <label>` 按名
回退——不用记 tick 数字。

**机制澄清**：checkpoint 不是写 tasklist 时声明的东西——tasklist 只描述
流水线结构；每 tick 自动快照由引擎（`_persist_tick`）零声明落盘，命名点
只是给已有快照的副本加标签。本命令是**纯数据操作、跨进程**：不需要运行中
的 runner（快照已落盘），与 checkpoints/snapshot/rollback 同族。

- 缺省 `<tick>` = 最新快照。
- `label` 自动补 `manual:` 前缀（库侧 `resume`/`rollback` 目标解析要求）；
  同名 label **覆盖**（打印提示）。
- 参数：`--run-id` 缺省 = 最近运行。

```
$ specmodule checkpoint before-prompt-change
已创建检查点 manual:before-prompt-change（tick 47）
回退: specmodule rollback manual:before-prompt-change --module <名>（或 resume manual:before-prompt-change）
```

典型用法：跑完一轮满意的中间状态 → 命名 → 继续调整 prompt 重跑 →
不满意时 `rollback manual:<label>` 一步回到命名点（数据保全已由自动快照
承担，命名点只是便于按名回退）。

---

## 11. `checkpoints` — 列出可用回退点

```
python -m module_harness.cli checkpoints [--run-id <id>] [--json]
```

`resume` / `rollback` 的目标清单：run.sqlite 中**全部**可用回退点 = 每 tick
轻量快照（snapshots 表，附带本 tick fired 节点轨迹）+ 手动检查点
（checkpoints 表，API 层 `Module.checkpoint(label)` 创建，永久保留）。

```
可用回退点 (run_id=hello):
  tick 1      fired: Greet
  tick 2      fired: Loop1
  manual:test  (tick 2)

回退: specmodule resume <目标> --module <名>（缺省续最新；rollback <目标> 须显式指定目标）
```

- `--run-id` 缺省 = 最近运行。
- `--json` 输出 `checkpoints_to_dict`（共享层，MCP/Web 复用）：

```json
{
  "module_id": "hello",
  "checkpoints": [
    {"target": "1", "tick": 1, "kind": "tick", "fired": ["Greet"], "label": null},
    {"target": "manual:test", "tick": 2, "kind": "manual", "fired": [], "label": "manual:test"}
  ]
}
```

`target` 字段即 `resume <目标>` / `rollback <目标>` 直传值。

**典型闭环**：`run` 中断 → `review --failed` 定位问题 tick → `checkpoints`
确认可回退点 → `rollback <tick>` 回到问题前微调重跑。

---

## 12. `snapshot` — 检视/导出快照

```
python -m module_harness.cli snapshot [<tick>] [--run-id <id>] [--json] [--out FILE]
```

检视指定 tick 的运行时快照；缺省 tick = 最新。

- **默认（文本摘要）**：tick / status / fireable / fired + 各节点最新输出
  （输出从 firings 表取——轻量快照剥离 records）。
- **`--json`**：stdout 打印**完整** runner 快照 JSON（即 `runner.restore()` 输入）。
- **`--out FILE`**：写完整快照 JSON 到文件——自包含（marking/run_state/
  fireable/fired），可 `restore` 到新 runner，是跨进程调试/存档素材。
  `--json` 与 `--out` 可并存（JSON 到 stdout + 写文件）。

```
快照 (run_id=hello):
  tick: 2
  status: idle
  各节点最新输出:
    Greet: {"greeting": "hello world"}
```

错误路径：无运行记录 / 指定 tick 不存在（附可用 tick 表）均退出码 1。

---

## 13. `rollback` — 回退到指定检查点重跑

```
python -m module_harness.cli rollback <目标> --module <名> [选项]
```

与 `resume` **同一次库调用**（`Module.resume`：目标解析 → 新图重建 →
兼容性校验 → restore → 续跑），接线与参数完全一致（见 §9 参数表）。
唯一差异：**`<目标>` 必填**——不会像 `resume` 那样缺省续最新，杜绝
"想回退却续了最新状态"的误操作。进入回退前用 `checkpoints` 选目标。

```bash
# 换 promptmode 前回退到 tick 3 重跑
python -m module_harness.cli rollback 3 --module academic_writer \
  --spec-file example/spec.academic_writer.json --verbose 2

# 回退到 API 创建的手动检查点
python -m module_harness.cli rollback manual:test --module academic_writer --mock
```

错误路径与 `resume` 一致（目标不存在列出可用清单、兼容性硬错误拒绝且不触碰
既有快照）。`rollback`/`resume`/`checkpoints` 组合即"快照-回退-续跑"完整闭环；
进程内调试 API（`Module.snapshot/restore/checkpoint/rollback_to`）仍由库侧提供。

---

## 14. `visualize` — 渲染 tasklist 对应图（mermaid）

```
python -m module_harness.cli visualize --module <名> [--tasklist <file> | --run-id <id>] [--out FILE] [--template <名>]
```

把 tasklist 的流水线结构渲染成 mermaid 文本——回答"这次 tasklist 长什么样"
（设计期/复盘用）。**纯静态**：只重建 Graph 并 `to_mermaid()`，零执行、不读
快照、不关心运行进度（图是声明式数据的投影，与执行历史无关）。

- **数据源**：`--tasklist <file>`（未运行的 tasklist 直接渲染，不依赖任何
  运行记录）优先；否则读 run.sqlite 的 `module_inputs` 存档。存档的
  run_id 缺省 = **模块同名运行目录**（`run` 的 `--run-id` 默认即模块名），
  不是"全局最近运行"——显式 `--run-id` 可指向其他运行（如自定义 run-id）。
- **registry**：由模块入口 `build_registry` 构建（graph 解析需校验已注册的
  guard/body）；llm_client 用 Mock 占位——**渲染不调用 LLM，免 key 可用**。
- `--out FILE` 写文件（缺省 stdout）；`--template` 决定 registry 构建
  （默认 `entry.default_template`）。
- **⚠️ `--template` 必须与 tasklist 匹配**：渲染的图来自存档/文件的
  tasklist，`--template` 只决定注册哪些 harness/script/guard。两者不一致时
  （如存档来自 detailed 模板、registry 按默认模板构建）会报"未注册元件"
  错误——此时错误信息会列出该模块全部可用模板，换对应 `--template` 即可；
  已注册元件恰为超集时可能静默渲染出与预期不符的图，请核对模板与存档来源。

输出示例（start 节点为 stadium 形状，guard 边标注）：

```
graph TD
    A(["A"])
    A --> B
    Loop -->|has_issues| Fix
    Fix --> Loop
```

错误路径：模块未找到（列出可用模块）、无运行记录且无 `--tasklist`，均退出码 1。

---

## 15. `init` — 生成模块开发脚手架

```
python -m module_harness.cli init <name> [--dir PATH] [--as-dir] [--force] [--description "..."]
```

两种形态（二选一，默认单文件）：

- **单文件**（默认）：`modules/<name>.py` python 原生骨架（harness/script/模板/registry/入口
  五区块）——代码密集模块通道。
- **目录形态**（`--as-dir`）：`modules/<name>/` pack 同构骨架（`module.json` + `scripts/` +
  `harnesses/` + `commands/` + `guards/` + `submodules/`）——与已装模块同构，简单写 module
  的默认入口，可直接 `publish` / `install`。

两种形态都补项目文件（幂等）。

### 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `<name>` | 是 | 模块名（合法 Python 标识符；同时是文件、`--module`、`entry.name`、默认 run_id——四处一致） |
| `--dir <path>` | — | 生成位置（默认 cwd） |
| `--as-dir` | — | 目录形态（pack 同构骨架；默认单文件） |
| `--force` | — | 覆盖已存在的模块文件/目录（仅模块文件） |
| `--description <str>` | — | 模块描述（展示用，不受标识符约束） |

### 生成布局

单文件形态：

```
<project>/
├─ modules/<name>.py    单文件模块骨架（harness/script/模板/registry/入口 五区块）
├─ config.json          provider/model 注册表（占位，真实运行前需填写）
├─ .env.example         API key 占位（复制为 .env 填密钥）
├─ .gitignore           （排除 .env / __pycache__ / .specmodule）
├─ spec.example.json    示例 spec
└─ README.md            用法 + config.json / .env 分工说明
```

目录形态（`--as-dir`）：

```
<project>/
├─ modules/<name>/
│  ├─ module.json       声明 spec_schema / tasklist（pack 格式，与已装模块同构）
│  ├─ scripts/greet.py  示例 script 组件
│  ├─ harnesses/        LLM 调用配置（JSON 文件，含示例 README）
│  ├─ commands/         shell 命令配置（JSON）
│  ├─ guards/           guard 函数（loop 条件）
│  └─ submodules/       嵌套子模块
├─ config.json / .env.example / .gitignore / spec.example.json / README.md
```

### 默认模板（立即冒烟）

- 单文件：默认模板 `hello` 为 **harness → script 流水线**——harness 节点读入 spec 的
  `message` 字段，script 节点消费其输出并回显。
- 目录形态：固定 `[Greet]` script 骨架（零 LLM 依赖，`--mock` 或真实运行皆可）。

```bash
# 免 key 冒烟（验证流水线接线，非内容质量）
python -m module_harness.cli run --module <name> --mock
```

### 冲突 / 幂等语义

- 模块名非法（含空格/连字符/中文等）：报错退出码 1，**零文件生成**。
- `modules/<name>.py` / `modules/<name>/` 已存在且未传 `--force`：报错退出码 1。
- 项目文件（config.json 等）已存在一律**跳过不覆盖**；`--force` 仅覆盖模块文件/目录。

### 配置分工

- `config.json`：非敏感注册表（providers 连接信息 + `api_key_env` 指向的**变量名**）。
- `.env`：密钥实际值（gitignored，不进版本库）。`config.json` 的 `api_key_env` 与 `.env`
  变量名必须对齐。

---

## 16. Store：家目录、配置链与模块管理

### 家目录（`~/.specmodule`，`SPECMODULE_HOME` 可覆盖）

```
~/.specmodule/
├─ modules/<name>/            pack 格式模块目录（唯一逻辑真相）
├─ manifests/<name>.json      {source, version, files:{rel→sha256}, installed_at}
├─ .env / config.json / rules.txt   用户级配置（回退层）
└─ cache/                     临时 clone/下载缓存，可清
```

无项目用户（`pip install specmodule` 后不建项目）的隐式项目根——模块有地方放、
API key 有地方配。搜索路径 = `cwd/modules` + `$SPECMODULE_PATH`（os.pathsep 分隔）
+ `store/modules` + pip entry points（`specmodule.modules` 组，附加来源）。

### 配置回退链

`os.environ`（最高，不覆盖已有键）→ 项目根 `.env`/`config.json`/`rules.txt` →
store 家目录同名文件。项目根没有配置时不再静默无 key。

### 管理命令

```
specmodule setup                     # 交互向导：provider/model/key → store 级 .env + config.json
specmodule install <本地 pack 目录|git URL>   # 校验（零 client）→ 复制进 store → 写 manifest
specmodule list [--json]             # 全部可用模块（同名多来源全量展示，含优先级）
specmodule info <name>               # 元数据 + 来源 + 安装时间
specmodule uninstall <name>          # 移除目录 + manifest
specmodule publish <name> --from <dir>   # 目录形态校验复制（同 install）；单文件形态经等价 SubModule 转化
specmodule update <name> [--yes|--keep]  # 按 manifest 来源重取 → 哈希比对 → 交互确认
```

- `install`/`publish` 校验失败零落盘（先 validate 后复制）；同名已存在报错不覆盖。
- **git URL 来源**（`http(s)://…` / `….git` / `git@…`）：`git clone --depth 1` 后校验——
  **仓库根必须是 pack 目录**（`module.json` 在根，子目录放模块不支持）；clone 工作树的 `.git`
  不复制进 store、不计入 manifest 哈希（`update` 脏检测不受版本库噪音干扰）。
- `update` 脏检测：本地改过的文件（与 manifest sha256 不同）列清单并交互确认，
  **绝不静默覆盖**；`--yes` 覆盖 / `--keep` 保留本地（非交互）。
- 已打包模块（packed/pip）可直接 `run`/`resume`/`rollback`/`visualize`（统一枚举解析）；
  同名冲突按搜索路径优先级，`list` 全量展示。

### 无项目用户完整闭环

```bash
pip install specmodule
specmodule setup                     # 配 provider/model/key（写 store 级配置）
specmodule install <模块 pack 目录或 git URL>
specmodule list
specmodule run --module <名> --spec '{"...": "..."}'
specmodule update <名>               # 作者更新后同步（脏检测）
specmodule uninstall <名>
```

---

## 17. `feed` — 零依赖运行 feed

```
python -m module_harness.cli feed [--host 127.0.0.1] [--port 8000] [--run-id <id>]
```

stdlib `http.server` 起的只读服务：浏览器打开 `http://127.0.0.1:8000/`（或带
`?run_id=<id>`）原生 JS 每 2s 轮询 `/feed.json`，展示运行状态阶段 / tick / 各节点
最新输出 / 时间线 / 检查点。运行中即可看（每 tick 落盘），运行后完整看。
零第三方依赖；富交互编辑器属生态项目 `SpecModule_webview`。

---

## 18. 范围 / 后续迭代

本子集**不含**（已记 roadmap，后续迭代）：

- 注册表 / 按名自动安装（npx 式 `run` 找不到就装）：依赖全局唯一名与策展，推迟到注册表时代。
- 运行时共享组件库（store 内组件跨模块加载期引用）：无第二消费者，YAGNI。
- 物理去重 / 内容寻址存储：sha256 仅作安装期元数据，不作路径。
- 可视化 / 编辑器本身：属生态项目（TUI/Web），本库只提供 `feed` 极简开关与枚举契约。
- zip/artifact URL 安装通道、模块版本矩阵。
- `init --with-source` / `--from-pip` 两模板：pyproject 落地后 `--from-pip` 即常态、
  `--with-source` 为框架开发用，大概率被吸收为流程说明而非新命令。

快照/回退闭环已完整：`checkpoint`（命名）→ `checkpoints`（列出）→
`snapshot`（检视/导出）→ `rollback`/`resume`（回退/续跑），外加 `visualize`
（mermaid 渲染）；进程内调试 API（`Module.snapshot/restore/checkpoint/
rollback_to`）为库侧能力，CLI 命令形态消费其持久化产物。