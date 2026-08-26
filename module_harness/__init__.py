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
    ConsistencyReviewed,
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
from .consistency import (
    ConsistencyError,
    ConsistencyReport,
    ConsistencyReviewer,
    REVIEW_HARNESS_CONFIG,
    register_review_harness,
)
from .align import ALIGN_CHECK_CONFIG, register_align_check_harness
from .builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from .translator import TasklistValidator, TemplateLoader, Translator
from .graph_builder import TasklistTranslator
from .module import Module
from .submodule import SubModule, SpecValidationError, script
from .loader import ModuleLoader, ModuleManifestError, ModuleRequirementError
from .spec import SpecSchema
from .status import ModuleStatus, query_run_status
from .entry import ModuleEntry, discover_modules
from .query import (
    CheckpointEntry,
    CheckpointList,
    QueryValueResult,
    ReviewEntry,
    ReviewTimeline,
    build_checkpoints,
    build_timeline,
    checkpoints_to_dict,
    create_checkpoint,
    filter_failed,
    filter_node,
    filter_tick,
    query_value,
    run_db_path,
    timeline_to_dict,
)
from .checkpoint import (
    ModuleInputStore,
    ResumeCheck,
    ResumeError,
    check_resume_compat,
)
from . import store as store_module
from .store import (
    ENTRY_POINT_GROUP,
    ModuleSource,
    apply_update,
    cache_dir,
    check_updates,
    file_sha256,
    install_pack,
    list_modules,
    load_manifest,
    manifests_dir,
    modules_dir,
    parse_dotenv,
    pip_entry_point_dirs,
    resolve_module,
    search_paths,
    store_home,
    uninstall_pack,
    validate_pack_dir,
)

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
    # 一致性审核
    "ConsistencyReviewed",
    "ConsistencyError",
    "ConsistencyReport",
    "ConsistencyReviewer",
    "REVIEW_HARNESS_CONFIG",
    "register_review_harness",
    # 对齐检查
    "ALIGN_CHECK_CONFIG",
    "register_align_check_harness",
    # 内置 harness（翻译/审核/对齐）：宿主需显式注册到自己的 registry
    "BUILTIN_HARNESS_NAMES",
    "register_builtin_harnesses",
    # 运行状态查询
    "ModuleStatus",
    "query_run_status",
    # submodule
    "SubModule",
    "script",
    "SpecValidationError",
    "SpecSchema",
    "ModuleLoader",
    "ModuleManifestError",
    "ModuleRequirementError",
    # 快照/回滚（roadmap #5）
    "ModuleInputStore",
    "ResumeCheck",
    "ResumeError",
    "check_resume_compat",
    # 模块入口（roadmap Phase 0：CLI 使用）
    "ModuleEntry",
    "discover_modules",
    # 共享查询层（roadmap Phase 0：CLI/MCP/Web 复用）
    "ReviewEntry",
    "ReviewTimeline",
    "QueryValueResult",
    "query_value",
    "build_timeline",
    "filter_failed",
    "filter_node",
    "filter_tick",
    "timeline_to_dict",
    "run_db_path",
    # 共享查询层（回退点列表：resume/rollback 目标清单）
    "CheckpointEntry",
    "CheckpointList",
    "build_checkpoints",
    "checkpoints_to_dict",
    "create_checkpoint",
    # store 共享层（module-user-store：家目录/枚举/安装管理）
    "store_home",
    "search_paths",
    "list_modules",
    "resolve_module",
    "ModuleSource",
    "ENTRY_POINT_GROUP",
    "pip_entry_point_dirs",
    "modules_dir",
    "manifests_dir",
    "cache_dir",
    "validate_pack_dir",
    "install_pack",
    "load_manifest",
    "uninstall_pack",
    "check_updates",
    "apply_update",
    "file_sha256",
    "parse_dotenv",
]
