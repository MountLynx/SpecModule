# SpecModule

An auditable, debuggable, fully-controlled framework for LLM usage.

Decompose LLM workflows into composable Petri-net nodes — each node is a
minimal unit of work: translation, review, shell command, Python function.
Nodes are connected via directed edges (with AND/OR joins, cycles), the
engine advances in synchronous steps, and all state is centrally recorded.
**A lightweight snapshot is persisted every tick**, making snapshots, pause,
and precise rewind (by tick number) cheap.

## Architecture

```
SpecModule/
├── tickflow/              # Petri-net workflow engine (standalone sub-project, synced from the upstream Graph repo)
│   ├── engine.py          #   Pure-function tick engine
│   ├── runner.py          #   Runner / AsyncRunner
│   ├── state.py           #   RunState — single source of truth
│   └── ...
├── llm/                   # LLM clients (Anthropic + OpenAI-compatible)
│   ├── client.py
│   └── config.py
├── module_harness/        # Module abstraction layer
│   ├── module.py          #   Module orchestrator (run/resume/snapshot/rollback)
│   ├── registry.py        #   HarnessRegistry (harness / script / command registration)
│   ├── harness.py         #   Harness class (LLM call node, three-layer prompt)
│   ├── command.py         #   Command node (shell subprocess)
│   ├── prompt.py          #   Three-layer prompt rendering
│   ├── outputfmt.py       #   Output format validation + auto-extraction
│   ├── spec.py            #   Spec, Tasklist, TasklistTemplate data models
│   ├── translator.py      #   spec → tasklist translation + validation + templates
│   ├── graph_builder.py   #   tasklist → tickflow Graph
│   ├── consistency.py     #   spec + tasklist consistency review
│   ├── align.py           #   Alignment-check harness
│   ├── checkpoint.py      #   Run-input archive + resume compatibility check
│   ├── status.py          #   Cross-process run status query
│   ├── submodule.py       #   Class-style module definition + pack/publish
│   ├── loader.py          #   Module loading + dependency validation
│   ├── builtins.py        #   Built-in harness set
│   ├── events.py          #   EventBus + typed events
│   ├── templates/         #   Built-in task templates
│   └── tests/             #   pytest suite (incl. real-LLM smoke)
└── docs/                  # Design docs, implementation plans, roadmap
```

## Quick Start

```python
from module_harness import Module, HarnessRegistry, HarnessConfig, EventBus
from module_harness import TemplateLoader, OutputFormat
from llm import create_llm_client, LLMConfig

# 1. Set up LLM client (LLM_PROVIDER / API keys from env or .env)
config = LLMConfig.from_env()
client = create_llm_client(config)
bus = EventBus()

# 2. Register harnesses and scripts
reg = HarnessRegistry(llm_client=client, event_bus=bus)

reg.harness("translate", HarnessConfig(
    prompt_core="Translate the following text to Chinese: {text}",
    output_format=OutputFormat(type="json_object"),
    notdo=["Do not add explanations"],
    temperature=0.3,
))

@reg.script("format_output")
def format_output(view):
    data = view.A.value
    return {"result": data["translation"].strip()}

# 3. Load built-in templates (spec-only → translation channel)
loader = TemplateLoader()
loader.load_builtins()

# 4. Run (persist=True persists a lightweight snapshot every tick)
module = Module(
    spec={"source_text": "Hello world", "style": "formal"},
    template_name="translate",
    llm_client=client,
    event_bus=bus,
    template_loader=loader,
)

firings = await module.run()
for f in firings:
    print(f"{f.node}: {f.output}")

# 5. Resume & rollback (cross-process)
await module.resume(rollback_to=3)          # precise rewind to after tick 3
module.list_checkpoints()                    # [(tick, fired node list, kind), ...]
```

## Core Concepts

### Three Node Types

