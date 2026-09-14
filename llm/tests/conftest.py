# llm/tests/conftest.py
"""共享测试助手:假 SDK 注入(测试不依赖 anthropic/openai 真包)。"""

from __future__ import annotations

import sys
import types


# 依赖 pytest prepend 导入模式 + 本目录无 __init__.py；若未来加 __init__.py 需改包相对导入
def _install_fake_sdk(monkeypatch, module_name: str, class_name: str, capture: dict) -> None:
    """注入假 SDK 模块,Fake 客户端构造参数记录进 capture。"""
    fake = types.ModuleType(module_name)

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            capture.update(kwargs)

    setattr(fake, class_name, FakeAsyncClient)
    monkeypatch.setitem(sys.modules, module_name, fake)
