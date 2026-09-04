# module_harness 子包化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 把 `module_harness/` 平铺的 28 个模块按职责收进 `core/ model/ orchestrate/ infra/ cli/` 五个子包，顶层 `module_harness/__init__.py` 公共 API（`__all__`）保持逐名不变。

**Architecture:** 纯机械搬移 + import 改写，零行为变化。依赖图已核实无文件级 import 环（`query`/`store` 对 cli 组的引用全部是函数内懒导入，搬移后保持懒导入）。验证基线 = 重构前全量 pytest 绿，重构后同绿。

**Tech Stack:** Python 3.13 / setuptools / pytest。git mv 保留历史。

---

## 归属映射（28 个模块 + 4 个新增 `__init__.py`）

| 子包 | 模块（自 module_harness/ 平铺迁入） |
|------|-----------------------------------|
| `core/` | config, outputfmt, prompt, harness, registry, builtins, call |
| `model/` | spec, translator, module, submodule |
| `orchestrate/` | graph_builder, align, consistency, feed |
| `infra/` | events, stream, control, checkpoint, query, status, store |
| `cli/` | cli, entry, scaffold, command, loader（+ 新增 `__main__.py`） |

用户清单未列的 4 个文件按内容归属：`builtins.py`/`call.py` → core（内置 harness 注册 / task 级 API 地板）；`control.py`/`stream.py` → infra（跨进程控制文件协议 / stream.log 落盘，与 status.json 同族）。`templates/` 原地不动（`package-data` 路径依赖它）。`tests/` 原地不动。

### 已知跨组依赖（核实过，无环）

- core → infra：`harness/registry/call` 顶层 import `events`（EventBus 在 infra）
- core → cli：`registry` import `command`（Command 节点类，用户指定放 cli/）
- core → orchestrate：`builtins` import `align/consistency`（注册内置 harness）
- infra → orchestrate：`checkpoint` import `graph_builder._is_constant_ref`；orchestrate → infra：`feed` import `query/status`
- model → core/orchestrate/infra：`module` 是编排汇聚点，import 全部组
- infra/cli → cli/model（仅函数内懒导入）：`query`/`store` 懒 import `loader/entry/submodule`

### 对外兼容面（不改动的契约）

- `from module_harness import X`：`__init__.py` 重写 import 路径，`__all__` 逐名不变
- `from module_harness import store` / `import submodule as sm_module`：`__init__.py` 显式绑定模块对象
- `from module_harness.cli import main` 与 pyproject console script `module_harness.cli:main`：`cli/__init__.py` 再导出 `main`
- `python -m module_harness.cli`：新增 `cli/__main__.py`
- `[tool.setuptools]` 无需改动（`include = ["module_harness*"]` 通配子包；`package-data` 的 `templates/builtin/*.json` 路径不变）

### 已知必须手修的一处

`model/translator.py:195` `Path(__file__).parent / "templates" / "builtin"` → `Path(__file__).parent.parent / ...`（文件下移一层后定位回包根）。

---

### Task 0: 基线

- [x] `python -m pytest module_harness/tests/ -q` 全绿（当前 HEAD 干净）。记录通过数。

### Task 1: 搬移文件 + 建子包

- [x] `git mv` 按归属映射搬 26 个文件（cli.py 最后，因其目标目录名与自身同名）；`mkdir core model orchestrate infra cli`
- [x] 每个子包写 `__init__.py`：一行 docstring（中文，声明该组职责），无 re-export（公共面只在顶层 `__init__.py`）

### Task 2: 包内相对 import 改写

- [x] 脚本改写 26 个迁移文件中的 `from .mod import` / `from . import mod`：同组不动；跨组 `from .mod import` → `from ..<组>.mod import`
- [x] 手修 `model/translator.py` 模板路径（见上）

### Task 3: 顶层 `__init__.py` + cli 兼容面

- [x] 重写 `module_harness/__init__.py` 全部 import 到子包路径；`from . import store as store_module` → `from .infra import store`；补 `from .model import submodule`；`__all__` 逐字不动
- [x] `cli/__init__.py`：`from module_harness.cli.cli import main` 再导出（`__all__ = ["main"]`）
- [x] `cli/__main__.py`：`python -m module_harness.cli` 入口

### Task 4: 测试/示例深层 import 改写

- [x] 脚本批量改写 `module_harness/tests/`、`example/`、`examples/` 中 `module_harness.<mod>` → `module_harness.<组>.<mod>`（26 条规则；`module_harness.cli`/`module_harness.tests` 例外不动）
- [x] `example/test_ppt_render.py`、`example/test_ppt_workflow.py` 的 `from module_harness.cli import MockLLMClient` → `from llm.mock import MockLLMClient`（正源导入，cli 包不再借道）

### Task 5: 回归

- [x] `python -m pytest module_harness/tests/ -q` 与基线同绿
- [x] `python -m module_harness.cli --help`、`specmodule --help`（console script）可跑

### Task 6: 活文档更新（历史 plans/specs 不动）

- [x] `AGENTS.md`：Project Layout 树 + 规则 6 的 `module_harness/query.py` 路径
- [x] `docs/dev/progress/module-roadmap.md`：3 处 `module_harness/query.py|store.py`
- [x] `docs/guides/embedding.md`、`docs/concepts/SpecModule.md`：`module_harness/events.py` 路径
- [x] `docs/references/api.md`：章节标题里 `module_harness.query|status|control|store|entry` 模块路径
- [x] `docs/references/cli-usage.md:310`：`from module_harness.entry import` → `module_harness.cli.entry`

### Task 7: 提交

- [x] 单 commit：`refactor: module_harness 子包化（core/model/orchestrate/infra/cli），公共 API 与 __all__ 不变`
