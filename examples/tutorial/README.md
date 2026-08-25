# examples/tutorial — 从零到第一个模块（教程配套案例）

配套文档：[docs/guides/tutorial-first-module.md](../../docs/guides/tutorial-first-module.md)。

本目录是教程的**每一步真实产物**：

| 路径 | 内容 | 教程对应步骤 |
|------|------|-------------|
| `modules/summarizer.py` | 模块入口（entry + registry 构建 + 模板） | 第 2 步产物 |
| `tasklist.json` | 手写 tasklist（`{Tasks, Flow}`，2 节点） | 第 3 步产物 |
| `spec.json` | 输入 spec（default_spec 兜底时可省略） | 第 1 步构思 |

## 运行（仓库源码环境）

```bash
# 免 key 冒烟（假 LLM，验证流水线形状）——直写 tasklist 通道
python -m module_harness.cli run --module summarizer \
  --modules-dir examples/tutorial/modules --tasklist examples/tutorial/tasklist.json --mock

# 免 key 冒烟——模板通道（script 翻译器，与直写同一条流水线）
python -m module_harness.cli run --module summarizer \
  --modules-dir examples/tutorial/modules --mock

# 真实 LLM（项目根 .env 配置 provider/key 后）
python -m module_harness.cli run --module summarizer \
  --modules-dir examples/tutorial/modules --mock --verbose 2

# 审阅与回退
python -m module_harness.cli review --run-id summarizer
python -m module_harness.cli checkpoints --run-id summarizer
```

`--modules-dir` 指向本目录 `modules/`；发布到 store 后即可省略（见 store-walkthrough）。
