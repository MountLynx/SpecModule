# ppt_master 模块实施计划（替代 M2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新模块 `ppt_master` 完全复刻 ppt-master Generate 主线工程范式（LLM 逐页 SVG → 质量门 → svg_to_pptx 编译），单模板 `generate` 动态图，替代 ppt_writer 成为 M2 本体。

**Architecture:** 翻译器（script）按 spec 显式页册静态展开 Petri 图：LLM 节点全部为 script 包 `call_harness`（失败收据保证 AND join 不饿死），确定性环节为 vendor 闭包 command 节点，质量门为 command + 守卫修复环（上限 2 轮）。节点间数据走"进程级信封 + 项目目录文件"。

**Tech Stack:** Python 3.13、tickflow（Petri 引擎）、module_harness（HarnessConfig/call_harness/HarnessRegistry/ModuleEntry）、vendor ppt-master scripts（MIT）、pytest + MockLLMClient。

**Spec:** `docs/dev/superpowers/specs/2026-09-15-ppt-master-module-design.md`（含 534100c 修订：NotesGen 在终门后）

---

## 已核实的框架事实（执行者必读）

以下事实已在本仓库源码逐行核实，任务代码直接依赖，不要重新发明：

1. **注册**：`HarnessRegistry(llm_client=, event_bus=)`；`reg.harness(name, HarnessConfig)` / `reg.script("n")(fn)`（支持 async fn）/ `reg.command("n", CommandConfig(command=静态串, timeout=))` / `reg.guard("n", fn)`。位置 `module_harness/core/registry.py`。
2. **script body**：`fn(view)`，inputs 经 `TaskDefinition(inputs={"字段名": "生产者节点"})` 绑定，函数内 `view.field("字段名")` 取上游输出。**guard 读的必须是守卫边源任务的 inputs 键**（ppt_writer `_has_issues` 模式，`example/ppt_writer/module.py:310`）。
3. **Failure 语义**（tickflow `ir.py`）：`Failure(type="llm")` → 出边写 False、下游 AND join **不点火**（饥饿）；`type="infrastructure"` → run ABORTED。⇒ 页节点绝不能返回 Failure，必须返回失败收据 dict。
4. **task 级调用**：`module_harness/core/call.py::call_harness(config, values, llm_client=, prompt_extra=, event_bus=)` → `HarnessCallResult(value, raw, usage)`；失败抛 `HarnessCallError`（携带 `.failure/.prompt/.raw/.usage`）。
5. **三层 prompt**：`HarnessConfig(prompt_core, prompt_modes, output_format, notdo, mode=, image_dir=)`；`promptmode` 缺 key 时 KeyError（不静默）。`mode="image"` 与 `output_format` 互斥；图像节点返回落盘路径 str。
6. **command body**：返回 `{"stdout","stderr","returncode"}`；失败（超时/异常）返回 `Failure(type="llm")`。命令串是静态的 → 动态路径经**进程级信封文件**（ppt_writer `normalize.write_envelope` 模式：pid 修饰的临时 JSON，路径嵌进静态命令串）。
7. **翻译器**：模板声明 `"translation": {"type": "script", "script": "名"}`；翻译 script 收 `view.field("spec")`（全 spec dict），返回 `{"Tasks": {名: TaskDefinition dict}, "Flow": "..."}`；节点名匹配 `[A-Za-z_][A-Za-z0-9_]*`。flow 一行一条边（`\n` 连接）。
8. **Module**：`Module(spec, template_name=, template_loader=, llm_client=, registry=, review_harness=None, persist=, status_file=)`；`asyncio.run(mod.run(max_ticks=N))` → firings 列表（`f.node` / `f.output`）。
9. **Mock**：`llm.mock.MockLLMClient.complete(**kw)` — kw 含 `prompt` 与 `output_format`；测试可子类化按 prompt 内容脚本化响应。
10. **CLI**：`from module_harness.cli import main; main(["run","--module","ppt_master","--mock","--modules-dir",str(dir),"--run-id","x","--spec",json_str])` → rc 0。
11. **测试跑法**：`python -m pytest example/test_ppt_master_xxx.py -q`（无 pytest 配置，直接指路径）。现有 ppt 测试模式见 `example/test_ppt_workflow.py`（monkeypatch 隔离、`asyncio.run`、`firings` 断言）。

## 文件结构（全部任务的落点）

```
example/ppt_master/                  # 新模块包
  __init__.py                        # Task 8
  module.py                          # 模板声明 + registry 构建 + run_generate（Task 8）
  spec_schema.py                     # validate_ppt_spec（Task 3）
  workspace.py                       # 目录契约 + 信封（Task 2）
  translator.py                      # build_generate_tasklist + tl_generate（Task 7）
  llm_nodes.py                       # call_harness 包裹节点工厂（Task 5）
  tools_nodes.py                     # 确定性 script 节点（Task 6）
  prompts_config.py                  # 参考包组装 + 5 个 HarnessConfig（Task 4）
  tools/run_tool.py                  # command 入口（Task 6）
  prompts/                           # 参考素材（Task 4 拷贝改编）
    ADAPTATIONS.md
  fixtures/                          # checker 合格 SVG fixture（Task 1 产出，Task 9 复用）
  vendor/ppt_master/                 # MIT 闭包（Task 1）
    LICENSE  NOTICE
    scripts/…  templates/…
example/modules/ppt_master.py        # ModuleEntry（Task 8）
example/test_ppt_master_*.py         # 各任务测试
example/modules/ppt_writer.py 等     # Task 10 归档
```

---

### Task 1: vendor 闭包落位与四件套真实验证

**风险最高，先行**（"工程上完全复刻"的验收底线）。本任务无 TDD——它是资产搬运 + 真实工具链验证，验收 = 闭包检查脚本通过 + checker/finalize/export 对 fixture 真实跑通。

**Files:**
- Create: `example/ppt_master/vendor/ppt_master/`（scripts/ templates/ LICENSE NOTICE）
- Create: `example/ppt_master/fixtures/`（页 SVG + 最小 project 工件）
- Create: `example/test_ppt_master_vendor.py`

- [x] **Step 1: 拷贝闭包**

源：`E:\Index\Programming\52a8bdf45d94\ppt-master\skills\ppt-master\`（下称 `$SRC`）。

```bash
V=example/ppt_master/vendor/ppt_master
mkdir -p $V/scripts $V/templates example/ppt_master/fixtures
# 11 个入口脚本
for f in svg_quality_checker.py finalize_svg.py svg_to_pptx.py text_measure.py \
         icon_sync.py image_gen.py image_search.py slice_images.py \
         analyze_images.py source_to_md.py total_md_split.py; do
  cp "$SRC/scripts/$f" $V/scripts/
done
# 平铺依赖模块（导入闭包，缺哪个下一步闭包检查会报）
for f in console_encoding.py attribution_guard.py resource_paths.py project_specs.py \
         slide_roster.py hyperlink_contract.py pptx_animations.py pptx_transitions.py \
         config.py error_helper.py native_payloads.py language_tags.py; do
  cp "$SRC/scripts/$f" $V/scripts/ 2>/dev/null || true
done
# 子包（整体拷贝）
for d in svg_quality svg_finalize svg_to_pptx pptx_to_svg pptx_ooxml pptx_shapes \
         image_backends image_sources source_to_md; do
  cp -r "$SRC/scripts/$d" $V/scripts/
done
# 模板语法文件与 schemas
cp "$SRC/templates/design_spec_reference.md" "$SRC/templates/spec_lock_reference.md" $V/templates/
cp -r "$SRC/templates/schemas" "$SRC/templates/icons" "$SRC/templates/charts" "$SRC/templates/tables" $V/templates/
cp "$SRC/LICENSE" $V/LICENSE
```

- [x] **Step 2: 写 NOTICE**

`example/ppt_master/vendor/ppt_master/NOTICE`：

```markdown
# NOTICE

