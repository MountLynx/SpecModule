# module_harness/tests/test_node_artifacts.py
"""query.node_artifacts：节点输出文件引用提取（存在性锚定 + 去重 + 交付物比对）。"""

from __future__ import annotations

import json

from tickflow.persistence import SqliteBackend
from tickflow.state import NodeState

from module_harness.infra.query import node_artifacts


def _seed(tmp_path, module_id="mod_x", firings=(), artifacts=None):
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(run_dir / "run.sqlite")
    for f in firings:
        backend.save_firing(module_id, NodeState(**f))
    backend.close()
    if artifacts is not None:
        (run_dir / "artifacts.json").write_text(
            json.dumps({"run_id": module_id, "artifacts": artifacts},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    return tmp_path


def _touch(base, rel: str):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x" * 11, encoding="utf-8")
    return p


class TestNodeArtifacts:
    def test_no_db_returns_none(self, tmp_path):
        assert node_artifacts("mod_x", base_dir=tmp_path) is None

    def test_relative_path_resolved_and_filtered(self, tmp_path):
        _touch(tmp_path, "projects/demo/page.svg")
        _seed(tmp_path, firings=[{
            "tick": 1, "node": "P1",
            "output": {"status": "ok", "file": "projects/demo/page.svg",
                       "note": "projects/demo/missing.svg"},
        }])
        result = node_artifacts("mod_x", base_dir=tmp_path)
        assert result is not None
        assert list(result) == ["P1"]
        (e,) = result["P1"]
        assert e["index"] == 0
        assert e["key"] == "file"
        assert e["name"] == "page.svg"
        assert e["kind"] == "intermediate"
        assert e["size"] == 11
        assert e["path"] == str(tmp_path / "projects" / "demo" / "page.svg")

    def test_nested_list_key_and_dedupe_within_node(self, tmp_path):
        a = _touch(tmp_path, "exports/a.pptx")
        _seed(tmp_path, firings=[{
            "tick": 1, "node": "R",
            "output": {"pptx": ["exports/a.pptx", "exports/a.pptx",
                                "exports/gone.pptx"]},
        }])
        result = node_artifacts("mod_x", base_dir=tmp_path)
        (e,) = result["R"]
        assert e["key"] == "pptx.0"
        assert e["path"] == str(a)

    def test_cross_node_dedupe_first_wins(self, tmp_path):
        _touch(tmp_path, "shared.bin")
        _seed(tmp_path, firings=[
            {"tick": 1, "node": "A", "output": {"file": "shared.bin"}},
            {"tick": 2, "node": "B", "output": {"file": "shared.bin"}},
        ])
        result = node_artifacts("mod_x", base_dir=tmp_path)
        assert list(result) == ["A"]

    def test_last_firing_wins(self, tmp_path):
        _touch(tmp_path, "old.bin")
        _touch(tmp_path, "new.bin")
        _seed(tmp_path, firings=[
            {"tick": 1, "node": "P", "output": {"file": "old.bin"}},
            {"tick": 2, "node": "P", "output": {"file": "new.bin"}},
        ])
        result = node_artifacts("mod_x", base_dir=tmp_path)
        (e,) = result["P"]
        assert e["name"] == "new.bin"

    def test_deliverable_kind_from_manifest(self, tmp_path):
        p = _touch(tmp_path, "exports/final.pptx")
        _seed(tmp_path, firings=[
            {"tick": 1, "node": "E", "output": {"file": "exports/final.pptx"}},
        ], artifacts=[{
            "name": "成品", "kind": "deliverable", "path": str(p),
            "size": 11, "modified": "2026-10-02T08:00:00",
        }])
        (e,) = node_artifacts("mod_x", base_dir=tmp_path)["E"]
        assert e["kind"] == "deliverable"

    def test_absolute_path_and_plain_strings_ignored(self, tmp_path):
        p = _touch(tmp_path, "abs.bin")
        _seed(tmp_path, firings=[{
            "tick": 1, "node": "M",
            "output": {"ok": "ok", "digest": "# 标题\n正文",
                       "abs_file": str(p)},
        }])
        result = node_artifacts("mod_x", base_dir=tmp_path)
        (e,) = result["M"]
        assert e["key"] == "abs_file"
        assert len(result["M"]) == 1

    def test_directory_value_skipped(self, tmp_path):
        (tmp_path / "projects" / "demo").mkdir(parents=True)
        _seed(tmp_path, firings=[{
            "tick": 1, "node": "I", "output": {"output_dir": "projects/demo"},
        }])
        assert node_artifacts("mod_x", base_dir=tmp_path) == {}
