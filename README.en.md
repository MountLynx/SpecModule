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
├── tickflow                # Petri-net workflow engine (external pip dependency tickflow-py, import name tickflow)
├── llm/                    # LLM clients (Anthropic + OpenAI-compatible)
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
│   ├── entry.py           #   ModuleEntry contract + directory discovery
│   ├── scaffold.py        #   init scaffolding (single-file + --as-dir directory form)
│   ├── store.py           #   Store shared layer (home/search paths/enumeration/install mgmt)
│   ├── feed.py            #   Zero-dep run feed (http.server; CLI feed command)
│   ├── query.py           #   Shared query layer (timeline/checkpoints; CLI/MCP/Web reuse)
│   ├── cli.py             #   specmodule CLI (18 subcommands, argparse, zero deps)
│   ├── templates/         #   Built-in task templates
│   └── tests/             #   pytest suite (incl. real-LLM smoke)
└── docs/                  # Design docs, implementation plans, roadmap
```

## Install Dependencies

```bash
# Library: pip install (pyproject.toml + console script `specmodule`)
pip install specmodule

# Development (this repo): source + test deps
pip install -r requirements.txt
```

| Package | Purpose | Required |
|---------|---------|----------|
| **`specmodule`** | The library itself (PyPI name; `pyproject.toml` packages `llm` + `module_harness` + `specmodule` CLI) | ✅ Yes |
| **`tickflow-py`** | Petri-net workflow engine. ⚠️ The PyPI package is named `tickflow-py`, but the **import name remains `tickflow`** (`import tickflow`, not `import tickflow_py`). Upstream: https://github.com/MountLynx/tickflow- | ✅ Yes |
| `anthropic` | Claude backend (when `provider=anthropic`) | Depends on provider |
| `openai` | OpenAI & compatible backends (when `provider=openai` / `openai-compatible`) | Depends on provider |
| `jsonschema` | Schema validation for `json_schema` output format (skips validation, JSON-only check if not installed) | Recommended |
| `pytest` | Test suite (`python -m pytest module_harness/tests/ -q`) | Dev only |

The CLI ships with the package (`specmodule run/status/review/...`, 18 subcommands, see `docs/cli-usage.md`); non-module users follow the store loop: `specmodule setup` (configure key) → `install` (install module) → `list`/`run` → `update`/`uninstall`.

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

# 6. Package & publish: class-style definition → pack → load & run
from module_harness import SubModule, SpecSchema, TaskDefinition, ModuleLoader, script

class Translator(SubModule):
    """Translation module with style selection (class-style + spec_schema contract)."""
    name = "my_translator"
    version = "1.0.0"
    description = "Translation module with style selection"
    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )
    harnesses = [HarnessConfig(
        name="translate",
        prompt_core="Translate: {text}",
        prompt_modes={"formal": "Formal style", "casual": "Casual style"},
        output_format=OutputFormat(type="json_object"),
    )]
    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                promptmode="{spec.style}",          # spec field drives promptmode
                inputs={"text": "{spec.source_text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_output", inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )

    @script("format_output")
    def format_output(view):
        return {"translation": view.A.value["translation"].strip()}

# Run directly (spec validated against the spec_schema contract)
await Translator(llm_client=client).run({"source_text": "Hello", "style": "formal"})

# Pack & publish: exports module.json + harnesses/ + scripts/ + commands/
dist = Translator().pack("dist/my_translator")

# Load & run from another process/project (requires dependency validation)
loaded = ModuleLoader().load(dist)
await loaded.run({"source_text": "Hello", "style": "casual"})
```

## Embedded Use (host project imports the library)

After `pip install specmodule`, another project can `import` the library's
public API directly and embed SpecModule as an LLM toolkit in its own service /
IDE plugin / web backend. Minimal demo in
[`examples/embed_minimal/`](examples/embed_minimal/main.py) (includes `--mock`
key-free smoke):

```bash
pip install specmodule
cd examples/embed_minimal && python main.py --mock
```

```python
from module_harness import (
    EventBus, HarnessConfig, HarnessRegistry, Module,
    TemplateLoader, OutputFormat, register_builtin_harnesses,
)
from llm import LLMConfig, create_llm_client

client = create_llm_client(LLMConfig.from_env())   # .env / env vars
bus = EventBus()

reg = HarnessRegistry(llm_client=client, event_bus=bus)
register_builtin_harnesses(reg)                    # spec_to_tasklist built-in set
reg.harness("translate", HarnessConfig(
    prompt_core="Translate the following text to Chinese: {text}",
    output_format=OutputFormat(type="json_object"),
))

loader = TemplateLoader(); loader.load_builtins()
module = Module(
    spec={"source_text": "Hello world", "style": "formal"},
    template_name="translate",
    llm_client=client, event_bus=bus, registry=reg, template_loader=loader,
    persist=False, status_file=False, keep_records=False,   # zero disk / zero residue
)
await module.run()
```

Embedding notes:

- **Events decoupled from records** (`decouple-embed-events`) — passing
  `event_bus` delivers `OutputValidated` / `HarnessFailed` events to the host
  without dragging in audit or persistence; no bus → silent zero overhead.
- **Zero-residue optional** — `persist=False` + `status_file=False` +
  `keep_records=False` leaves nothing on disk.
- **Built-in harnesses registered explicitly** — translation/review/alignment
  harnesses are not implicitly loaded; call `register_builtin_harnesses(reg)`.
- The public surface = `module_harness` top-level exports (`Module / HarnessRegistry /
  SubModule / Translator / query` shared layer...); don't reach into internal modules.

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

**Library core framework capabilities complete** (18 items); **library mainline
complete** (2026-08-22): packaging wiring (`pyproject.toml` + `specmodule` CLI
shipped with the library), full module-user-store series (store home / config
fallback chain / unified enumeration for run / CLI management setup-install-list-
info-uninstall-publish-update / init directory form), independent lines (embed
verification demo + stdlib visualization feed). **0.1.1 (2026-08-23)**: fixed
`init --as-dir` scaffold template brace rendering (generated modules run directly
with `--mock`); git-URL install refined — clone worktree `.git` is excluded from
store and from manifest hashes (`update` dirty detection no longer sees
version-control noise; git-source repos must be pack directories at the root —
`module.json` at root). Remaining: M2 practice line (store
acceptance), API stabilization, ecosystem projects (TUI/MCP/Web). See
[module-roadmap.md](docs/progress/module-roadmap.md) for the full roadmap.

## Design Principles

- **Zero tickflow modification (conditional)** — tickflow is an external dependency (PyPI package `tickflow-py`, import name `tickflow`, upstream https://github.com/MountLynx/tickflow-); no tickflow code lives in this repo. Before modifying anything, judge: does the change have universal value / genuinely improve tickflow itself? **No → don't touch it** (module-layer features extend via `Registry` subclassing). **Yes → make the change upstream**, publish a new `tickflow-py` release, and bump the installed version.
- **Two user levels** — the framework serves two audiences: **developer users** (author and publish modules) and **end users** (only write spec/tasklist). The boundary is intentionally soft — developers also consume modules, and end users can customize them. In essence there are two usage scenarios (the *development scenario* vs the *usage scenario*); new features must state which scenario they primarily serve.
- **Full control** — No implicit behavior; a wrong promptmode raises KeyError; the framework does not paper over design mistakes
- **Audit by design** — All state is recorded in RunState; snapshot and rewind are built-in capabilities
- **SDK first** — Design the data-query interface before implementing any new feature; consumer surfaces (CLI/agent/Web) are thin wrappers over the SDK
- **YAGNI** — Every feature has a concrete use case before it is added

## License

MIT