本目录是 ppt-master (https://github.com/hugohe3/ppt-master) v6.3.2 的部分文件拷贝，
MIT License，版权归原作（见 LICENSE）。拷贝范围：SVG→PPTX 导出管线所需最小闭包
（11 个入口脚本 + 平铺依赖 + 8 个子包 + 模板语法文件）。

本地改动（如有，逐条记录于此）：
- （拷贝后核对 attribution_guard.py 在 skill 目录外是否可运行；不可运行时的最小
  适配记录在此，适配原则：只放宽路径锚定，不改校验逻辑）
```

- [x] **Step 3: 写闭包检查脚本并跑通**

`example/test_ppt_master_vendor.py`：

```python
# example/test_ppt_master_vendor.py
"""vendor 闭包完整性：入口脚本可被 python 语法加载（AST 导入名均可在
vendor 内或标准库解析）；attribution_guard 行为已核对。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

VENDOR_SCRIPTS = (
    Path(__file__).parent / "ppt_master" / "vendor" / "ppt_master" / "scripts"
)

ENTRY_SCRIPTS = [
    "svg_quality_checker.py", "finalize_svg.py", "svg_to_pptx.py",
    "text_measure.py", "icon_sync.py", "image_gen.py", "image_search.py",
    "slice_images.py", "analyze_images.py", "source_to_md.py",
    "total_md_split.py",
]


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_entry_scripts_exist():
    for name in ENTRY_SCRIPTS:
        assert (VENDOR_SCRIPTS / name).is_file(), name


def test_entry_scripts_parse_and_imports_resolvable():
    sys.path.insert(0, str(VENDOR_SCRIPTS))
    try:
        local = {p.stem for p in VENDOR_SCRIPTS.glob("*.py")}
        local |= {p.name for p in VENDOR_SCRIPTS.iterdir() if p.is_dir()}
        for name in ENTRY_SCRIPTS:
            imports = _top_level_imports(VENDOR_SCRIPTS / name)
            missing = {
                m for m in imports
                if m not in local and m not in sys.stdlib_module_names
                and importlib_util_find_spec(m) is None
            }
            assert not missing, f"{name} 缺依赖: {sorted(missing)}"
    finally:
        sys.path.pop(0)


def importlib_util_find_spec(name: str):
    import importlib.util
    try:
        return importlib.util.find_spec(name)
    except (ModuleNotFoundError, ValueError):
        return None
```

Run: `python -m pytest example/test_ppt_master_vendor.py -q`
Expected: PASS（缺文件/缺依赖会点名；按缺项回 Step 1 补拷对应文件/子包，循环直至 PASS）

- [x] **Step 4: 构造最小 fixture 项目并跑通 checker（early）**

fixture = 一个最小可过 checker 的单页项目，放 `example/ppt_master/fixtures/`：`page_p01.svg`（1280×720，标题 + 两要点 + `data-pptx-*` 语义元数据按 `vendor/.../scripts/docs/` 中 SVG 契约文档写）+ `design_spec.md` / `spec_lock.md` 最小件（语法按 `$SRC/templates/design_spec_reference.md`，§IX 一页含 `Audience move:` 行）。

迭代程序（诚实执行，允许 3–5 轮）：

```bash
F=example/ppt_master/fixtures
python example/ppt_master/vendor/ppt_master/scripts/svg_quality_checker.py $F \
  --canonical-authoring --stage early --json
# 读 stdout 摘要 + validation/svg_quality_early_report.json 的
# categories.blocking.issues，修 fixture SVG/spec 文件后重跑，直至 0 blocking。
```

若 3–5 轮后 checker 仍要求 fixture 无法满足的项目级工件（例如强制完整 §I–X、图标清单等）：**停下升级给用户**，带上来的是 checker 的具体 blocking 清单——不要为过门而削 gate 逻辑。

- [x] **Step 5: 跑通 finalize + export 四件套**

```bash
F=example/ppt_master/fixtures
python example/ppt_master/vendor/ppt_master/scripts/svg_quality_checker.py $F \
  --canonical-authoring --stage final --json
python example/ppt_master/vendor/ppt_master/scripts/finalize_svg.py $F
python example/ppt_master/vendor/ppt_master/scripts/svg_to_pptx.py $F --no-notes
ls $F/exports/   # 期望: *.pptx + validation/*report.json (passed)
```

Expected: `exports/` 出现 .pptx；用 `python -c "from pptx import Presentation; print(len(Presentation(r'<pptx 路径>').slides))"` 断言 1 页。

- [x] **Step 6: 提交**

```bash
git add example/ppt_master/vendor example/ppt_master/fixtures example/test_ppt_master_vendor.py
git commit -m "feat(ppt_master): vendor ppt-master SVG→PPTX 最小闭包 + fixture 四件套验证"
```

---

### Task 2: workspace.py —— 目录契约 + 进程级信封

**Files:**
- Create: `example/ppt_master/workspace.py`
- Test: `example/test_ppt_master_workspace.py`

- [x] **Step 1: 写失败测试**

```python
# example/test_ppt_master_workspace.py
"""workspace：目录契约建立 + 信封读写（pid 隔离）。"""

from __future__ import annotations

from example.ppt_master import workspace


def test_init_workspace_creates_contract(tmp_path):
    root = workspace.init_workspace(tmp_path / "deck")
    for d in ("sources", "analysis", "images", "icons", "svg_output",
              "svg_final", "notes", "validation", "exports"):
        assert (root / d).is_dir(), d
    assert (root / "validation" / "workflow.log").exists()
    # 幂等：重复调用不抛
    workspace.init_workspace(root)


def test_envelope_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    workspace.write_envelope({"output_dir": str(tmp_path), "roster": []})
    assert workspace.read_envelope()["output_dir"] == str(tmp_path)


def test_gate_report_path(tmp_path):
    assert workspace.gate_report_path(tmp_path, "early").name == "svg_quality_early_report.json"
    assert workspace.gate_report_path(tmp_path, "final").name == "svg_quality_report.json"


def test_append_workflow_log(tmp_path):
    workspace.init_workspace(tmp_path)
    workspace.append_workflow_log(tmp_path, "手工恢复一次")
    log = (tmp_path / "validation" / "workflow.log").read_text(encoding="utf-8")
    assert "手工恢复一次" in log
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_workspace.py -q`
Expected: FAIL（`No module named 'example.ppt_master'`）——先 `touch example/ppt_master/__init__.py`（空文件）再跑仍是 FAIL（缺函数）。

- [x] **Step 3: 实现**

```python
# example/ppt_master/workspace.py
"""项目目录契约 + 进程级信封（ppt_writer normalize 信封模式复用）。

command 节点命令串是静态的（框架约束）——动态路径（output_dir 等）写入
pid 修饰的临时信封 JSON，路径字面量嵌进命令串，工具入口 run_tool.py 读信封。
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
VENDOR_SCRIPTS = MODULE_DIR / "vendor" / "ppt_master" / "scripts"

_ENVELOPE_DIR = Path(tempfile.gettempdir())

WORKSPACE_DIRS = (
    "sources", "analysis", "images", "icons", "svg_output",
    "svg_final", "notes", "validation", "exports",
)


def _envelope_path() -> Path:
    return _ENVELOPE_DIR / f"specmodule_ppt_master_generate_{os.getpid()}.json"


def write_envelope(data: dict[str, Any]) -> Path:
    path = _envelope_path()
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def read_envelope() -> dict[str, Any]:
    return json.loads(_envelope_path().read_text(encoding="utf-8"))


def init_workspace(output_dir: str | Path) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    for d in WORKSPACE_DIRS:
        (root / d).mkdir(exist_ok=True)
    (root / "validation" / "workflow.log").touch(exist_ok=True)
    return root


def append_workflow_log(root: str | Path, detail: str) -> None:
    log = Path(root) / "validation" / "workflow.log"
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {detail}\n")


def gate_report_path(root: str | Path, stage: str) -> Path:
    name = "svg_quality_early_report.json" if stage == "early" else "svg_quality_report.json"
    return Path(root) / "validation" / name
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_ppt_master_workspace.py -q`
Expected: PASS（4 项）

- [x] **Step 5: 提交**

```bash
git add example/ppt_master/__init__.py example/ppt_master/workspace.py example/test_ppt_master_workspace.py
git commit -m "feat(ppt_master): workspace 目录契约 + 进程级信封"
```

---

### Task 3: spec_schema.py —— spec 校验（字段路径报错）

**Files:**
- Create: `example/ppt_master/spec_schema.py`
- Test: `example/test_ppt_master_schema.py`

- [x] **Step 1: 写失败测试**

```python
# example/test_ppt_master_schema.py
"""spec 校验：必备字段、页册形状、枚举值、缺省回填（含字段路径报错）。"""

from __future__ import annotations

import pytest

from example.ppt_master.spec_schema import validate_ppt_spec


def _base_spec() -> dict:
    return {
        "project": "demo",
        "source": {"kind": "files", "paths": ["paper.md"]},
        "roster": [
            {"id": "p01", "title": "封面", "role": "cover"},
            {"id": "p02", "title": "背景", "points": ["a"]},
        ],
    }


def test_minimal_spec_passes_with_defaults():
    spec = _base_spec()
    validate_ppt_spec(spec)  # 不抛
    assert spec["reading_mode"] == "balanced"
    assert spec["output"]["dir"] == "projects/demo"
    assert spec["production"]["speaker_notes"] is True
    assert spec["roster"][1]["role"] == "content"


def test_missing_roster_raises_with_field_path():
    spec = _base_spec(); del spec["roster"]
    with pytest.raises(ValueError, match="roster"):
        validate_ppt_spec(spec)


def test_duplicate_page_id_rejected():
    spec = _base_spec()
    spec["roster"].append({"id": "p01", "title": "重复"})
    with pytest.raises(ValueError, match="p01"):
        validate_ppt_spec(spec)


def test_bad_role_rejected():
    spec = _base_spec()
    spec["roster"][0]["role"] = "hero"
    with pytest.raises(ValueError, match="role"):
        validate_ppt_spec(spec)


def test_source_kind_topic_requires_topic():
    spec = _base_spec()
    spec["source"] = {"kind": "topic"}
    with pytest.raises(ValueError, match="source.topic"):
        validate_ppt_spec(spec)


def test_bad_reading_mode_rejected():
    spec = _base_spec()
    spec["reading_mode"] = "dense"
    with pytest.raises(ValueError, match="reading_mode"):
        validate_ppt_spec(spec)


def test_bad_image_source_rejected():
    spec = _base_spec()
    spec["images"] = {"sources": ["ai", "magic"]}
    with pytest.raises(ValueError, match="images.sources"):
        validate_ppt_spec(spec)


def test_images_none_conflict_rejected():
    spec = _base_spec()
    spec["images"] = {"sources": ["none", "ai"]}
    with pytest.raises(ValueError, match="none"):
        validate_ppt_spec(spec)
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_schema.py -q`
Expected: FAIL（ModuleNotFoundError）

- [x] **Step 3: 实现**

```python
# example/ppt_master/spec_schema.py
"""generate 模板 spec 契约：页册先行 + 即确认字段（缺省回填原地进行）。

规则（spec §4）：roster/source 必备；其余可选有缺省。校验错误一律携带
字段路径。校验通过后在原 dict 上回填缺省（translator 依赖回填后的值）。
"""

from __future__ import annotations

from typing import Any

_ROLES = {"cover", "content", "divider", "closing"}
_READING_MODES = {"text", "balanced", "presentation"}
_IMAGE_SOURCES = {"ai", "web", "user", "placeholder", "none"}


def _err(path: str, msg: str) -> ValueError:
    return ValueError(f"非法 spec: '{path}' {msg}")


def validate_ppt_spec(spec: dict[str, Any]) -> None:
    if not isinstance(spec, dict):
        raise _err("spec", "应为 dict")
    if not spec.get("project") or not isinstance(spec["project"], str):
        raise _err("project", "缺少非空字符串")

    source = spec.get("source")
    if not isinstance(source, dict):
        raise _err("source", "缺少 dict {kind, paths|topic}")
    kind = source.get("kind")
    if kind == "files":
        paths = source.get("paths")
        if not paths or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise _err("source.paths", "kind=files 时应为非空字符串列表")
    elif kind == "topic":
        if not source.get("topic") or not isinstance(source["topic"], str):
            raise _err("source.topic", "kind=topic 时应为非空字符串")
    else:
        raise _err("source.kind", "应为 'files' | 'topic'")

    roster = spec.get("roster")
    if not roster or not isinstance(roster, list):
        raise _err("roster", "缺少非空列表（显式页册，页册先行）")
    seen: set[str] = set()
    for i, page in enumerate(roster):
        where = f"roster[{i}]"
        if not isinstance(page, dict):
            raise _err(where, "应为 dict")
        pid = page.get("id")
        if not pid or not isinstance(pid, str):
            raise _err(f"{where}.id", "缺少非空字符串")
        if not pid.replace("_", "").isalnum():
            raise _err(f"{where}.id", "仅限字母数字下划线（节点名约束）")
        if pid in seen:
            raise _err(f"{where}.id", f"重复页 id '{pid}'")
        seen.add(pid)
        if not page.get("title") or not isinstance(page["title"], str):
            raise _err(f"{where}.title", "缺少非空字符串")
        role = page.get("role", "content")
        if role not in _ROLES:
            raise _err(f"{where}.role", f"应为 {sorted(_ROLES)} 之一")
        points = page.get("points")
        if points is not None and (
            not isinstance(points, list) or not all(isinstance(p, str) for p in points)
        ):
            raise _err(f"{where}.points", "应为字符串列表（可省略）")

    contract = spec.get("contract")
    if contract is not None:
        if not isinstance(contract, dict):
            raise _err("contract", "应为 dict")
        for k, v in contract.items():
            if not isinstance(v, str):
                raise _err(f"contract.{k}", "应为字符串（空缺 = 规划者裁量）")

    rm = spec.get("reading_mode", "balanced")
    if rm not in _READING_MODES:
        raise _err("reading_mode", f"应为 {sorted(_READING_MODES)} 之一")

    images = spec.get("images")
    if images is not None:
        if not isinstance(images, dict):
            raise _err("images", "应为 dict {sources, notes}")
        sources = images.get("sources")
        if sources is not None:
            if not isinstance(sources, list) or not all(s in _IMAGE_SOURCES for s in sources):
                raise _err("images.sources", f"元素应为 {sorted(_IMAGE_SOURCES)} 之一")
            if "none" in sources and len(sources) > 1:
                raise _err("images.sources", "'none' 不可与其他来源并存")

    prod = spec.get("production")
    if prod is not None:
        if not isinstance(prod, dict):
            raise _err("production", "应为 dict {speaker_notes}")
        sn = prod.get("speaker_notes", True)
        if not isinstance(sn, bool):
            raise _err("production.speaker_notes", "应为 bool")

    template = spec.get("template")
    if template is not None:
        if not isinstance(template, dict) or not isinstance(template.get("roots", []), list):
            raise _err("template", "应为 dict {roots: list}（一期不实现，仅保留）")

    out = spec.get("output")
    if out is not None:
        if not isinstance(out, dict) or not isinstance(out.get("dir", ""), str):
            raise _err("output.dir", "应为字符串路径")

    # ── 缺省回填（校验全过后）──
    spec.setdefault("reading_mode", "balanced")
    spec.setdefault("production", {}).setdefault("speaker_notes", True)
    spec.setdefault("output", {})["dir"] = spec["output"].get("dir") or f"projects/{spec['project']}"
    for page in spec["roster"]:
        page.setdefault("role", "content")
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_ppt_master_schema.py -q`
Expected: PASS（8 项）

- [x] **Step 5: 提交**

```bash
git add example/ppt_master/spec_schema.py example/test_ppt_master_schema.py
git commit -m "feat(ppt_master): spec 契约校验（页册先行 + 缺省回填）"
```

---

### Task 4: prompts 素材落位 + prompts_config.py

**Files:**
- Create: `example/ppt_master/prompts/`（8 个素材文件 + ADAPTATIONS.md）
- Create: `example/ppt_master/prompts_config.py`
- Test: `example/test_ppt_master_prompts.py`

- [x] **Step 1: 拷贝并改编参考素材**

源目录：`$SRC/references/`（ppt-master skill）。逐文件拷贝后**在文件头加同一段改编声明**，并把文中"向用户提问/等待确认"类交互条款按声明的替代理解（这些条款分布稀疏，改编只做标记不重写正文）：

```bash
R=example/ppt_master/prompts
mkdir -p $R
for f in strategist.md plan-core.md executor-base.md shared-standards-core.md \
         semantic-svg.md preset-shape-vocabulary.md executor-notes.md image-base.md; do
  cp "$SRC/references/$f" $R/
done
cp "$SRC/templates/design_spec_reference.md" "$SRC/templates/spec_lock_reference.md" $R/
```

每个文件头部插入（python 单行循环或手动均可）：

```markdown
> **SpecModule 改编声明**：本文为 ppt-master v6.3.2 参考文档原样拷贝（MIT）。
> 在 SpecModule 非交互运行中：所有"⛔ BLOCKING/向用户确认/等待回复"条款按
> "spec 即确认"语义执行——确认契约来自 module spec（已确认值不可改，空缺字段
> 由规划者裁量并记录 provenance）；所有"读取文件路径"按信封 output_dir 解析。
> 改动明细见 prompts/ADAPTATIONS.md。
```

`example/ppt_master/prompts/ADAPTATIONS.md` 首次提交内容：

```markdown
# prompts 改编明细

素材为 ppt-master references/ 原样拷贝（MIT，出处见 vendor/NOTICE）。
每个文件头部有统一改编声明（spec 即确认 / 信封路径解析）。
除此声明外，v1 未改动任何正文。后续若裁剪正文，逐条记录：文件、位置、原因。
```

- [x] **Step 2: 写失败测试**

```python
# example/test_ppt_master_prompts.py
"""参考包组装：文件存在、包内含关键锚点、HarnessConfig 形状。"""

from __future__ import annotations

from example.ppt_master import prompts_config as pc


def test_prompt_assets_present():
    for name in pc.REQUIRED_ASSETS:
        assert (pc.PROMPTS_DIR / name).is_file(), name


def test_plan_pack_contains_anchors():
    pack = pc.plan_prompt_pack()
    assert "design_spec" in pack and "spec_lock" in pack
    assert "SpecModule 改编声明" in pack


def test_page_pack_contains_vocab():
    pack = pc.page_prompt_pack()
    assert "SVG" in pack
    assert len(pack) > 5000  # 词表 + 契约非占位


def test_configs_shape():
    for cfg in (pc.plan_config(), pc.page_config(), pc.repair_config(),
                pc.notes_config(), pc.research_config()):
        assert cfg.prompt_core
        assert cfg.mode == "text"
        assert cfg.prompt_modes == {}  # v1 层 2 预留
    # 图像配置：image 模式互斥断言
    img = pc.image_config(image_dir="x")
    assert img.mode == "image" and img.output_format is None
```

- [x] **Step 3: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_prompts.py -q`
Expected: FAIL

- [x] **Step 4: 实现 prompts_config.py**

```python
# example/ppt_master/prompts_config.py
"""5 类 LLM 节点的 HarnessConfig + 参考包组装。

Layer 1（prompt_core）= 角色纪律 + {占位符}；Layer 2 v1 预留（prompt_modes
恒空，不用 promptmode 即无 KeyError 面）；Layer 3（prompt_extra）= 参考包
（prompts/ 素材拼接，调用时读文件）。参考包是确定性拼装，非 prompt 工程。
"""

from __future__ import annotations

from pathlib import Path

from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

REQUIRED_ASSETS = (
    "strategist.md", "plan-core.md", "executor-base.md",
    "shared-standards-core.md", "semantic-svg.md",
    "preset-shape-vocabulary.md", "executor-notes.md", "image-base.md",
    "design_spec_reference.md", "spec_lock_reference.md",
)


def _asset(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _pack(*names: str) -> str:
    return "\n\n".join(f"## 参考：{n}\n\n{_asset(n)}" for n in names)


def plan_prompt_pack() -> str:
    return _pack("strategist.md", "plan-core.md",
                 "design_spec_reference.md", "spec_lock_reference.md")


def page_prompt_pack() -> str:
    return _pack("executor-base.md", "shared-standards-core.md",
                 "semantic-svg.md", "preset-shape-vocabulary.md")


def repair_prompt_pack() -> str:
    return _pack("executor-base.md", "shared-standards-core.md")


def notes_prompt_pack() -> str:
    return _pack("executor-notes.md")


_PLAN_CORE = (
    "你是演示文稿规划者（Strategist）。依据已确认契约（spec）与源事实，"
    "一次性产出完整 design_spec.md 与 spec_lock.md（语法见参考包），"
    "并以 JSON 收据返回。硬规则：页册神圣——不得增删页、改序、改标题；"
    "spec 已确认字段为 Literal/Semantic 要求逐字保留；空缺字段你裁量并在"
    "收据 provenance 标 planner-decided。\n"
    "收据 JSON：{status, roster_ids, design_spec_md, spec_lock_md, "
    "image_rows, icon_pool, notes_enabled}\n"
    "占位符 —— 契约：{contract}；页册：{roster}；源摘要：{source_digest}"
)

_PAGE_CORE = (
    "你是演示文稿页面作者（Executor）。依据页面任务书产出**一个完整 SVG 页**"
    "（1280x720 viewBox），只输出 SVG 本体，无任何包裹文字。\n"
    "占位符 —— 页块：{page}；执行锁：{lock}；校准表：{calibration}"
)

_REPAIR_CORE = (
    "你是页面修复者。依据质量门报告修复指定页，重写完整 SVG，只输出 SVG 本体。"
    "修在拥有层：页内错修页；同类问题跨页复现（方法级偏差）则在修复说明中指出"
    "应改校准/规则。\n占位符 —— 页块：{page}；锁：{lock}；校准表：{calibration}；"
    "问题清单：{issues}"
)

_NOTES_CORE = (
    "你是讲者备注作者。基于最终页 SVG 逐页写备注，输出 total.md 全文"
    "（含每页 `# <页id>` 小节）。占位符 —— 页册：{roster}；页 SVG 摘要：{pages_digest}"
)

