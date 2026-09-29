# SpecModule — Agent Instructions

## Purpose

SpecModule is an auditable, debuggable, fully controllable LLM usage framework. It decomposes LLM calls into composable Petri-net nodes — each node is a minimal execution unit (translation, review, shell, Python). The engine (`tickflow`) runs synchronous Petri-net steps with centralized state, making snapshots, pause, and rewind cheap.

## Project Layout

```
tickflow           Petri-net workflow engine — external pip dependency (tickflow-py, import name tickflow); owns IR/parser/engine/runner/state
llm/               LLM clients (Anthropic + OpenAI-compatible) — self-contained, env-driven config
module_harness/    Upper layer — harness/script/command nodes, spec/tasklist models, translation, Module orchestrator
  core/            Harness node, config, prompt, output validation, registry, builtins, task-level call floor
  model/           Spec/tasklist models, translator, Module orchestrator, SubModule
  orchestrate/     Tasklist→Petri-net graph builder, consistency review, align check, run feed
  infra/           Events, cross-process stream/control channels, checkpoint, shared query layer, module store
  cli/             CLI entry (module_harness.cli:main), module discovery/loader, scaffold, command node
  templates/       Built-in tasklist templates (JSON)
  tests/           Pytest test suite
docs/
  progress/        module-roadmap.md — implementation status & roadmap
  superpowers/     specs/ (design specs), plans/ (implementation plans)
```

## Test Commands

Run tests directly with pytest (no test runner configured in `pyproject.toml`):

```bash
python -m pytest module_harness/tests/ -q
```

`pyproject.toml` exists for packaging (`pip install specmodule`; console
script `specmodule = module_harness.cli:main`) — it does not configure
build/test tooling. Tests use `pytest` + `unittest.mock` (`MagicMock`,
`AsyncMock`). Fixtures defined in `conftest.py`. Python 3.13. Requires
`tickflow-py` installed (`pip install tickflow-py`).

## Architecture Rules

1. **tickflow is an external dependency.** No tickflow code lives in this
   repo — it's installed from PyPI as `tickflow-py` (import name `tickflow`,
   upstream `https://github.com/MountLynx/tickflow-`), and `import tickflow`
   resolves to site-packages. Before touching any tickflow
   internals, judge whether the change has universal value / genuinely
   improves tickflow itself:
   - **No universal value** → do NOT modify tickflow. Module-layer features
     extend via `Registry` subclass (`HarnessRegistry(Registry)`) — the
     engine, runner, state, parser are off-limits by default.
   - **Has universal value** → make the change in the local upstream
     clone and commit/push it there,
     publish a new `tickflow-py` release to PyPI, then bump the installed
     version here (`pip install -U tickflow-py`).

2. **Layer dependency order** (strict, no cycles):
   - `tickflow` (external pip pkg `tickflow-py`) → no internal deps (zero external imports)
   - `llm/` → self-contained (only `os`, `dataclasses`, `pathlib`, `typing`)
   - `module_harness/` → depends on `tickflow` + `llm`

3. **Single source of truth:** `RunState` (tickflow) is the sole runtime state container. Three layers: `_edges` (fast input resolution, windowed to last 2 firings per node), `_state` (per-node mutable state), `_records` (full audit, gated by `keep_records`; persisted via backend when one is attached). Never create parallel state tracking.

4. **Namespace isolation:** Multiple `Module` instances coexist in one process. Body names are prefixed `{module_id}:{key}` by `TasklistTranslator` — never hardcode bare names across modules.

5. **This repo is a developer library.** SpecModule's current form is the
   *library* (`tickflow` + `llm` + `module_harness` + thin CLI), serving
   **developer users** — authoring and publishing modules (submodule / script /
   harness / tasklist templates), driving workflows programmatically via the
   programming API / CLI, and debugging runs. It is NOT an end-user product.
   End-user consumption forms (rich TUI, MCP/ACP server, web visualization)
   are **separate ecosystem projects** (`SpecModule_tui/`, `SpecModule_mcp/`,
   `SpecModule_webview/`), each an independent repo that consumes this library
   the way this library consumes `tickflow`. "Write spec / tasklist" is a
   **module-facing interface contract** every module exposes — not a separate
   usage scenario of this repo. New features should state which developer
   capability they serve; end-user form work belongs to the ecosystem repos.

