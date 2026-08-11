## Why

仓库定位此前是模糊的：AGENTS.md 的两级用户模型（开发者 vs 使用者）把"使用场景"
留成一句空话（"写 spec 和 tasklist"），roadmap 又把 CLI/MCP/Web 并进同一条"形态线"，
始终没回答"这个仓库是库还是产品"。这阻碍了干净打包、嵌入式复用与多形态消费——使用者
必然 `pip install specmodule`，但库的边界、CLI 归属、形态所在都未定。

## What Changes

- **仓库定位为库（framework）**，不是某一形态的产品。库是中性的——不承诺完整 UX。
- **CLI 留在库内**（参考 Django `django-admin`/`manage.py` 管理壳），随 `pip install
  specmodule` 分发：`specmodule run/status/review/init/snapshot/rollback/resume/visualize`。
  理由：CLI 零依赖（仅 argparse + 库本身）；`init` 脚手架生成的 `ModuleEntry/module.json/
  config.json` 是库契约，须与库同版本不 drift；使用者必然 pip install。
- **三个形态拆成独立生态项目**（各自仓库，各自 roadmap）：
  - `SpecModule_tui` — 富交互终端界面（面板/实时流/键盘导航/交互式回滚），消费库 query 层，引 TUI 框架依赖。
  - `SpecModule_mcp` — MCP/ACP 服务器，把 run/status/review/snapshot/resume 暴露为工具供 agent 用；薄层零逻辑。
  - `SpecModule_webview` — Web 可视化 + HTTP API（封装 query 层）；独立前端。
- **边界原则**：协议适配器（MCP 库 / FastAPI / TUI 框架）永远活在生态项目里；库只给
  "纯函数查询层 + 编排 helper + 薄 CLI"，保持依赖轻、可嵌入。
- **共享层**：`module_harness/query.py` 是 CLI/TUI/MCP/Web/嵌入方共同 import 的消费层。
- **打包**：库加 `pyproject.toml` + `[project.scripts] specmodule = module_harness.cli:main`；
  生态项目各自加自己的入口。
- **init 脚手架两模板**：`--with-source`（dev，框架源码入项目）/ `--from-pip`（consumer，库为依赖）。
- **嵌入式场景**：库编程 API（`Module/HarnessRegistry/SubModule/Translator`）可被别项目
  import 作 LLM 工具套件。

## Capabilities

### New Capabilities
- `specmodule-lib`: 库的定位与打包——CLI 随库分发、依赖轻、可嵌入、query 共享层、init 两模板。
- `specmodule-ecosystem`: 库与生态的边界——TUI/MCP/Web 为独立项目，协议适配器在生态，不污染库。

### Modified Capabilities
无（specs 目录当前为空，无既有 capability）。