# module_harness/__init__.py
"""ModuleHarness — tickflow 上层抽象：harness 与 script 执行元件。"""

from .config import HarnessConfig
from .outputfmt import OutputFormat, OutputValidator
from .prompt import PromptRenderer
from .events import (
    EventBus,
    HarnessEvent,
    PromptRendered,
    LlmCallStarted,
    LlmToken,
    LlmCallCompleted,
    OutputValidated,
    HarnessFailed,
    ScriptEvent,
    ScriptStarted,
    ScriptCompleted,
    ScriptFailed,
    CommandStarted,
    CommandCompleted,
    CommandFailed,
)
from .command import Command, CommandConfig
from .harness import Harness
from .registry import HarnessRegistry
from .spec import (
    Spec,
    TaskDefinition,
    Tasklist,
    TranslationSpec,
    TasklistTemplate,
)
from .translator import TasklistValidator, TemplateLoader, Translator
from .graph_builder import TasklistTranslator
from .module import Module

__all__ = [
    # 配置
    "HarnessConfig",
    # 输出格式
    "OutputFormat",
    "OutputValidator",
    # Prompt
    "PromptRenderer",
    # 事件
    "EventBus",
    "HarnessEvent",
    "PromptRendered",
    "LlmCallStarted",
    "LlmToken",
    "LlmCallCompleted",
    "OutputValidated",
    "HarnessFailed",
    "ScriptEvent",
    "ScriptStarted",
    "ScriptCompleted",
    "ScriptFailed",
    # 核心
    "Harness",
    "HarnessRegistry",
    "Command",
    "CommandConfig",
    # Command 事件
    "CommandStarted",
    "CommandCompleted",
    "CommandFailed",
    # 数据模型
    "Spec",
    "TaskDefinition",
    "Tasklist",
    "TranslationSpec",
    "TasklistTemplate",
    # 翻译
    "TasklistValidator",
    "TemplateLoader",
    "Translator",
    # Graph 构建
    "TasklistTranslator",
    # 编排
    "Module",
]
