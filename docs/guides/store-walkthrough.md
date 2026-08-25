# Store 使用闭环：setup → install → run → update → uninstall

端到端操作指南，覆盖模块的**获取、运行、更新、卸载**（用 module 侧）与**发布**（写 module 侧）。命令级细节（参数、错误、退出码）见 [`references/cli-usage.md`](../references/cli-usage.md)，此处只讲路径。

```
安装/配置 ──► 获取 ──► 运行 ──► 更新 ──► 卸载
setup        install    run        update     uninstall
              │
              │ publish（写 module 侧：本地目录 → store）
              ▼
           list / info（查看）
```

## 0. 前置：安装与配置

```bash
pip install specmodule
specmodule setup        # 交互向导：provider/model/key → 写 store 级 .env + config.json
```

`setup` 一次性配置后全局可用（写 `~/.specmodule/` 用户级配置）。配置回退链与手动配置方法见 [`guides/config-guide.md`](config-guide.md)。无 key 时 `run --mock` 可冒烟验证。

## 1. 获取模块（install）

```bash
specmodule install <本地 pack 目录 | git URL>
```

- **本地 pack 目录**：目录根有 `module.json`（见第 5 节"目录结构"）。
- **git URL**（`http(s)://…` / `….git` / `git@…`）：`git clone --depth 1` 后校验复制；**仓库根必须是 pack 目录**（子目录放模块不支持）。
- 校验失败**零落盘**（先 validate 后复制）；同名已存在报错不覆盖。

装完可查：

```bash
specmodule list            # 全部可用模块（同名多来源全量展示，含优先级）
specmodule info <name>     # 元数据 + 来源 + 安装时间
```

搜索路径（`run`/`list` 按此解析，前面优先）：`cwd/modules` → `$SPECMODULE_PATH`（os.pathsep 分隔）→ `store/modules` → pip entry points（`specmodule.modules` 组）。

## 2. 运行（run）

```bash
specmodule run --module <name> --spec '{"text": "……"}' [--mock] [--verbose 2]
```

- **spec 解析优先级**：`--spec`（内联 JSON）> `--spec-file`（文件）> 模块声明的 `default_spec` > 报错。
- **流程二选一**：`--template <名>`（spec → 模板翻译为 tasklist）或 `--tasklist <path>`（直写 tasklist，与 `--template` 互斥）。
- **`--mock`**：免 key 假 LLM，验证流水线形状。
- 运行落盘 `.specmodule/runs/<run_id>/`，可用 `status`/`review`/`checkpoints`/`rollback` 审阅（见教程第 6 步）。

## 3. 更新（update）

```bash
specmodule update <name>            # 按 manifest 来源重取 → 哈希比对 → 交互确认
specmodule update <name> --yes      # 覆盖本地改动（非交互）
specmodule update <name> --keep     # 保留本地改动（非交互）
```

**脏检测机制**：安装清单 `~/.specmodule/manifests/<name>.json` 记录每个文件的 sha256。`update` 重取来源后逐文件比对——**本地改过的文件（哈希与清单不符）列清单交互确认，绝不静默覆盖**。git 来源的 clone 工作树 `.git` 不复制进 store、不计入哈希（版本库噪音不干扰检测）。

## 4. 卸载（uninstall）

```bash
specmodule uninstall <name>     # 移除 store/modules/<name>/ 目录 + manifests/<name>.json
```

## 5. 发布（写 module 侧）

开发侧两条路径：

```bash
# 路径 A：模块入口（entry）已就绪——publish 校验复制到 store
specmodule publish <name> --from <模块目录> --modules-dir <模块所在目录>

# 路径 B：已有 pack 目录——install 等同（校验复制）
specmodule install <pack 目录>
```

发布产物是 **pack 格式模块目录**（唯一逻辑真相）：

```
<name>/
├── module.json          # 清单：name/version/description/spec_schema/
│                        #   requires/modules/tasklist（Tasks+Flow）
├── harnesses/           # harness 配置（按注册名）
├── scripts/             # script 函数源码（@script 装饰器行 + 函数体 + 必要 import）
├── commands/            # command 配置
├── guards/              # guard 函数（边条件）
└── submodules/          # 嵌套子模块（每个是完整子包，递归 module.json）
```

`module.json` 必填字段：`name`、`tasklist`；可选：`version`/`description`/`spec_schema`/`requires`/`modules`。`requires` 声明依赖（加载时校验）。**加载目录 = 可信代码**（scripts/guards 经 exec 加载），只安装可信来源。

单文件入口形态（`modules/<name>.py`）经 `publish` 自动转化为等价 SubModule 再打包——写 module 侧先读 [`guides/tutorial-first-module.md`](tutorial-first-module.md) 第 7 步。

## 6. 无项目用户完整闭环（最小路径）

```bash
pip install specmodule
specmodule setup                       # 配 provider/model/key
specmodule install <模块 pack 目录或 git URL>
specmodule list
specmodule run --module <名> --spec '{"...": "..."}'
specmodule update <名>                 # 作者更新后同步（脏检测）
specmodule uninstall <名>
```

## 7. 目录结构速查

```
~/.specmodule/                 # store 家目录（SPECMODULE_HOME 可覆盖）
├── modules/<name>/            # pack 格式模块目录（唯一逻辑真相）
├── manifests/<name>.json      # 安装清单 {source, version, files:{rel→sha256}, installed_at}
├── .env / config.json / rules.txt   # 用户级配置（回退层，见 config-guide）
└── cache/                     # 临时 clone/下载缓存，可清
```

故障排查（报错/退出码/常见错误）见 [`references/cli-usage.md`](../references/cli-usage.md) 第 6 节；配置问题见 [`guides/config-guide.md`](config-guide.md)。