| Type | Purpose | Registration |
|------|---------|-------------|
| **harness** | LLM call — three-layer prompt, output validation, streaming tokens | `reg.harness("name", config)` |
| **script** | Pure Python function — processing, computation, I/O | `@reg.script("name")` |
| **command** | Shell command — a single string becomes a node | `reg.command("name", CommandConfig(...))` |

### Spec & Tasklist

- **spec** — Structured key-value pairs describing *what you want*. No predefined schema; fields are defined by template authors.
- **tasklist** — `{Tasks: {A: {...}, B: {...}}, Flow: "A --> B"}`. Describes *how to do it*; each Task maps to a tickflow node.
- **Two input modes**: ① spec only (translated to tasklist via template) ② spec + tasklist (consistency check, then straight into graph builder).

### Snapshot & Rollback (roadmap #5)

- **Lightweight per-tick snapshot**: with persist=True the engine persists one snapshot per tick (audit records stripped; O(nodes+edges) constant size) — any tick can be rewound.
- **Precise tick-number rewind**: `resume(tick)` continues across processes, re-running only the unfinished part (executed node outputs are preserved); manual checkpoints `checkpoint("label")` / `rollback_to("label")` are kept permanently.
- **`list_checkpoints()`** returns `(tick, fired node list, kind)` — the tick↔node trace, a seed for history review.
- **In-process** `snapshot()` / `restore()` full snapshots for branching/rewind.

### Run Status Query (roadmap #7)

Cross-process query: `status.json` (phase machine: idle → translating → reviewing → ... → done) + latest snapshot in `run.sqlite` (tick-level: status/tick/fireable/fired + latest output per node). Queryable from any process, independent of a live Module instance.

```python
from module_harness import query_run_status
st = query_run_status("my_module")     # ModuleStatus: phase/tick/fired/outputs/node_states
```

### SubModule — class-style modules + pack/publish

```python
from module_harness import SubModule, script, SpecSchema

class Dig(SubModule):
    name = "dig"
    spec_schema = SpecSchema(input={"url": "str"})
    tasklist = Tasklist(tasks={...}, flow="...")

    @script("fetch")
    def fetch(view):
        return {"html": ...}
```

`SubModule` class-style declaration (with `spec_schema` input contract) → `pack()` exports a publishable manifest → `ModuleLoader` loads it (`requires` dependency validation). `mode = "fast"` runs with zero disk I/O.

### Consistency Review & Alignment Check

- **Consistency review** — the custom-tasklist channel runs a built-in review harness (`spec_tasklist_review`) by default, an LLM-based spec↔tasklist semantic check; failure raises `ConsistencyError`.
- **Alignment check** — built-in `align_check` node compares spec goals against outputs and reports alignment/deviations with suggestions.

### Event System

EventBus provides two layers — flow-level (tickflow hooks: `on_fire`, `on_tick_end`) and intra-node events (EventBus: prompt rendering, token streaming, command execution, validation results). Consumers subscribe as needed.

### Namespace Isolation

Multiple Modules can coexist in the same process. Bodies are registered under `{module_id}:{key}` prefixes to prevent collisions.

## Current Status

**18 core features implemented** (framework-capability stage). Now entering stage two: the user-facing layer — data-exposure SDK → CLI + history review → agent interface → web visualization, driven by real-world modules as acceptance cases. See [module-roadmap.md](docs/progress/module-roadmap.md) for the full roadmap.

## Design Principles

- **Zero tickflow modification** — All module-layer features extend through `Registry` subclassing
- **Full control** — No implicit behavior; a wrong promptmode raises KeyError; the framework does not paper over design mistakes
- **Audit by design** — All state is recorded in RunState; snapshot and rewind are built-in capabilities
- **SDK first** — Design the data-query interface before implementing any new feature; consumer surfaces (CLI/agent/Web) are thin wrappers over the SDK
- **YAGNI** — Every feature has a concrete use case before it is added

## License

MIT
