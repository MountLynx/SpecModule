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

## 与 submodule 嵌入的区别

本文档的"嵌入"指**宿主项目 import 库面**；`SubModule` 的"嵌入模式"（`audit=False` 内存不留审计、`mode="fast"` 全内存）指**运行形态**——两者独立，见 [`concepts/SpecModule.md`](../concepts/SpecModule.md)「嵌入的两种含义」。
