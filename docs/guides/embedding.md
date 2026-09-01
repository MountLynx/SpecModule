# 嵌入指南：把 SpecModule 当库嵌入宿主项目

`pip install specmodule` 后在宿主项目（服务 / IDE 插件 / Web 后端）`import` 库面编程 API，把 SpecModule 当 LLM 工具套件嵌入。最小 demo 见 [`examples/embed_minimal/`](../../examples/embed_minimal/main.py)（含 `--mock` 免 key 冒烟，可直接运行验证）：

```bash
pip install specmodule
cd examples/embed_minimal && python main.py --mock
```

## 最小嵌入形态

```python
from module_harness import (
    EventBus, HarnessConfig, HarnessRegistry, Module,
    TemplateLoader, OutputFormat, register_builtin_harnesses,
)
from llm import LLMConfig, create_llm_client

client = create_llm_client(LLMConfig.from_env())   # .env / 环境变量（见 config-guide）
bus = EventBus()

reg = HarnessRegistry(llm_client=client, event_bus=bus)
register_builtin_harnesses(reg)                    # spec_to_tasklist 等内置集
reg.harness("translate", HarnessConfig(
    prompt_core="将以下文本翻译为中文：{text}",
    output_format=OutputFormat(type="json_object"),
))

loader = TemplateLoader(); loader.load_builtins()
module = Module(
    spec={"source_text": "Hello world", "style": "formal"},
    template_name="translate",
    llm_client=client, event_bus=bus, registry=reg, template_loader=loader,
    persist=False, status_file=False, keep_records=False,   # 嵌入方零落盘/零残留
)
await module.run()
```

## 嵌入要点

- **事件与 records 解耦**（`decouple-embed-events`）——宿主传 `event_bus` 即收 `OutputValidated` / `HarnessFailed` 等事件做反馈，不拖审计与落盘；不传则静默零开销。事件类型清单与订阅模式见 `module_harness/events.py`（类型化 dataclass 事件：harness 6 + script 3 + command 3 + 一致性审核 1）。
- **零残留可选**——`persist=False` + `status_file=False` + `keep_records=False` 时嵌入方磁盘上不留任何 `.specmodule/` 产物（全内存快速模式）。
- **内置 harness 显式注册**——翻译/审核/对齐 harness 不走隐式加载，宿主对 `HarnessRegistry` 调 `register_builtin_harnesses(reg)` 注册。
- **库面导入约定**——`module_harness` 顶层导出（`Module / HarnessRegistry / SubModule / query` 共享层……），导入勿触达内部子模块（`module_harness.cli` 等属 CLI 实现，非稳定库面）。

## task 级调用：一次函数调用 LLM 任务

嵌入者最小价值单位是**一次函数调用**，不是 run——单次结构化 LLM 任务（翻译 / 抽取 /
审核）不必建图：

```python
import asyncio
from module_harness import HarnessConfig, OutputFormat, call_harness
from llm import LLMConfig, create_llm_client

client = create_llm_client(LLMConfig.from_env())

result = asyncio.run(call_harness(
    HarnessConfig(
        prompt_core='从下列文本提取 JSON：{"translation": "..."}\n{text}',
        output_format=OutputFormat(type="json_object"),
    ),
    {"text": "Hello world"},
    llm_client=client,   # 显式必传：与 Module / HarnessRegistry 同一注入哲学
))
result.value   # 校验 + 自动提取后的解析值（json_object → dict）
result.raw     # LLM 原始输出（审计链）
result.usage   # token 用量
```

失败（LLM 错误 / 输出不合法）抛 `HarnessCallError`，携带 `failure / prompt / raw /
usage` 诊断链（异常即审计）。`promptmode` 传了但配置里没有该 key → `KeyError`
原样冒出（框架不猜）。

**保证边界**：task 层得到三层 prompt / 输出校验 / 事件流；得不到审计落盘 / 快照回滚 /
失败隔离 / 断点续跑——那些是 run 级保证，需要时往上爬一层建图（上文 Module 形态）。
红线：**task 级地板不许长成迷你引擎**——重试/落盘/条件分支属图，不在函数里重建。

## 嵌入者分层纪律

- **函数住 module_harness，调用方是应用层/模块层。** 宿主侧基础库（工具库、数据封装）
  想 import module_harness 即**分层警报**——该 LLM 调用应上移：由应用层 `call_harness`
  （基础库保持纯函数，数据进数据出），或在 module 里包成 script 节点（图编排）。
- **判定口诀：看 import 箭头**——高层 import 低层永远合法；箭头向上即警报。
- 完整论证见
  [`docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md`](../dev/superpowers/specs/2026-09-01-embedder-face-design.md) §2。

## 与 submodule 嵌入的区别

本文档的"嵌入"指**宿主项目 import 库面**；`SubModule` 的"嵌入模式"（`audit=False` 内存不留审计、`mode="fast"` 全内存）指**运行形态**——两者独立，见 [`concepts/SpecModule.md`](../concepts/SpecModule.md)「嵌入的两种含义」。
