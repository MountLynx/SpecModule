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
│   └── config.py           #   LLMConfig.from_env(): config fallback chain (env > project root > store)
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
├── examples/              # Embedded minimal demo (embed_minimal) + tutorial case (tutorial)
└── docs/                  # User docs (guides/references/concepts) + internal docs (dev/)
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

## Quick Start

```bash
pip install specmodule
specmodule setup                    # one-time provider/model/key setup (writes store-level config)
specmodule install <pack dir or git URL>   # fetch a module (see store-walkthrough)
specmodule run --module <name> --spec '{"text": "..."}' --mock   # --mock key-free smoke
specmodule review --run-id <name>   # review the tick timeline
```

Writing your first module (entry declaration → harness/script registration →
tasklist → publish): see [**Tutorial: from zero to your first module**](docs/guides/tutorial-first-module.md);
the store usage loop: [**store-walkthrough**](docs/guides/store-walkthrough.md);
configuration: [**config-guide**](docs/guides/config-guide.md).

## Docs Navigation

| You are | Entry point |
|---------|-------------|
| Module user (CLI) | [store-walkthrough](docs/guides/store-walkthrough.md) → [cli-usage reference](docs/references/cli-usage.md) |
| Module author | [Tutorial](docs/guides/tutorial-first-module.md) → [tasklist execution semantics](docs/references/tickflow-integration.md) → [syntax reference](docs/references/spec-harness-syntax.md) |
| Understand the framework (concepts) | [concepts/SpecModule.md](docs/concepts/SpecModule.md) |
| Embed in a host project | [embedding.md](docs/guides/embedding.md) (demo: `examples/embed_minimal/`) |
| Full index | [docs/README.md](docs/README.md) |

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
- **Two input modes**: ① spec only (translated to tasklist via template) ② spec + tasklist (consistency check, then straight into graph builder). Selection guidance and the template channel: [concepts](docs/concepts/SpecModule.md).

### Snapshot & Rollback

Lightweight per-tick snapshot (persisted every tick with `persist=True`); any
tick can be precisely rewound (`resume(tick)` / `rollback`); manual checkpoints
`checkpoint("label")` are kept permanently; in-process `snapshot()` / `restore()`
support branching. Persistence conventions and the sensitive-data note:
[concepts](docs/concepts/SpecModule.md).

### SubModule — class-style modules + pack/publish

`SubModule` class-style declaration (with `spec_schema` input contract) →
`pack()` exports a publishable manifest (module.json + harnesses/ + scripts/ +
commands/) → `ModuleLoader` loads it (`requires` dependency validation).
`mode = "fast"` runs with zero disk I/O.

## Current Status

**Library core framework capabilities complete** (18 items); **library mainline
complete** (2026-08-22): packaging wiring, full module-user-store series (store
home / config fallback chain / unified enumeration for run / CLI management
setup-install-list-info-uninstall-publish-update / init directory form),
independent lines (embed verification demo + stdlib visualization feed);
**0.1.1 (2026-08-23)** init scaffold fix + git-URL install refinement. Remaining:
M2 practice line, API stabilization, ecosystem projects (TUI/MCP/Web). Full
roadmap: [docs/dev/progress/module-roadmap.md](docs/dev/progress/module-roadmap.md) (internal doc).

## Design Principles

- **Zero tickflow modification (conditional)** — tickflow is an external dependency (PyPI package `tickflow-py`, import name `tickflow`, upstream https://github.com/MountLynx/tickflow-); no tickflow code lives in this repo. Before modifying anything, judge: does the change have universal value / genuinely improve tickflow itself? **No → don't touch it** (module-layer features extend via `Registry` subclassing). **Yes → make the change upstream**, publish a new `tickflow-py` release, and bump the installed version.
- **Two user levels** — the framework serves two audiences: **developer users** (author and publish modules) and **end users** (only write spec/tasklist). The boundary is intentionally soft. In essence there are two usage scenarios (the *development scenario* vs the *usage scenario*); new features must state which scenario they primarily serve.
- **Full control** — No implicit behavior; a wrong promptmode raises KeyError; the framework does not paper over design mistakes.

## License

MIT
