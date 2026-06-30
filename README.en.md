# SpecModule

An auditable, debuggable, fully-controlled framework for LLM usage.

Decompose LLM workflows into composable Petri-net nodes — each node is a
minimal unit of work: translation, review, shell command, Python function.
Nodes are connected via directed edges (with AND/OR joins, cycles), the
engine advances in synchronous steps, and all state is centrally recorded.
Snapshots, pause, and rewind are cheap at single-tick granularity.

## Architecture

```
SpecModule/
├── tickflow/              # Petri-net workflow engine (standalone sub-project)
│   ├── engine.py          #   Pure-function tick engine
│   ├── runner.py          #   Runner / AsyncRunner
│   ├── state.py           #   RunState — single source of truth
│   └── ...
├── llm/                   # LLM clients (Anthropic + OpenAI-compatible)
│   ├── client.py
│   └── config.py
├── module_harness/        # Module abstraction layer
│   ├── events.py          #   EventBus + typed events
│   ├── config.py          #   HarnessConfig
│   ├── harness.py         #   Harness class (LLM call node)
│   ├── registry.py        #   HarnessRegistry (harness / script / command registration)
│   ├── command.py         #   Command node (shell subprocess)
│   ├── prompt.py          #   Three-layer prompt rendering
│   ├── outputfmt.py       #   Output format validation + auto-extraction
│   ├── spec.py            #   Spec, Tasklist, TasklistTemplate data models
│   ├── translator.py      #   spec → tasklist translation + validation + templates
│   ├── graph_builder.py   #   tasklist → tickflow Graph
│   ├── module.py          #   Module orchestrator
│   └── templates/         #   Built-in task templates
└── docs/                  # Design docs, specs, roadmap
```

## Quick Start

```python
from module_harness import Module, HarnessRegistry, HarnessConfig, EventBus
from module_harness import TemplateLoader, OutputFormat
from llm import create_llm_client, LLMConfig

# 1. Set up LLM client
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

reg.harness("spec_to_tasklist", HarnessConfig(
    prompt_core="You are a workflow designer. Generate a tasklist JSON from the spec.",
))

@reg.script("format_output")
def format_output(view):
    data = view.A.value
    return {"result": data["translation"].strip()}

# 3. Load template
loader = TemplateLoader()
loader.load_builtins()

# 4. Run
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

### Event System

EventBus provides two layers — flow-level (tickflow hooks: `on_fire`, `on_tick_end`) and intra-node events (EventBus: prompt rendering, token streaming, command execution, validation results). Consumers subscribe as needed.

### Namespace Isolation

Multiple Modules can coexist in the same process. Bodies are registered under `{module_id}:{key}` prefixes to prevent collisions.

## Current Status

12 core features implemented. See [module-roadmap.md](docs/superpowers/progress/module-roadmap.md) for full progress and roadmap.

## Design Principles

- **Zero tickflow modification** — All module-layer features extend through `Registry` subclassing
- **Full control** — No implicit behavior; a wrong promptmode raises KeyError; the framework does not paper over design mistakes
- **Audit by design** — All state is recorded in RunState; snapshot and rewind are built-in capabilities
- **YAGNI** — Every feature has a concrete use case before it is added

## License

MIT
