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
    out_backfill = spec.setdefault("output", {})
    out_backfill["dir"] = out_backfill.get("dir") or f"projects/{spec['project']}"
    for page in spec["roster"]:
        page.setdefault("role", "content")
