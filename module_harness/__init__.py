# module_harness/__init__.py
"""ModuleHarness — tickflow 上层抽象：harness 与 script 执行元件。"""

# core：核心执行元件
from .core.config import HarnessConfig
from .core.outputfmt import OutputFormat, OutputValidator
from .core.prompt import PromptRenderer
from .core.harness import Harness
from .core.call import HarnessCallError, HarnessCallResult, call_harness
from .core.registry import HarnessRegistry
from .core.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
# model：数据模型与模块形态
from .model.spec import (
    Spec,
    TaskDefinition,
    Tasklist,
    TranslationSpec,
    TasklistTemplate,
)
from .model.spec import SpecSchema
from .model.translator import TasklistValidator, TemplateLoader, Translator
from .model.module import Module
from .model.submodule import SubModule, SpecValidationError, script
# orchestrate：图编排
from .orchestrate.consistency import (
    ConsistencyError,
    ConsistencyReport,
    ConsistencyReviewer,
    REVIEW_HARNESS_CONFIG,
    register_review_harness,
)
from .orchestrate.align import ALIGN_CHECK_CONFIG, register_align_check_harness
from .orchestrate.graph_builder import TasklistTranslator
# infra：运行基础设施
from .infra.events import (
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
from .infra.status import ModuleStatus, query_run_status
from .infra.query import (
    CheckpointEntry,
    CheckpointList,
    QueryValueResult,
    ReviewEntry,
    ReviewTimeline,
    build_checkpoints,
    build_timeline,
    check_resume_compat_from_run,
    checkpoints_to_dict,
    create_checkpoint,
    delete_run,
    filter_failed,
    filter_node,
    filter_tick,
    list_runs,
    load_snapshot_summary,
    query_value,
    run_db_path,
    timeline_to_dict,
)
from .infra.checkpoint import (
    ModuleInputStore,
    ResumeCheck,
    ResumeError,
    check_resume_compat,
)
from .infra.store import (
    ENTRY_POINT_GROUP,
    ModuleSource,
    ResolvedModule,
    apply_update,
    cache_dir,
    check_updates,
    detail_to_dict,
    file_sha256,
    install_pack,
    list_modules,
    load_manifest,
    manifests_dir,
    modules_dir,
    parse_dotenv,
    pip_entry_point_dirs,
    resolve_module,
    resolve_module_full,
    search_paths,
    store_home,
    uninstall_pack,
    validate_pack_dir,
)
# cli 层（CLI 实现，非库面）
from .cli.command import Command, CommandConfig
from .cli.entry import ModuleEntry, discover_modules
from .cli.loader import ModuleLoader, ModuleManifestError, ModuleRequirementError

# 模块对象绑定（`from module_harness import store / submodule / query` 可用；
# query 共享层是 embedding.md 明示的顶层导出面）
from .infra import store
from .infra import query
from .model import submodule

# 嵌入者最小面（用法见 docs/guides/embedding.md）：
#   task 级 = call_harness / HarnessCallResult / HarnessCallError
#   图级   = Module / HarnessRegistry + HarnessConfig / OutputFormat / EventBus
#             + TemplateLoader + register_builtin_harnesses
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
    # task 级 API 地板（嵌入者消费面，docs/dev/superpowers/specs/2026-09-01-embedder-face-design.md）
    "HarnessCallResult",
    "HarnessCallError",
    "call_harness",
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
    "check_resume_compat_from_run",
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
    "load_snapshot_summary",
    # 共享查询层（run 历史枚举与删除）
    "list_runs",
    "delete_run",
    # store 共享层（module-user-store：家目录/枚举/安装管理）
    "store_home",
    "search_paths",
    "list_modules",
    "resolve_module",
    "resolve_module_full",
    "ResolvedModule",
    "detail_to_dict",
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
