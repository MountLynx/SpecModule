# SpecModule — Agent Instructions

## Purpose

SpecModule is an auditable, debuggable, fully controllable LLM usage framework. It decomposes LLM calls into composable Petri-net nodes — each node is a minimal execution unit (translation, review, shell, Python). The engine (`tickflow`) runs synchronous Petri-net steps with centralized state, making snapshots, pause, and rewind cheap.

## Project Layout

```
tickflow/          Petri-net workflow engine — zero external deps, owns IR/parser/engine/runner/state
llm/               LLM clients (Anthropic + OpenAI-compatible) — self-contained, env-driven config
module_harness/    Upper layer — harness/script/command nodes, spec/tasklist models, translation, Module orchestrator
  templates/       Built-in tasklist templates (JSON)
  tests/           Pytest test suite
docs/
  progress/        module-roadmap.md — implementation status & roadmap
  superpowers/     specs/ (design specs), plans/ (implementation plans)
```

## Test Commands

No build system (`pyproject.toml`, `setup.py`). Run tests directly with pytest:

```bash
python -m pytest module_harness/tests/ -q
python -m pytest tickflow/tests/ -q        # if tickflow has its own tests
```

Tests use `pytest` + `unittest.mock` (`MagicMock`, `AsyncMock`). Fixtures defined in `conftest.py`. Python 3.13.

## Architecture Rules

1. **tickflow is never modified.** All module-layer features extend via `Registry` subclass (`HarnessRegistry(Registry)`). The engine, runner, state, parser — all tickflow internals — are off-limits for module_harness changes.

2. **Layer dependency order** (strict, no cycles):
   - `tickflow/` → no internal deps (zero external imports)
   - `llm/` → self-contained (only `os`, `dataclasses`, `pathlib`, `typing`)
   - `module_harness/` → depends on `tickflow` + `llm`

3. **Single source of truth:** `RunState` (tickflow) is the sole runtime state container. Three layers: `_edges` (fast input resolution, windowed to last 2 firings per node), `_state` (per-node mutable state), `_records` (full audit, gated by `keep_records`; persisted via backend when one is attached). Never create parallel state tracking.

4. **Namespace isolation:** Multiple `Module` instances coexist in one process. Body names are prefixed `{module_id}:{key}` by `TasklistTranslator` — never hardcode bare names across modules.

## Coding Conventions

- Every file starts with `from __future__ import annotations`
- Data models use `@dataclass`; config uses `@dataclass` with `field(default_factory=...)`
- Type annotations on all public signatures
- Public API exported via `__init__.py` with explicit `__all__`
- Chinese docstrings in `module_harness/`; English in `tickflow/`
- Event-driven observer pattern: `EventBus` with typed dataclass events per lifecycle stage
- Harness has 3-layer prompt: `prompt_core` (template) → `prompt_modes` (dynamic selection) → `prompt_extra` (human-injected). Layer 2 key mismatch raises `KeyError` — no silent fallback.

## Node Types

| Type | Registration | Purpose |
|------|-------------|---------|
| **harness** | `reg.harness("name", config)` | LLM call with 3-layer prompt, output validation, streaming tokens |
| **script** | `@reg.script("name")` | Pure Python function, decorator-registered |
| **command** | `reg.command("name", config)` | Shell subprocess, one-line string = node |

## Key Design Docs

Before modifying sensitive areas, read the relevant spec:

- **Roadmap:** `docs/progress/module-roadmap.md` — 12/19 features done, implementation order with dependency chain
- **Harness design:** `docs/superpowers/specs/2026-06-29-module-harness-design.md`
- **Command node:** `docs/superpowers/specs/2026-06-30-command-node-design.md`
- **Spec/Tasklist:** `docs/superpowers/specs/2026-06-30-spec-tasklist-design.md`
- **Tickflow engine:** `tickflow/README.md` — full semantics (Petri net model, joins, deadlock detection, snapshots, persistence)

## Gotchas

- **No implicit behavior:** Missing `promptmode` key → `KeyError`, empty `output_format` with `json_object` type → validation error. The framework does not guess.
- **OutputFormat enum values:** `"json_object"`, `"json_schema"`, `"text"` — typos fail at runtime.
- **Failure types:** `Failure("msg", type="llm")` → downstream skipped but run continues. `type="infrastructure"` → `ABORTED`, run halts.
- **Deadlock:** AND-join nodes fed by XOR-splitter branches will deadlock. The checker catches this; `Runner` construction raises `DeadlockError` if unresolved.
- **Unguarded cycles:** A loop without at least one guarded edge will run forever. Parser emits `UnguardedCycleWarning`.
- **LLM config:** reads from env vars (`LLM_PROVIDER`, `LLM_MODEL`, `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`, etc.) via `LLMConfig.from_env()`. `.env` file auto-loaded from project root.
