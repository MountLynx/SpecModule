"""SpecModule 嵌入式最小 demo —— 证明「pip install specmodule 后库面干净可嵌入」。

本文件刻意只做一件事：以宿主项目视角 import 库面编程 API
（``Module / HarnessRegistry / HarnessConfig / EventBus / TemplateLoader /
OutputFormat``），注册 harness + script，跑通一个真实 workflow，并展示
宿主选择性订阅事件（``decouple-embed-events``：事件投递与 keep_records
解耦——宿主传 ``event_bus`` 即收事件，不拖审计与落盘）。

运行（在 venv / 已安装 specmodule 的环境，且 cwd 为本目录）::

    python main.py --mock      # 免 key 假 LLM 冒烟（演示数据流）
    python main.py             # 真实 LLM（.env / 环境变量配置）

验证点：
    - import 只触达库面（无任何框架内部符号）
    - 全部异步，宿主自持 asyncio 事件循环
    - 事件订阅不依赖 audit/records（keep_records=False 时事件照常可达）
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock

from llm import LLMConfig, create_llm_client
from llm.client import LLMResponse

from module_harness import (
    EventBus,
    HarnessConfig,
    HarnessRegistry,
    HarnessFailed,
    Module,
    OutputFormat,
    OutputValidated,
    TemplateLoader,
    register_builtin_harnesses,
)


def _mock_client() -> MagicMock:
    """免 key 假客户端：--mock 冒烟用（演示数据流形状，不经网络）。"""

    async def complete(prompt: str | None = None, **kwargs) -> LLMResponse:
        p = prompt or ""
        if "tasklist JSON" in p:  # spec_to_tasklist 翻译通道 → 返回合法 tasklist
            content = json.dumps({
                "Tasks": {
                    "A": {
                        "type": "harness", "harness": "translate",
                        "promptmode": "formal", "inputs": {"text": "{spec.source_text}"},
                        "outputformat": {"type": "json_object"},
                    },
                    "B": {
                        "type": "script", "script": "format_output",
                        "inputs": {"data": "A"},
                    },
                },
                "Flow": "A --> B",
            })
        else:  # translate harness 节点
            content = '{"translation": "你好，世界！"}'
        return LLMResponse(content=content, usage={}, finish_reason="end_turn")

    client = MagicMock()
    client.complete = AsyncMock(side_effect=complete)
    return client


async def main() -> int:
    # 1. 宿主选择性订阅：只关心「输出校验」与「harness 失败」
    bus = EventBus()
    validated: list[str] = []

    def on_validated(event) -> None:
        validated.append(f"tick {event.tick} {event.node} passed={event.passed}")

    bus.subscribe(OutputValidated, on_validated)
    bus.subscribe(
        HarnessFailed,
        lambda e: print(f"  [宿主] HarnessFailed: {e.reason}"),
    )

    # 2. 客户端：--mock 用假客户端，否则 LLMConfig.from_env()（.env 自动加载）
    client = _mock_client() if "--mock" in sys.argv else create_llm_client(LLMConfig.from_env())

    # 3. 注册 harness + script（宿主自己的注册表）
    reg = HarnessRegistry(llm_client=client, event_bus=bus)
    register_builtin_harnesses(reg)  # spec_to_tasklist 翻译 harness（内置集）
    reg.harness("translate", HarnessConfig(
        prompt_core="将以下文本翻译为中文：{text}",
        prompt_modes={"formal": "正式", "casual": "随意"},
        output_format=OutputFormat(type="json_object"),
        notdo=["不要添加解释"],
        temperature=0.3,
    ))

    @reg.script("format_output")
    def format_output(view):
        return {"result": view.A.value["translation"].strip()}

    # 4. 内置模板（spec only → 翻译通道）+ 运行
    loader = TemplateLoader()
    loader.load_builtins()

    module = Module(
        spec={"source_text": "Hello world", "style": "formal"},
        template_name="translate",
        llm_client=client,
        event_bus=bus,
        registry=reg,
        template_loader=loader,
        module_id="embed_minimal_demo",
        keep_records=False,   # 事件照常可达（与 records 解耦）
        persist=False,        # 嵌入方零落盘
        status_file=False,    # 嵌入方零残留
    )
    firings = await module.run()

    # 5. 结果与事件
    print("=== firings ===")
    for f in firings:
        print(f"  {f.node}: {f.output}")
    print("=== 宿主收到的事件 (keep_records=False) ===")
    for line in validated:
        print(f"  [宿主] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))