_RESEARCH_CORE = (
    "你是事实研究员。只针对既述信息缺口研究并输出 JSON："
    "{research_md, facts}。facts 为 [{id, claim, url}]。不得编造可核查断言。"
    "占位符 —— 主题：{topic}；缺口：{gaps}"
)


def plan_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_plan", prompt_core=_PLAN_CORE,
        output_format=OutputFormat(type="json_object"),
        notdo=["增删或重排页册", "改页标题", "虚构可核查事实"],
    )


def page_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_page", prompt_core=_PAGE_CORE,
        output_format=OutputFormat(type="text"),
        notdo=["输出非 SVG 内容", "引入锁外新色/新字体"],
    )


def repair_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_repair", prompt_core=_REPAIR_CORE,
        output_format=OutputFormat(type="text"),
        notdo=["改页册结构", "输出非 SVG 内容"],
    )


def notes_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_notes", prompt_core=_NOTES_CORE,
        output_format=OutputFormat(type="text"),
    )


def research_config() -> HarnessConfig:
    return HarnessConfig(
        name="ppt_research", prompt_core=_RESEARCH_CORE,
        output_format=OutputFormat(type="json_object"),
    )


def image_config(image_dir: str) -> HarnessConfig:
    return HarnessConfig(
        name="ppt_image", prompt_core="{image_prompt}", mode="image",
        image_dir=image_dir,
    )
```

- [x] **Step 5: 跑测试确认通过**

Run: `python -m pytest example/test_ppt_master_prompts.py -q`
Expected: PASS（4 项）

- [x] **Step 6: 提交**

```bash
git add example/ppt_master/prompts example/ppt_master/prompts_config.py example/test_ppt_master_prompts.py
git commit -m "feat(ppt_master): 参考素材落位 + 5 类 harness 配置与参考包组装"
```

---

### Task 5: llm_nodes.py —— call_harness 包裹节点工厂

所有触 LLM 节点 = **script**（注册工厂闭包，捕获 llm_client），失败收据语义在此落地。

**Files:**
- Create: `example/ppt_master/llm_nodes.py`
- Test: `example/test_ppt_master_nodes.py`

- [x] **Step 1: 写失败测试**

```python
# example/test_ppt_master_nodes.py
"""LLM 节点工厂：成功收据 / 失败收据（AND 不饿死）/ 修复轮上限。"""

from __future__ import annotations

import json
import pytest

from example.ppt_master import workspace
from example.ppt_master.llm_nodes import (
    make_page_node, make_plan_node, make_repair_node,
)


class FakeResult:
    def __init__(self, value): self.value = value


class OkClient:
    """prompt 含 'PLANNING' 返回计划收据 JSON，否则返回合格 SVG。"""

    def __init__(self, svg): self.svg = svg

    async def complete(self, **kw):
        from llm.client import LLMResponse
        prompt = kw.get("prompt", "")
        if "PLANNING" in prompt:
            receipt = {
                "status": "ok", "roster_ids": ["p01", "p02"],
                "design_spec_md": "# spec\n§IX\n## p01 封面\nAudience move: 建立信任\n## p02 背景\nAudience move: 给出动机\n",
                "spec_lock_md": "# lock\npalette: #000000\n",
                "image_rows": [], "icon_pool": [],
                "notes_enabled": True,
            }
            return LLMResponse(content=json.dumps(receipt))
        return LLMResponse(content=self.svg)


