# SpecModule 文档索引

按读者分型的文档地图。**本索引是正式文档清单**——新增/移动/删除文档必须同步更新；一致性核对按下列逐篇进行（含最后核对日期）。

## 引导层（guides/）—— 教程与操作指南

| 文档 | 范围 | 入口受众 | 最后核对 |
|------|------|---------|---------|
| [guides/tutorial-first-module.md](guides/tutorial-first-module.md) | 从零到第一个模块（入口/harness/script/tasklist/冒烟/调试/发布）；配套案例 `examples/tutorial/` | 写 module | 2026-08-24 |
| [guides/store-walkthrough.md](guides/store-walkthrough.md) | store 使用闭环：setup→install→run→update→uninstall + 发布 + 目录结构/manifest/脏检测 | 用 module（含发布者） | 2026-08-24 |
| [guides/config-guide.md](guides/config-guide.md) | 配置全貌：.env / config.json / rules.txt / LLMConfig / 回退链 / 常见问题 | 写与用两侧 | 2026-08-24 |
| [guides/embedding.md](guides/embedding.md) | 宿主项目 import 库面嵌入（embed_minimal demo + 嵌入要点） | 嵌入用户 | 2026-08-24 |

## 参考层（references/）—— 语法与命令面

| 文档 | 范围 | 入口受众 | 最后核对 |
|------|------|---------|---------|
| [references/cli-usage.md](references/cli-usage.md) | 18 个子命令参数表/示例/错误/退出码（748 行） | 用 module | 2026-08-22（repo-docs-tidy） |
| [references/spec-harness-syntax.md](references/spec-harness-syntax.md) | spec/tasklist/harness/template 声明语法 + 引用解析 + 错误矩阵 | 写 module | 2026-08-22（repo-docs-tidy） |
| [references/tickflow-integration.md](references/tickflow-integration.md) | tasklist 执行语义：Graph 映射/join/guard/edges 窗口/死锁/快照粒度 | 写 module | 2026-08-24 |

## 概念层（concepts/）

| 文档 | 范围 | 入口受众 | 最后核对 |
|------|------|---------|---------|
| [concepts/SpecModule.md](concepts/SpecModule.md) | 设计理念与概念澄清：双输入模式/模板通道/对齐检查/持久化/嵌入两义 | 理解框架 | 2026-08-24 |

## 内部（dev/）—— 不面向用户

| 文档 | 范围 | 说明 |
|------|------|------|
| [dev/README.md](dev/README.md) | 内部文档分类说明 | 维护者入口 |
| dev/progress/module-roadmap.md | 开发路线图 | 随开发更新 |
| dev/superpowers/ | 设计 spec（15 份）+ 实施计划（12 份） | 改敏感代码前按 AGENTS.md 阅读对应设计 |

## 维护契约

- **核对方法**：按本索引逐篇核对（对照实现），更新对应"最后核对"日期——单篇成本，无需全量翻阅。
- **变更义务**：任何正式文档的新增/移动/删除，必须同步更新本索引与所有指向旧路径的引用（README / AGENTS.md / 其他文档）。
- **读者分类**：`guides/` 面向任务（先读），`references/` 面向查询，`concepts/` 面向理解，`dev/` 仅维护者。