6. **Extract on duplication; house what has a confirmed second consumer.**
   Never abstract for the future: extract a function only when the same logic
   appears a second time (DRY). A shared-layer home (in `module_harness`) is
   justified only by a **confirmed** second consumer — which may live outside
   this repo (an ecosystem form, an embedder) as well as inside it. Pure
   passthrough wrappers are never extracted. Example: query composition
   (audit timeline / output history) is consumed by the in-repo CLI and by
   ecosystem forms (MCP, Web) and embedders → it lives once in
   `module_harness/infra/query.py`; every consumer imports it, never reimplements.

## Run & Debug Discipline — reuse the framework's capabilities

SpecModule's core value is auditable, debuggable, controllable runs. When
driving real (non-mock) module pipelines — debugging, tuning, or long
generation chains — use what the framework already provides instead of
bypassing it with ad-hoc tooling:

1. **Run with persistence on.** Debugging/tuning runs use `persist=True`
   (persistence backend + status/stream + `specmodule feed` query layer),
   not the zero-persistence fast path. Diagnose failures from the audit
   timeline and on-disk gate reports — never "rerun until it dies" as a
   probing strategy, and never rebuild parallel audit dumps (ad-hoc firing
   logs) for what events/records already capture. Cross-run comparisons
   (did the issue set shrink after a contract change?) go through the
   persisted audit, not reread-from-memory.
2. **Let the repair ring do its job.** Page-level failures (issues carrying
   a `page` key) are the designed self-healing path — read repair receipts
   (`repaired/failed/skipped`) before changing anything. Scope-level
   failures (design_spec §VIII / spec_lock / icon_pool — one-shot artifacts
   the repair ring cannot rewrite) are unrepairable by design: fix the
   spec/prompt contract, then rerun the affected prefix.
3. **Cheap preflight before long chains.** When iterating on spec/prompt
   contracts, probe the plan stage alone (one LLM call) and machine-check
   its receipt against every vocabulary (machine anchors, resource word
   lists, icon existence) before spending page-generation cost.
4. **Know the state boundary.** RunState snapshots/persistence cover engine
   state only; workspace artifacts (svg_output, spec_lock, gate reports)
   live outside it — "rewinding the graph" is not "rewinding the deck".
   Artifact-level versioning/resume belongs to ecosystem forms (rule 5);
   the library states this boundary instead of faking it.

Write-ups behind these rules: `docs/dev/ppt-master-spec-authoring.md`
(spec workflow, word-list layering), `docs/dev/planner-resource-blindspot.md`
(planner blindspot analysis, candidate fixes).

## Coding Conventions

- Every file starts with `from __future__ import annotations`
- Data models use `@dataclass`; config uses `@dataclass` with `field(default_factory=...)`
- Type annotations on all public signatures
- Public API exported via `__init__.py` with explicit `__all__`
- Chinese docstrings in `module_harness/`
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

- **Roadmap:** `docs/dev/progress/module-roadmap.md` — library done (framework capabilities complete); next: library packaging / embedding / init scaffold, then ecosystem projects (TUI/MCP/Web) with practice-line modules
- **Harness design:** `docs/dev/superpowers/specs/2026-06-29-module-harness-design.md`
- **Command node:** `docs/dev/superpowers/specs/2026-06-30-command-node-design.md`
- **Spec/Tasklist:** `docs/dev/superpowers/specs/2026-06-30-spec-tasklist-design.md`
- **Tickflow engine:** upstream README (`https://github.com/MountLynx/tickflow-`) — full semantics (Petri net model, joins, deadlock detection, snapshots, persistence); installed copy at `site-packages/tickflow/README.md`

## Gotchas

- **No implicit behavior:** Missing `promptmode` key → `KeyError`, empty `output_format` with `json_object` type → validation error. The framework does not guess.
- **OutputFormat enum values:** `"json_object"`, `"json_schema"`, `"text"` — typos fail at runtime.
- **Failure types:** `Failure("msg", type="llm")` → downstream skipped but run continues. `type="infrastructure"` → `ABORTED`, run halts.
- **Deadlock:** AND-join nodes fed by XOR-splitter branches will deadlock. The checker catches this; `Runner` construction raises `DeadlockError` if unresolved.
- **Unguarded cycles:** A loop without at least one guarded edge will run forever. Parser emits `UnguardedCycleWarning`.
- **LLM config:** reads from env vars (`LLM_PROVIDER`, `LLM_MODEL`, `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`, etc.) via `LLMConfig.from_env()`. `.env` file auto-loaded from project root.