class BoomClient(OkClient):
    async def complete(self, **kw):
        raise RuntimeError("network down")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    root = workspace.init_workspace(tmp_path / "deck")
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": [{"id": "p01", "title": "封面", "role": "cover"},
                   {"id": "p02", "title": "背景", "role": "content"}],
    })
    return root


class _View:
    def __init__(self, node, fields): self._f = fields; self.node = node
    def field(self, k): return self._f[k]


def test_plan_node_writes_files_and_receipt(env):
    node = make_plan_node(OkClient("<svg/>"))
    view = _View("Plan", {"source": {"digest": "论文内容"}})
    out = _run(node(view))
    assert out["status"] == "ok"
    assert (env / "design_spec.md").exists() and (env / "spec_lock.md").exists()
    assert out["roster_ids"] == ["p01", "p02"]


def test_page_node_receipt_ok_and_failed(env, tmp_path):
    svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1280 720'></svg>"
    page = make_page_node(OkClient(svg))
    view = _View("P01", {"plan": {"status": "ok"}, "calibration": "{}"})
    out = _run(page(view))
    assert out["status"] == "ok"
    assert (env / "svg_output" / "p01.svg").exists()

    # LLM 挂 → 失败收据（不是 Failure！AND join 不饿死）
    bad = make_page_node(BoomClient(""))
    out2 = _run(bad(view))
    assert out2["status"] == "failed" and "network down" in out2["error"]


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_repair_node_round_limit(env):
    repair = make_repair_node(OkClient("<svg/>"), max_rounds=2)
    verdict = {"ok": False, "issues": [{"page": "p01", "message": "溢出"}]}
    view = _View("FinalRepair", {"verdict": verdict, "calibration": "{}"})
    rounds = env / "validation" / "repair_rounds.json"
    rounds.write_text(json.dumps({"final": 2}), encoding="utf-8")
    from tickflow import Failure
    out = _run(repair(view))
    assert isinstance(out, Failure) and out.type == "infrastructure"
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_nodes.py -q`
Expected: FAIL（ModuleNotFoundError: llm_nodes）

- [x] **Step 3: 实现 llm_nodes.py**

```python
# example/ppt_master/llm_nodes.py
"""LLM 节点工厂：call_harness 包裹 → 注册为 script。

为什么是 script 不是裸 harness：tickflow 中 body 返回 Failure 会让下游
AND join 永不点火（饥饿）。页/规划节点必须"永不 Failure"——LLM 错误降级
为失败收据 dict，由质量门/校验节点判定。工厂捕获 llm_client（注册期闭包），
避免模块级全局（多 Module 同进程 namespace 隔离，架构规则 4）。
"""

from __future__ import annotations

import json
from typing import Any

from tickflow import Failure

from module_harness.core.call import HarnessCallError, call_harness

from . import prompts_config as pc
from . import workspace


def _write_text(path: Any, text: str) -> None:
    Path = type(workspace.MODULE_DIR)  # pathlib.Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def make_plan_node(llm_client: Any, event_bus: Any = None):
    """规划节点：契约+源事实 → design_spec.md + spec_lock.md + 收据。"""

    async def plan_node(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        try:
            result = await call_harness(
                pc.plan_config(),
                {
                    "contract": json.dumps(env.get("spec", {}).get("contract", {}), ensure_ascii=False),
                    "roster": json.dumps(env["roster"], ensure_ascii=False),
                    "source_digest": json.dumps(view.field("source"), ensure_ascii=False),
                },
                llm_client=llm_client,
                prompt_extra=pc.plan_prompt_pack(),
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "error": str(e)}
        receipt = result.value if isinstance(result.value, dict) else json.loads(result.value)
        root = env["output_dir"]
        _write_text(f"{root}/design_spec.md", receipt.get("design_spec_md", ""))
        _write_text(f"{root}/spec_lock.md", receipt.get("spec_lock_md", ""))
        return receipt

    return plan_node


def make_research_node(llm_client: Any, event_bus: Any = None):
    """研究节点（topic-only）：缺口研究 → sources/research.md + facts。"""

    async def research_node(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        try:
            result = await call_harness(
                pc.research_config(),
                {
                    "topic": env.get("spec", {}).get("source", {}).get("topic", ""),
                    "gaps": "（topic-only：整题研究）",
                },
                llm_client=llm_client,
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "error": str(e)}
        val = result.value if isinstance(result.value, dict) else json.loads(result.value)
        root = env["output_dir"]
        _write_text(f"{root}/sources/research.md", val.get("research_md", ""))
        _write_text(f"{root}/sources/facts.json", json.dumps(val.get("facts", []), ensure_ascii=False))
        return {"status": "ok", "digest": val.get("research_md", "")[:4000], "topic_researched": True}

    return research_node


def make_page_node(llm_client: Any, event_bus: Any = None):
    """页节点：节点名即页 id（P01→p01），输出 SVG 落盘 svg_output/。"""

    async def page_node(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        page_id = str(view.node)[1:].lower()  # "P01" -> "p01"
        page = next((p for p in env["roster"] if p["id"] == page_id), None)
        if page is None:
            return {"status": "failed", "error": f"页册中无 id '{page_id}'"}
        try:
            result = await call_harness(
                pc.page_config(),
                {
                    "page": json.dumps(page, ensure_ascii=False),
                    "lock": (workspace.Path(env["output_dir"]) / "spec_lock.md").read_text(encoding="utf-8"),
                    "calibration": json.dumps(view.field("calibration"), ensure_ascii=False),
                },
                llm_client=llm_client,
                prompt_extra=pc.page_prompt_pack(),
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "page": page_id, "error": str(e)}
        svg = result.value
        if not isinstance(svg, str) or "<svg" not in svg:
            return {"status": "failed", "page": page_id, "error": "输出非 SVG"}
        out = workspace.Path(env["output_dir"]) / "svg_output" / f"{page_id}.svg"
        out.write_text(svg, encoding="utf-8")
        return {"status": "ok", "page": page_id, "file": str(out)}

    return page_node


def make_repair_node(llm_client: Any, event_bus: Any = None, max_rounds: int = 2):
    """修复节点（Early/Final 共用，按 view.node 区分 stage）。

    轮次上限：validation/repair_rounds.json 计数（{early: n, final: n}），
    超限 → Failure(infrastructure) 停图（非交互环境无人工兜底）。
    """

    async def repair_node(view) -> dict[str, Any]:
        stage = "early" if str(view.node).startswith("Early") else "final"
        env = workspace.read_envelope()
        root = workspace.Path(env["output_dir"])
        rounds_file = root / "validation" / "repair_rounds.json"
        rounds: dict = {}
        if rounds_file.exists():
            rounds = json.loads(rounds_file.read_text(encoding="utf-8"))
        rounds[stage] = rounds.get(stage, 0) + 1
        rounds_file.write_text(json.dumps(rounds), encoding="utf-8")
        if rounds[stage] > max_rounds:
            return Failure(
                f"{stage} 修复轮超过上限 {max_rounds}", type="infrastructure"
            )

        verdict = view.field("verdict")
        issues = [i for i in verdict.get("issues", []) if i.get("page")]
        by_page: dict[str, list] = {}
        for i in issues:
            by_page.setdefault(i["page"], []).append(i)
        repaired, failed = [], []
        for page_id, page_issues in by_page.items():
            page = next((p for p in env["roster"] if p["id"] == page_id), None)
            if page is None:
                failed.append({"page": page_id, "error": "页册无此 id"})
                continue
            try:
                result = await call_harness(
                    pc.repair_config(),
                    {
                        "page": json.dumps(page, ensure_ascii=False),
                        "lock": (root / "spec_lock.md").read_text(encoding="utf-8"),
                        "calibration": json.dumps(view.field("calibration"), ensure_ascii=False),
                        "issues": json.dumps(page_issues, ensure_ascii=False),
                    },
                    llm_client=llm_client,
                    prompt_extra=pc.repair_prompt_pack(),
                    event_bus=event_bus,
                )
            except HarnessCallError as e:
                failed.append({"page": page_id, "error": str(e)})
                continue
            svg = result.value
            if not isinstance(svg, str) or "<svg" not in svg:
                failed.append({"page": page_id, "error": "修复输出非 SVG"})
                continue
            (root / "svg_output" / f"{page_id}.svg").write_text(svg, encoding="utf-8")
            repaired.append(page_id)
        return {"status": "ok", "stage": stage, "round": rounds[stage],
                "repaired": repaired, "failed": failed}

    return repair_node


def make_notes_node(llm_client: Any, event_bus: Any = None):
    """备注节点：最终页 SVG → notes/total.md。"""

    async def notes_node(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        root = workspace.Path(env["output_dir"])
        digest = []
        for p in env["roster"]:
            f = root / "svg_output" / f"{p['id']}.svg"
            digest.append({"id": p["id"], "svg_head": f.read_text(encoding="utf-8")[:1200] if f.exists() else ""})
        try:
            result = await call_harness(
                pc.notes_config(),
                {
                    "roster": json.dumps(env["roster"], ensure_ascii=False),
                    "pages_digest": json.dumps(digest, ensure_ascii=False),
                },
                llm_client=llm_client,
                prompt_extra=pc.notes_prompt_pack(),
                event_bus=event_bus,
            )
        except HarnessCallError as e:
            return {"status": "failed", "error": str(e)}
        (root / "notes" / "total.md").write_text(result.value, encoding="utf-8")
        return {"status": "ok", "file": str(root / "notes" / "total.md")}

    return notes_node


def make_image_node(llm_client: Any, event_bus: Any = None):
    """AI 图像行获取：§VIII 行 prompt → harness 图像模式 → images/。

    图像 harness 失败（infrastructure Failure）在 call_harness 侧抛
    HarnessCallError → 行标 failed（Needs-Manual 语义），不中断运行。
    """

    async def image_node(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        root = workspace.Path(env["output_dir"])
        rows = (view.field("plan") or {}).get("image_rows") or []
        out_rows = []
        for row in rows:
            if row.get("acquire") != "ai":
                out_rows.append(row)
                continue
            try:
                result = await call_harness(
                    pc.image_config(image_dir=str(root / "images")),
                    {"image_prompt": row.get("prompt", "")},
                    llm_client=llm_client,
                    event_bus=event_bus,
                )
                row = {**row, "status": "terminal", "file": result.value}
            except HarnessCallError as e:
                row = {**row, "status": "Needs-Manual", "error": str(e)}
            out_rows.append(row)
        return {"status": "ok", "rows": out_rows}

    return image_node
```

注意：`llm_nodes.py` 顶部 `from pathlib import Path` 直接导入（把 `_write_text` 里那行 `Path = type(...)` 换成正常 `Path`——实现时用 `from pathlib import Path`，不要用上面的取巧写法）。实施时以上述说明为准修正 `_write_text`：

```python
from pathlib import Path

def _write_text(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
```

并把函数体内的 `workspace.Path` 全部改为直接用的 `Path`。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_ppt_master_nodes.py -q`
Expected: PASS（4 项）

- [x] **Step 5: 提交**

```bash
git add example/ppt_master/llm_nodes.py example/test_ppt_master_nodes.py
git commit -m "feat(ppt_master): LLM 节点工厂（失败收据 + 修复轮上限 + 图像行获取）"
```

---

### Task 6: tools_nodes.py + tools/run_tool.py —— 确定性节点与 command 入口

**Files:**
- Create: `example/ppt_master/tools_nodes.py`
- Create: `example/ppt_master/tools/run_tool.py`
- Test: `example/test_ppt_master_tools.py`

- [x] **Step 1: 写失败测试**

```python
# example/test_ppt_master_tools.py
"""确定性节点：ingest/plan_validate/门裁决/readiness/report；run_tool 透传。"""

from __future__ import annotations

import json
import subprocess
import sys

from example.ppt_master import workspace


class _View:
    def __init__(self, node, fields): self._f = fields; self.node = node
    def field(self, k): return self._f[k]


def _env(tmp_path, monkeypatch, roster=None, sources=None):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    root = workspace.init_workspace(tmp_path / "deck")
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": roster or [{"id": "p01", "title": "封面", "role": "cover"}],
        "sources": sources or [],
    })
    return root


def test_ingest_copies_md_and_digests(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import ingest
    src = tmp_path / "paper.md"
    src.write_text("# 论文\n" + "内容" * 100, encoding="utf-8")
    _env(tmp_path, monkeypatch, sources=[str(src)])
    out = _run(ingest(_View("Ingest", {})))
    assert out["status"] == "ok" and out["digest"]
    assert (tmp_path / "deck" / "sources" / "paper.md").exists()


def test_plan_validate_catches_roster_drift(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import plan_validate
    root = _env(tmp_path, monkeypatch)
    # design_spec 页册漂移：缺 p02
    (root / "design_spec.md").write_text(
        "# spec\n## p01 封面\nAudience move: 建立信任\n", encoding="utf-8")
    (root / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    from tickflow import Failure
    out = _run(plan_validate(_View("PlanValidate", {"plan": {"roster_ids": ["p01"]}})))
    assert isinstance(out, Failure) and out.type == "infrastructure"


def test_plan_validate_passes(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import plan_validate
    root = _env(tmp_path, monkeypatch, roster=[
        {"id": "p01", "title": "封面", "role": "cover"},
        {"id": "p02", "title": "背景", "role": "content"},
    ])
    (root / "design_spec.md").write_text(
        "# spec\n## p01 封面\nAudience move: 建立信任\n"
        "## p02 背景\nAudience move: 给出动机\n", encoding="utf-8")
    (root / "spec_lock.md").write_text("# lock\npalette: #000000\n", encoding="utf-8")
    out = _run(plan_validate(_View("PlanValidate", {"plan": {"roster_ids": ["p01", "p02"]}})))
    assert out == {"status": "ok"}


def test_gate_verdict_parses_blocking(tmp_path, monkeypatch):
    from example.ppt_master.tools_nodes import make_gate_verdict
    root = _env(tmp_path, monkeypatch)
    report = {"categories": {"blocking": {"issues": [
        {"page": "p01", "message": "文本溢出"}]}}}
    workspace.gate_report_path(root, "final").write_text(
        json.dumps(report), encoding="utf-8")
    verdict = make_gate_verdict("final")
    out = verdict(_View("FinalVerdict", {"gate": {"returncode": 1}}))
    assert out["ok"] is False and out["issues"][0]["page"] == "p01"


def _run(coro):
    import asyncio
    return asyncio.run(coro) if coro is not None else None
```

同步节点（ingest/plan_validate/gate_verdict）实现为**同步函数**（registry 支持 sync script），`_run` 直接返回值即可——实现时把测试里 `_run(x)` 调用改成直呼 `x(...)` 亦可，二选一保持一致。下文 Step 3 按 sync 实现，测试 `_run` 相应简化：

```python
def test_ingest_copies_md_and_digests(...):
    out = ingest(_View("Ingest", {}))   # sync 直呼
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_tools.py -q`
Expected: FAIL

- [x] **Step 3: 实现 tools_nodes.py 与 tools/run_tool.py**

```python
# example/ppt_master/tools_nodes.py
"""确定性 script 节点：ingest / init / plan_validate / 门裁决 / readiness / report。

校验是 Gate 1/2 的机械化子集（spec §5）：页册保真、Audience move 存在、
锁锚点存在。schema 合法 ≠ 保真；保真以页册与必需要素为准，违反即
infrastructure Failure（硬合规 fail-fast）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tickflow import Failure

from . import workspace

_MD_EXTS = {".md", ".markdown", ".txt", ".csv", ".tsv"}


def ingest(view) -> dict[str, Any]:
    """files → sources/ 拷贝 + 摘要（topic-only 由 Research 节点供摘要）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    sources = env.get("sources") or []
    digest_parts, copied = [], []
    for s in sources:
        p = Path(s)
        if not p.exists():
            digest_parts.append(f"### {p.name}（源缺失，契约驱动）")
            continue
        if p.suffix.lower() in _MD_EXTS:
            dest = root / "sources" / p.name
            shutil.copy2(p, dest)
            text = p.read_text(encoding="utf-8", errors="replace")
            digest_parts.append(f"### {p.name}\n{text[:6000]}")
            copied.append(str(dest))
        else:
            r = subprocess.run(
                [sys.executable, str(workspace.VENDOR_SCRIPTS / "source_to_md.py"), str(p),
                 "-o", str(root / "sources")],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
            )
            if r.returncode != 0:
                return {"status": "failed", "error": f"source_to_md 失败: {p.name}: {r.stderr[-500:]}"}
            digest_parts.append(f"### {p.name}（已转换，见 sources/）")
            copied.append(str(p))
    return {"status": "ok", "copied": copied,
            "digest": "\n".join(digest_parts)[:20000] or "（无源文件，仅契约）"}


def init(view) -> dict[str, Any]:
    env = workspace.read_envelope()
    workspace.init_workspace(env["output_dir"])
    workspace.append_workflow_log(env["output_dir"], "workspace 初始化（Init 节点）")
    return {"status": "ok", "output_dir": env["output_dir"]}


_AUDIENCE_MOVE = re.compile(r"Audience move", re.IGNORECASE)


def plan_validate(view) -> dict[str, Any]:
    """Gate 1/2 机械化：页册保真 + 每页 Audience move + 锁锚点。违反即停图。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    spec_md = (root / "design_spec.md").read_text(encoding="utf-8")
    lock_md = (root / "spec_lock.md").read_text(encoding="utf-8")

    errors: list[str] = []
    roster = env["roster"]
    plan = view.field("plan") or {}
    if plan.get("roster_ids") != [p["id"] for p in roster]:
        errors.append(f"收据页册 {plan.get('roster_ids')} != spec 页册")

    for page in roster:
        block = re.search(
            rf"##\s+{re.escape(page['id'])}\b(.*?)(?=\n## |\Z)", spec_md, re.S)
        if block is None:
            errors.append(f"design_spec 缺页块 '{page['id']}'")
        elif not _AUDIENCE_MOVE.search(block.group(1)):
            errors.append(f"页 '{page['id']}' 缺 Audience move")
    for anchor in ("palette", "typography"):
        if anchor not in lock_md:
            errors.append(f"spec_lock 缺锚点节 '{anchor}'")

    if errors:
        return Failure("规划校验失败（硬合规）:\n" + "\n".join(errors),
                       type="infrastructure")
    return {"status": "ok"}


def make_gate_verdict(stage: str):
    """门裁决 script：读 validation 报告 → 结构化 verdict（guard 消费）。"""

    def verdict(view) -> dict[str, Any]:
        env = workspace.read_envelope()
        gate = view.field("gate") or {}
        report_file = workspace.gate_report_path(env["output_dir"], stage)
        issues: list[dict] = []
        if report_file.exists():
            data = json.loads(report_file.read_text(encoding="utf-8"))
            issues = data.get("categories", {}).get("blocking", {}).get("issues", []) or []
        return {"stage": stage, "returncode": gate.get("returncode"),
                "ok": not issues, "issues": issues}

    return verdict


def make_guard(stage: str, want_clean: bool):
    """守卫工厂：读源任务 inputs 键 'gate' 的输出并复检报告文件。

    与 ppt_writer guard 同机制：guard 读守卫边源任务的 inputs 键
    （此处 'gate' = 门 command 节点输出 {stdout,stderr,returncode}）。
    """
    def guard(view) -> bool:
        env = workspace.read_envelope()
        report_file = workspace.gate_report_path(env["output_dir"], stage)
        has_issues = False
        if report_file.exists():
            data = json.loads(report_file.read_text(encoding="utf-8"))
            has_issues = bool(data.get("categories", {}).get("blocking", {}).get("issues"))
        return (not has_issues) if want_clean else has_issues
    return guard


def calibrate(view) -> dict[str, Any]:
    """text_measure calibrate → validation/text_calibration.json（全体页节点共享）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    r = subprocess.run(
        [sys.executable, str(workspace.VENDOR_SCRIPTS / "text_measure.py"),
         "calibrate", str(root), "--outline"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    cal_file = root / "validation" / "text_calibration.json"
    return {"status": "ok" if cal_file.exists() else "empty",
            "returncode": r.returncode,
            "calibration": cal_file.read_text(encoding="utf-8") if cal_file.exists() else "{}"}


def icon_sync_node(view) -> dict[str, Any]:
    """图标池物化（池来自 Plan 收据；空池 no-op）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    pool = (view.field("plan") or {}).get("icon_pool") or []
    if not pool:
        return {"status": "ok", "synced": 0}
    r = subprocess.run(
        [sys.executable, str(workspace.VENDOR_SCRIPTS / "icon_sync.py"), str(root), *pool],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if r.returncode != 0:
        return Failure(f"icon_sync 失败: {r.stderr[-500:]}", type="llm")
    return {"status": "ok", "synced": len(pool)}


def image_readiness(view) -> dict[str, Any]:
    """Needs-Manual 行必须已有真实文件，否则 fail-fast 列名（Step 7 readiness 门）。"""
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    rows = (view.field("rows") or {}).get("rows") or []
    missing = []
    for row in rows:
        if row.get("status") == "Needs-Manual":
            f = row.get("file")
            if not f or not (root / "images" / Path(f).name).exists():
                missing.append(row.get("name") or f or row.get("id", "?"))
    if missing:
        return Failure("图像就绪门：以下行缺文件：" + ", ".join(missing),
                       type="infrastructure")
    return {"status": "ready"}


def report(view) -> dict[str, Any]:
    export = view.field("export") or {}
    finalize = view.field("finalize") or {}
    env = workspace.read_envelope()
    root = Path(env["output_dir"])
    pptx_files = sorted(str(p) for p in (root / "exports").glob("*.pptx"))
    ok = export.get("returncode") == 0 and bool(pptx_files)
    return {"status": "ok" if ok else "error",
            "pptx": pptx_files, "export_returncode": export.get("returncode"),
            "finalize_returncode": (finalize or {}).get("returncode"),
            "message": "导出完成" if ok else "导出失败（见 exports/ 与 validation/）"}
```

`example/ppt_master/tools/run_tool.py`（command 静态串的入口；无业务逻辑）：

```python
# example/ppt_master/tools/run_tool.py
"""vendor 工具命令入口：读信封取 output_dir，透传调用 vendor 脚本。

用法（命令串由 CommandConfig 静态生成）：
  python run_tool.py --envelope <path> --tool svg_quality_checker.py \
      -- --stage final --canonical-authoring --json
退出码原样透传（checker/exporter 的 0/非 0 语义即门语义）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("args", nargs="*", default=[])
    ns = parser.parse_args(argv)

    envelope = json.loads(Path(ns.envelope).read_text(encoding="utf-8"))
    root = envelope["output_dir"]
    tool = Path(__file__).resolve().parent.parent / "vendor" / "ppt_master" / "scripts" / ns.tool
    cmd = [sys.executable, str(tool), root, *ns.args]
    result = subprocess.run(cmd, encoding="utf-8", errors="replace")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
```

注意 `nargs="*"` 与位置参数混用时 `--` 分隔符的解析：argparse 里 `parser.add_argument("args", nargs="*")` 支持 `-- --stage final` 形式。若实测解析有误（argparse 版本差异），改为 `parser.parse_known_args()` 并把未知项拼进 args——两种写法选实测通过的，在测试中固定。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_ppt_master_tools.py -q`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add example/ppt_master/tools_nodes.py example/ppt_master/tools/run_tool.py example/test_ppt_master_tools.py
git commit -m "feat(ppt_master): 确定性节点（门裁决/守卫/校验/就绪门）+ run_tool 命令入口"
```

---

### Task 7: translator.py —— 动态图展开

**Files:**
- Create: `example/ppt_master/translator.py`
- Test: `example/test_ppt_master_translator.py`

- [x] **Step 1: 写失败测试**

```python
# example/test_ppt_master_translator.py
"""翻译器展开规则：页节点数、批次门、条件段、≤6 无早门、命令串形状。"""

from __future__ import annotations

import pytest

from example.ppt_master import workspace
from example.ppt_master.translator import build_generate_tasklist


def _spec(n: int, **kw) -> dict:
    spec = {
        "project": "t",
        "source": {"kind": "files", "paths": ["a.md"]},
        "roster": [{"id": f"p{i:02d}", "title": f"页{i}"} for i in range(1, n + 1)],
    }
    spec.update(kw)
    return spec


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)


def test_four_pages_no_early_gate(tmp_path):
    tasks, flow = build_generate_tasklist(_spec(4))
    for i in range(1, 5):
        assert f"P{i:02d}" in tasks
    assert "EarlyGate" not in tasks
    assert "FinalGate" in tasks and "Repair" in tasks
    assert "NotesGen" in tasks  # notes 缺省开
    assert "SplitNotes" in tasks and "Export" in tasks
    assert "ImageAcquire" not in tasks  # 无 images 声明


def test_ten_pages_has_early_gate_and_batching(tmp_path):
    tasks, flow = build_generate_tasklist(_spec(10))
    assert "EarlyGate" in tasks and "EarlyRepair" in tasks
    # 批1 = P01..P05 直连 EarlyGate；P06..P10 与 EarlyRepair 汇入 FinalGate
    assert "P05 --> EarlyGate" in flow
    assert "EarlyGate --> EarlyVerdict" in flow
    assert "EarlyRepair --> FinalGate" in flow
    assert "P10 --> FinalGate" in flow


def test_six_pages_no_early_gate_seven_has(tmp_path):
    assert "EarlyGate" not in build_generate_tasklist(_spec(6))[0]
    assert "EarlyGate" in build_generate_tasklist(_spec(7))[0]


def test_images_branch_and_notes_off(tmp_path):
    spec = _spec(3, images={"sources": ["ai"]},
                 production={"speaker_notes": False})
    tasks, flow = build_generate_tasklist(spec)
    assert "ImageAcquire" in tasks and "IconSync" in tasks
    assert "ImageReadiness" in tasks
    assert "NotesGen" not in tasks and "SplitNotes" not in tasks
    # notes 关 → export 命令带 --no-notes
    assert "--no-notes" in tasks["Export"]["command"]


def test_topic_source_uses_research_node(tmp_path):
    tasks, _ = build_generate_tasklist(_spec(2, source={"kind": "topic", "topic": "量子计算"}))
    assert "Research" in tasks and "Ingest" not in tasks


def test_tl_generate_writes_envelope_and_returns_tasks(tmp_path, monkeypatch):
    from example.ppt_master.translator import tl_generate
    from tickflow.views import DictView
    spec = _spec(2)
    # 翻译器视图：v.field("spec") 取 spec dict（translator.py 合成视图供数）
    out = tl_generate(DictView({"spec": spec}))
    assert "Tasks" in out and "Flow" in out
    envelope = workspace.read_envelope()
    assert envelope["roster"][0]["id"] == "p01"
    assert (tmp_path / "projects" / "t").is_dir()
```

（`DictView` 若与翻译器取值方式不合，以 `module_harness/model/translator.py::_translator_view` 的合成视图为准——测试里复用该函数构造视图：`from module_harness.model.translator import _translator_view; view = _translator_view({"spec": spec}, "__translator__")`。实现时择一，测试与实现保持一致。）

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_translator.py -q`
Expected: FAIL

- [x] **Step 3: 实现 translator.py**

```python
# example/ppt_master/translator.py
"""generate 模板翻译器：spec → 信封 + 完整 tasklist（动态展开）。

页册先行：roster 静态展开为 N 个页节点；批1 = min(5, N) 过 EarlyGate
（N ≤ 6 无早门，批2 页从 EarlyDispatch 扇出）；条件段按 spec 声明
生成/省略。命令串静态，动态路径经信封（workspace._envelope_path()
字面量嵌入）。守卫环：FinalVerdict --|final_errors|--> Repair --> FinalGate
（环上含守卫边，满足 tickflow 环约束）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import spec_schema, workspace

_RUN_TOOL = Path(__file__).resolve().parent / "tools" / "run_tool.py"


def _cmd(tool: str, *tool_args: str) -> str:
    args = " ".join(tool_args)
    suffix = f" -- {args}" if args else ""
    return (f'"{sys.executable}" "{_RUN_TOOL}" '
            f'--envelope "{workspace._envelope_path()}" --tool {tool}{suffix}')


def build_generate_tasklist(
    spec: dict[str, Any],
) -> tuple[dict[str, dict], str]:
    """返回 (Tasks dict, Flow str)。spec 须已过 validate_ppt_spec（含回填）。"""
    roster = spec["roster"]
    n = len(roster)
    batch1 = min(5, n)
    notes_on = spec["production"]["speaker_notes"]
    img_sources = [s for s in (spec.get("images") or {}).get("sources") or []
                   if s != "none"]
    has_images = bool(img_sources)
    topic = spec["source"]["kind"] == "topic"
    early = n > 6
    gate1 = "EarlyGate" if early else "FinalGate"

    tasks: dict[str, dict] = {}
    flow: list[str] = []

    # ── 头段：源 → Init → Plan → PlanValidate ──
    if topic:
        tasks["Research"] = {"type": "script", "script": "research_node"}
        tasks["Init"] = {"type": "script", "script": "init",
                         "inputs": {"source": "Research"}}
        flow.append("[Research] --> Init")
        plan_src = "Research"
    else:
        tasks["Ingest"] = {"type": "script", "script": "ingest"}
        tasks["Init"] = {"type": "script", "script": "init",
                         "inputs": {"source": "Ingest"}}
        flow.append("[Ingest] --> Init")
        plan_src = "Ingest"
    tasks["Plan"] = {"type": "script", "script": "plan_node",
                     "inputs": {"source": plan_src}}
    tasks["PlanValidate"] = {"type": "script", "script": "plan_validate",
                             "inputs": {"plan": "Plan"}}
    flow.append("Init --> Plan")
    flow.append("Plan --> PlanValidate")

    # ── 资源段：IconSync → (ImageAcquire) → Calibrate ──
    tasks["IconSync"] = {"type": "script", "script": "icon_sync_node",
                         "inputs": {"plan": "Plan"}}
    flow.append("PlanValidate --> IconSync")
    prev = "IconSync"
    if has_images:
        tasks["ImageAcquire"] = {"type": "script", "script": "image_acquire",
                                 "inputs": {"plan": "Plan"}}
        flow.append(f"{prev} --> ImageAcquire")
        prev = "ImageAcquire"
    tasks["Calibrate"] = {"type": "script", "script": "calibrate"}
    flow.append(f"{prev} --> Calibrate")

    # ── 页段：批1（P01..P05）→ 门1；早门段守卫分流 ──
    for i in range(1, batch1 + 1):
        tasks[f"P{i:02d}"] = {"type": "script", "script": "page_node",
                              "inputs": {"plan": "Plan", "calibration": "Calibrate"}}
        flow.append(f"Calibrate --> P{i:02d}")
        flow.append(f"P{i:02d} --> {gate1}")

    if early:
        tasks["EarlyGate"] = {"type": "command",
                              "command": _cmd("svg_quality_checker.py",
                                              "--stage early --canonical-authoring --json"),
                              "timeout": 600.0}
        tasks["EarlyVerdict"] = {"type": "script", "script": "early_verdict",
                                 "inputs": {"gate": "EarlyGate"}}
        tasks["EarlyRepair"] = {"type": "script", "script": "repair_node",
                                "inputs": {"verdict": "EarlyVerdict",
                                           "calibration": "Calibrate"}}
        tasks["EarlyDispatch"] = {"type": "script", "script": "passthrough",
                                  "inputs": {"verdict": "EarlyVerdict"}}
        flow += ["EarlyGate --> EarlyVerdict",
                 "EarlyVerdict --|early_issues|--> EarlyRepair",
                 "EarlyRepair --> FinalGate",
                 "EarlyVerdict --|early_clean|--> EarlyDispatch"]
        batch2_src = "EarlyDispatch"
    else:
        batch2_src = None  # n ≤ 6：无批2

    # ── 终门 + 修复守卫环 + 汇出 ──
    tasks["FinalGate"] = {"type": "command",
                          "command": _cmd("svg_quality_checker.py",
                                          "--stage final --canonical-authoring --json"),
                          "timeout": 900.0}
    tasks["FinalVerdict"] = {"type": "script", "script": "final_verdict",
                             "inputs": {"gate": "FinalGate"}}
    tasks["Repair"] = {"type": "script", "script": "repair_node",
                       "inputs": {"verdict": "FinalVerdict",
                                  "calibration": "Calibrate"}}
    tasks["FinalDispatch"] = {"type": "script", "script": "passthrough",
                              "inputs": {"verdict": "FinalVerdict"}}
    flow += ["FinalGate --> FinalVerdict",
             "FinalVerdict --|final_errors|--> Repair",
             "Repair --> FinalGate",
             "FinalVerdict --|final_clean|--> FinalDispatch"]

    # 批2 页（仅 n > 6）：EarlyDispatch 扇出后进 FinalGate
    if early:
        for i in range(batch1 + 1, n + 1):
            tasks[f"P{i:02d}"] = {"type": "script", "script": "page_node",
                                  "inputs": {"plan": "Plan", "calibration": "Calibrate"}}
            flow.append(f"EarlyDispatch --> P{i:02d}")
            flow.append(f"P{i:02d} --> FinalGate")

    # ── 尾段：readiness → notes → split → finalize → export → report ──
    prev = "FinalDispatch"
    if has_images:
        tasks["ImageReadiness"] = {"type": "script", "script": "image_readiness",
                                   "inputs": {"rows": "ImageAcquire"}}
        flow.append(f"{prev} --> ImageReadiness")
        prev = "ImageReadiness"
    if notes_on:
        tasks["NotesGen"] = {"type": "script", "script": "notes_node"}
        flow.append(f"{prev} --> NotesGen")
        prev = "NotesGen"
        tasks["SplitNotes"] = {"type": "command",
                               "command": _cmd("total_md_split.py"),
                               "timeout": 120.0}
        flow.append(f"{prev} --> SplitNotes")
        prev = "SplitNotes"
    tasks["Finalize"] = {"type": "command", "command": _cmd("finalize_svg.py"),
                         "timeout": 600.0}
    flow.append(f"{prev} --> Finalize")
    export_args = [] if notes_on else ["--no-notes"]
    tasks["Export"] = {"type": "command",
                       "command": _cmd("svg_to_pptx.py", *export_args),
                       "timeout": 1200.0}
    flow.append("Finalize --> Export")
    tasks["Report"] = {"type": "script", "script": "ppt_report",
                       "inputs": {"export": "Export", "finalize": "Finalize"}}
    flow.append("Export --> Report")

    return tasks, "\n".join(flow)


def tl_generate(view) -> dict[str, Any]:
    """模板翻译入口：校验并回填 spec → 初始化 workspace + 信封 → tasklist。"""
    spec = view.field("spec")
    spec_schema.validate_ppt_spec(spec)   # 原地校验 + 缺省回填（一次）
    root = workspace.init_workspace(spec["output"]["dir"])
    workspace.write_envelope({
        "output_dir": str(root),
        "roster": spec["roster"],
        "sources": spec["source"].get("paths") or [],
        "spec": spec,
    })
    tasks, flow = build_generate_tasklist(spec)
    return {"Tasks": tasks, "Flow": flow}
```

实现注意：`passthrough` script 是扇出中转（tickflow 一条守卫边只写一个槽，
扇出须经中转节点平边展开），实现为最简 `def passthrough(view): return {"status": "ok"}`。
守卫边命名（early_issues/early_clean/final_errors/final_clean）与 Task 8
registry 注册的四个 guard 名严格一致。

- [x] **Step 4: 跑测试确认通过（按干净版修正断言）**

Run: `python -m pytest example/test_ppt_master_translator.py -q`
Expected: PASS（断言以干净版流为准：`EarlyDispatch --> P06` 存在、无 `early_clean2`）

- [x] **Step 5: 提交**

```bash
git add example/ppt_master/translator.py example/test_ppt_master_translator.py
git commit -m "feat(ppt_master): generate 翻译器（页册静态展开 + 批次门 + 条件段）"
```

---

### Task 8: module.py 组装 + ModuleEntry + 编程 API

**Files:**
- Create: `example/ppt_master/module.py`
- Create: `example/modules/ppt_master.py`
- Test: `example/test_ppt_master_module.py`

- [x] **Step 1: 写失败测试**

```python
# example/test_ppt_master_module.py
"""组装：registry 完整性（翻译器引用的每个 script/command 都已注册）+ 守卫。"""

from __future__ import annotations

from example.ppt_master import workspace
from example.ppt_master.module import GENERATE_TEMPLATE, _build_registry
from example.ppt_master.test_support import sample_spec
from example.ppt_master.translator import build_generate_tasklist
from module_harness.model.spec import TaskDefinition, Tasklist
from module_harness.model.translator import TasklistValidator


def test_registry_covers_translator_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    reg = _build_registry(llm_client=object())
    tasks, flow = build_generate_tasklist(sample_spec())
    tasklist = Tasklist(
        tasks={k: TaskDefinition.from_dict(v) for k, v in tasks.items()},
        flow=flow,
    )
    errors = TasklistValidator.validate(tasklist, reg)
    assert not errors, errors


def test_template_declares_script_translation():
    assert GENERATE_TEMPLATE["translation"] == {
        "type": "script", "script": "tl_generate"}
    assert GENERATE_TEMPLATE["name"] == "generate"
```

注意：`_build_registry(llm_client=object())` 第二参缺省（event_bus=None → EventBus.null()，签名按此实现）。

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest example/test_ppt_master_module.py -q`
Expected: FAIL

- [x] **Step 3: 实现 module.py + modules/ppt_master.py + test_support**

```python
# example/ppt_master/module.py
"""ppt_master — ppt-master Generate 主线的 SpecModule 复刻（单模板 generate）。

节点类型总原则（spec §5/§7）：LLM 节点 = script 包 call_harness（失败收据，
AND 不饿死）；确定性工具 = script 或 command（vendor 闭包）；门 = command +
verdict script + 守卫修复环（上限 2 轮，tools_nodes.repair_node 内计数）。
"""

from __future__ import annotations

import sys
from typing import Any

from llm import LLMConfig, create_llm_client

from module_harness.cli.command import CommandConfig
from module_harness.infra.events import EventBus
from module_harness.model.module import Module
from module_harness.model.spec import TaskDefinition, Tasklist
from module_harness.model.translator import TemplateLoader
from module_harness.core.registry import HarnessRegistry

from . import llm_nodes, tools_nodes, workspace
from .translator import build_generate_tasklist, tl_generate


def _run_tool_command(tool: str, *args: str, timeout: float = 300.0) -> CommandConfig:
    return CommandConfig(
        command=(f'"{sys.executable}" '
                 f'"{workspace.MODULE_DIR / "tools" / "run_tool.py"}" '
                 f'--envelope "{workspace._envelope_path()}" '
                 f"--tool {tool}" + (f" -- {' '.join(args)}" if args else "")),
        timeout=timeout,
    )


def _build_registry(
    llm_client: Any,
    event_bus: EventBus | None = None,
    max_repair_rounds: int = 2,
) -> HarnessRegistry:
    bus = event_bus or EventBus.null()
    reg = HarnessRegistry(llm_client=llm_client, event_bus=bus)

    # LLM 节点（工厂闭包捕获 client —— namespace 隔离）
    reg.script("plan_node")(llm_nodes.make_plan_node(llm_client, bus))
    reg.script("research_node")(llm_nodes.make_research_node(llm_client, bus))
    reg.script("page_node")(llm_nodes.make_page_node(llm_client, bus))
    reg.script("repair_node")(llm_nodes.make_repair_node(llm_client, bus, max_rounds))
    reg.script("notes_node")(llm_nodes.make_notes_node(llm_client, bus))
    reg.script("image_acquire")(llm_nodes.make_image_node(llm_client, bus))

    # 确定性 script 节点
    reg.script("ingest")(tools_nodes.ingest)
    reg.script("init")(tools_nodes.init)
    reg.script("plan_validate")(tools_nodes.plan_validate)
    reg.script("calibrate")(tools_nodes.calibrate)
    reg.script("icon_sync_node")(tools_nodes.icon_sync_node)
    reg.script("image_readiness")(tools_nodes.image_readiness)
    reg.script("passthrough")(tools_nodes.passthrough)
    reg.script("early_verdict")(tools_nodes.make_gate_verdict("early"))
    reg.script("final_verdict")(tools_nodes.make_gate_verdict("final"))
    reg.script("ppt_report")(tools_nodes.report)

    # 守卫（读源任务 inputs 键 'gate'；文件复检，want_clean 分正反）
    reg.guard("early_issues", tools_nodes.make_guard("early", want_clean=False))
    reg.guard("early_clean", tools_nodes.make_guard("early", want_clean=True))
    reg.guard("final_errors", tools_nodes.make_guard("final", want_clean=False))
    reg.guard("final_clean", tools_nodes.make_guard("final", want_clean=True))
    return reg


def _generate_tasklist() -> Tasklist:
    """静态骨架（翻译器运行时会整体替换；模板声明需要合法 tasklist）。"""
    tasks, flow = build_generate_tasklist({
        "project": "scaffold",
        "source": {"kind": "files", "paths": ["x.md"]},
        "roster": [{"id": "p01", "title": "占位"}],
    })
    return Tasklist(tasks={
        k: TaskDefinition.from_dict(v) for k, v in tasks.items()
    }, flow=flow)


GENERATE_TEMPLATE: dict[str, Any] = {
    "name": "generate",
    "description": (
        "ppt-master Generate 主线复刻：源处理 → 规划（design_spec/spec_lock）"
        "→ 图像/图标 → 并行逐页 SVG → 早/终质量门（守卫修复环）→ 导出 pptx"
    ),
    "translation": {"type": "script", "script": "tl_generate"},
    "tasklist": _generate_tasklist().to_dict(),
}


def run_generate(
    spec: dict[str, Any],
    *,
    llm_client: Any = None,
    max_ticks: int = 400,
    persist: bool = False,
):
    """编程 API（测试/嵌入用）：构造并运行 generate。"""
    if llm_client is None:
        llm_client = create_llm_client(LLMConfig.from_env())
    loader = TemplateLoader()
    loader.register("generate", GENERATE_TEMPLATE)
    mod = Module(
        spec=spec,
        template_name="generate",
        template_loader=loader,
        llm_client=llm_client,
        registry=_build_registry(llm_client),
        review_harness=None,
        persist=persist,
        status_file=persist,
    )
    return mod.run(max_ticks=max_ticks)
```

`example/modules/ppt_master.py`（CLI 发现入口）：

```python
# example/modules/ppt_master.py
"""ppt_master 模块入口（CLI 发现用）。"""

from __future__ import annotations

from typing import Any

from example.ppt_master.module import GENERATE_TEMPLATE, _build_registry
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus


def _registry_for(llm_client: Any, template_name: str, event_bus: EventBus) -> Any:
    return _build_registry(llm_client, event_bus)


entry = ModuleEntry(
    name="ppt_master",
    description=(
        "ppt-master Generate 主线复刻：spec(页册+契约) → 规划"
        "(design_spec/spec_lock) → 并行逐页 SVG → 质量门修复环 → "
        "svg_to_pptx 导出（spec 即确认，非交互）"
    ),
    templates={"generate": GENERATE_TEMPLATE},
    build_registry=_registry_for,
    default_spec=None,   # 页册因项目而异，无零配置缺省（示例 spec 见 fixtures/）
    default_template="generate",
    review_harness=None,  # 固定流程模板，发布前已验证
)
```

`example/ppt_master/test_support.py`（测试辅助，10 页无图像、notes 开——覆盖最多节点的形状）：

```python
# example/ppt_master/test_support.py
"""测试辅助：覆盖全条件段的样例 spec（10 页、ai 图像、notes 开）。"""

from __future__ import annotations


def sample_spec() -> dict:
    return {
        "project": "support",
        "source": {"kind": "files", "paths": ["a.md"]},
        "images": {"sources": ["ai", "user"]},
        "roster": [{"id": f"p{i:02d}", "title": f"页{i}"} for i in range(1, 11)],
    }
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest example/test_ppt_master_module.py -q`
Expected: PASS。若 validator 报"孤立节点/未注册"，按报错补注册或改流——**以 validator 为准**，它是翻译校验的同一道闸。

- [x] **Step 5: 提交**

```bash
git add example/ppt_master/module.py example/ppt_master/test_support.py example/modules/ppt_master.py example/test_ppt_master_module.py
git commit -m "feat(ppt_master): registry 组装 + ModuleEntry + run_generate 编程 API"
```

---

### Task 9: fixtures mock 全链 E2E + CLI 冒烟

前置：Task 1 的 fixture SVG 已过 checker final（Task 1 Step 4–5 产出）。

**Files:**
- Test: `example/test_ppt_master_e2e.py`
- Modify: `example/ppt_master/fixtures/`（按需补 fixture 页数副本）

- [x] **Step 1: 写 E2E 测试**

```python
# example/test_ppt_master_e2e.py
"""Mock 全链 E2E：4 页册（≤6 无早门）、notes 关（最少条件段）、零 LLM。

ScriptedMock 按 prompt 内容分流：PLANNING → 计划收据 JSON（含过校验的
design_spec/spec_lock 全文）；否则 → fixture SVG（Task 1 已验可过 final 门）。
全链真跑 vendor checker/finalize/export —— 工程复刻的验收底线。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from example.ppt_master import workspace
from example.ppt_master.module import run_generate
from llm.client import LLMResponse
from llm.mock import MockLLMClient

FIXTURES = Path(__file__).parent / "ppt_master" / "fixtures"
_SVG = (FIXTURES / "page_p01.svg").read_text(encoding="utf-8")
_LOCK = "# lock\npalette: #111111\ntypography: sans\n"
_SPEC_MD = (
    "# spec\n§IX\n"
    "## p01 封面\nAudience move: 建立第一印象\n"
    "## p02 方法\nAudience move: 交代路径\n"
    "## p03 结果\nAudience move: 呈现证据\n"
    "## p04 结语\nAudience move: 给出结论\n"
)


class ScriptedMock(MockLLMClient):
    async def complete(self, **kw):
        prompt = kw.get("prompt", "")
        if "PLANNING" in prompt:
            return LLMResponse(content=json.dumps({
                "status": "ok",
                "roster_ids": ["p01", "p02", "p03", "p04"],
                "design_spec_md": _SPEC_MD,
                "spec_lock_md": _LOCK,
                "image_rows": [], "icon_pool": [], "notes_enabled": False,
            }))
        return LLMResponse(content=_SVG)


def _spec(output_dir: Path) -> dict:
    return {
        "project": "e2e",
        "source": {"kind": "files", "paths": ["__none__"]},  # ingest 对不存在 md 容错见实现
        "production": {"speaker_notes": False},
        "output": {"dir": str(output_dir)},
        "roster": [
            {"id": "p01", "title": "封面", "role": "cover"},
            {"id": "p02", "title": "方法"},
            {"id": "p03", "title": "结果"},
            {"id": "p04", "title": "结语", "role": "closing"},
        ],
    }


def test_full_pipeline_mock(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    out_dir = tmp_path / "deck"
    firings = asyncio.run(run_generate(
        _spec(out_dir), llm_client=ScriptedMock(), persist=False))
    by_node = {f.node: f.output for f in firings}
    assert by_node["Report"]["status"] == "ok", by_node.get("Report")
    pptx = list((out_dir / "exports").glob("*.pptx"))
    assert pptx, "exports/ 应有 pptx"

    from pptx import Presentation
    assert len(Presentation(str(pptx[0])).slides) == 4
```

实现缺口预案（写测试时同步处理）：
- `ingest` 对不存在路径：`Path(s).exists()` 为假时返回 `{"status": "ok", "digest": "（源缺失，契约驱动）", "copied": []}`——在 Task 6 的 `ingest` 里加此分支（测试驱动补上）。
- 页 SVG 落盘文件名须与 checker 期望一致（`svg_output/p01.svg` …）。Task 1 的 fixture 项目里 checker 认的页名规则以实测为准；若 checker 要求 `01_cover.svg` 式命名，改 `llm_nodes.make_page_node` 的落盘名与 `notes`/checker 对齐，并同步 `fixtures/` 文件名——**命名规则以 checker 实测为准，四件套（Task 1）先行锁定**。
- checker 对 4 个相同 SVG 可能报重复类 warning（非 blocking 即可过）。

- [x] **Step 2: 跑测试**

Run: `python -m pytest example/test_ppt_master_e2e.py -q`
Expected: PASS（首次大概率 FAIL——按失败链逐环修：ingest 容错 → 页名 → 收据页册一致 → verdict 解析；每修一环重跑）

- [x] **Step 3: CLI 冒烟**

```python
# 追加到 example/test_ppt_master_e2e.py
def test_cli_mock_smoke(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from module_harness.cli import main
    spec = _spec(tmp_path / "cli_deck")
    rc = main([
        "run", "--module", "ppt_master", "--mock",
        "--modules-dir", str(Path(__file__).parent / "modules"),
        "--run-id", "ppt_master_cli_smoke",
        "--spec", json.dumps(spec, ensure_ascii=False),
    ])
    assert rc == 0, capsys.readouterr().err
    assert list((tmp_path / "cli_deck" / "exports").glob("*.pptx"))
```

注意：CLI 进程与测试进程不同（main 直接调用则同进程）——信封是 **pid 修饰**的，main 同进程调用时 translator 写、command 子进程读的是同一路径，成立。若 CLI 以子进程方式跑（subscript `__main__`），信封 pid 不同会断——以 `test_ppt_cli.py` 同款 in-process `main()` 调用为准（已核实是 in-process）。

Run: `python -m pytest example/test_ppt_master_e2e.py -q`
Expected: PASS（2 项）

- [x] **Step 4: 全量回归**

Run: `python -m pytest module_harness/tests/ -q && python -m pytest example/ -q`
Expected: 全 PASS（框架测试不受影响；example 旧 ppt 测试在 Task 10 前仍在，应继续通过）

- [x] **Step 5: 提交**

```bash
git add example/test_ppt_master_e2e.py example/ppt_master/fixtures example/ppt_master/tools_nodes.py example/ppt_master/llm_nodes.py
git commit -m "test(ppt_master): mock 全链 E2E + CLI 冒烟（真跑 vendor 四件套）"
```

---

### Task 10: ppt_writer 归档 + roadmap 更新 + 真实 LLM smoke（skip 默认）

**Files:**
- Delete: `example/modules/ppt_writer.py`、`example/ppt_writer/`、`example/test_ppt_*.py`
- Modify: `docs/dev/progress/module-roadmap.md`（M2 段）
- Create: `example/test_ppt_master_smoke_llm.py`

- [x] **Step 1: 归档 ppt_writer**

```bash
git rm -r example/ppt_writer example/modules/ppt_writer.py \
  example/test_ppt_cli.py example/test_ppt_entry.py example/test_ppt_normalize.py \
  example/test_ppt_render.py example/test_ppt_workflow.py
# openspec/changes/ppt-writer-module 历史记录保留（git 历史可追溯），不删
```

跑 `python -m pytest example/ -q` 确认无残留引用报错（`academic_writer`/`demo_*` 等不 import ppt_writer，已核实）。

- [x] **Step 2: roadmap M2 段更新**

`docs/dev/progress/module-roadmap.md`：
- 行 208–214（M2 基础版状态块）替换为：

```markdown
- [x] **M2 论文→PPT 实践线（ppt-master 化重构，2026-09-15 设计）**：
  ppt_writer 基础版（python-pptx 模板填充）已由 `ppt_master` 模块替代归档。
  新模块完全复刻 ppt-master Generate 主线工程范式：LLM 逐页 SVG → 早/终
  质量门（守卫修复环）→ svg_to_pptx 确定性编译（vendor MIT 最小闭包）；
  spec 即确认（页册先行，非交互）；设计见
  `docs/dev/superpowers/specs/2026-09-15-ppt-master-module-design.md`。
  待办：store 发布闭环（publish→install→run，Mock 全链零 LLM 路径作首个
  验收 fixture）；真实 LLM 端到端调优（prompt 素材质量）
```

- 行 260–279 的 M2 目标块：在"框架验证点"后追加一行：

```markdown
- **守卫修复环 + 失败收据（新增）**：门失败定向修复（上限 2 轮）、页节点
  失败收据保证 AND join 不饿死（tickflow Failure 语义下的非交互补强）
```

- [x] **Step 3: 真实 LLM smoke（默认 skip）**

```python
# example/test_ppt_master_smoke_llm.py
"""真实 LLM 端到端 smoke（付费，默认跳过）。

跑法：SMOKE_LLM=1 python -m pytest example/test_ppt_master_smoke_llm.py -q
前置：.env 配好 LLM_* ；2 页小册子，验证真模型下规划/逐页/门/导出全链。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from example.ppt_master import workspace
from example.ppt_master.module import run_generate

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOKE_LLM") != "1", reason="真实 LLM smoke 需 SMOKE_LLM=1"
)


def test_two_page_real_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    spec = {
        "project": "smoke",
        "source": {"kind": "topic", "topic": "Petri 网工作流引擎简介"},
        "production": {"speaker_notes": False},
        "output": {"dir": str(tmp_path / "deck")},
        "roster": [{"id": "p01", "title": "开场", "role": "cover"},
                   {"id": "p02", "title": "核心思想"}],
    }
    firings = asyncio.run(run_generate(spec, persist=False))
    by_node = {f.node: f.output for f in firings}
    assert by_node["Report"]["status"] == "ok"
```

- [x] **Step 4: 全量回归 + 提交**

```bash
python -m pytest module_harness/tests/ -q && python -m pytest example/ -q
git add -A
git commit -m "refactor(M2): ppt_writer 归档，ppt_master 接替；roadmap 更新；真实 LLM smoke（skip 默认）"
```

---

## 收尾核对（计划自审结论）

- **Spec 覆盖**：§2 模块形态→Task 8/10；§3 页册先行→Task 3/7；§4 schema→Task 3；§5 流图与节点→Task 5/6/7（节点类型微调：LLM 节点以 script 包 call_harness 实现，为满足 §7 失败收据语义，spec 已注明）；§6 工件/vendor→Task 1/2；§7 失败语义→Task 5/6（轮上限、readiness、fail-fast）；§8 测试四层→Task 1（层3）/9（层2）/10（层4）/各任务单测（层1）；§9 store 闭环→Task 10 roadmap 待办（publish 验收属 store 主线，不入本计划）；§10 后续扩展→不入计划。
- **类型/命名一致**：节点名（EarlyGate/EarlyVerdict/EarlyRepair/EarlyDispatch/FinalGate/FinalVerdict/Repair/FinalDispatch）、script 名（page_node/repair_node/early_verdict/final_verdict/passthrough/ppt_report）、信封键（output_dir/roster/sources/spec）在 Task 5/6/7/8 间已互核。
- **已知风险**：① Task 1 fixture 过 checker 的迭代次数（升级路径已写明）；② svg_to_pptx 对同构 4 页 fixture 可能报非阻断 warning（可接受）；③ argparse `--` 分隔行为以实测为准（备选 parse_known_args 已写明）。
