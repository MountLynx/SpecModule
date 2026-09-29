# module_harness/tests/test_artifacts.py
"""run 产物清单：声明模型 / 收集器 / 终态挂点 / 查询读端 / CLI。"""

from __future__ import annotations

import json
import os

import pytest

from module_harness.infra.artifacts import artifacts_path, collect_artifacts, write_artifacts_manifest
from module_harness.model.spec import ArtifactDecl, TaskDefinition, Tasklist


# ── ArtifactDecl / Tasklist.Artifacts（模型层）─────────────────────────


class TestArtifactDecl:
    def test_from_dict_roundtrip(self):
        d = {"name": "deck", "path": "exports/*.pptx",
             "kind": "deliverable", "pick": "latest"}
        decl = ArtifactDecl.from_dict(d)
        assert decl.name == "deck"
        assert decl.kind == "deliverable"
        assert decl.pick == "latest"
        assert decl.to_dict() == d

    def test_defaults(self):
        decl = ArtifactDecl.from_dict({"name": "n", "path": "p/*"})
        assert decl.kind == "intermediate"
        assert decl.pick == "all"

    def test_blank_name_rejected(self):
        with pytest.raises(ValueError, match="name"):
            ArtifactDecl.from_dict({"name": " ", "path": "p/*"})

    def test_blank_path_rejected(self):
        with pytest.raises(ValueError, match="path"):
            ArtifactDecl.from_dict({"name": "n", "path": ""})

    def test_bad_kind_rejected(self):
        with pytest.raises(ValueError, match="kind"):
            ArtifactDecl.from_dict({"name": "n", "path": "p/*", "kind": "x"})

    def test_bad_pick_rejected(self):
        with pytest.raises(ValueError, match="pick"):
            ArtifactDecl.from_dict({"name": "n", "path": "p/*", "pick": "x"})


class TestTasklistArtifacts:
    @staticmethod
    def _tl(artifacts):
        return Tasklist(
            tasks={"A": TaskDefinition(type="script", script="A")},
            flow="[A]",
            artifacts=artifacts,
        )

    def test_from_json_parses_artifacts(self):
        tl = Tasklist.from_json({
            "Tasks": {"A": {"type": "script", "script": "A"}},
            "Flow": "[A]",
            "Artifacts": [{"name": "deck", "path": "exports/*.pptx"}],
        })
        assert len(tl.artifacts) == 1
        assert tl.artifacts[0].kind == "intermediate"

    def test_from_json_without_artifacts_defaults_empty(self):
        tl = Tasklist.from_json({
            "Tasks": {"A": {"type": "script", "script": "A"}},
            "Flow": "[A]",
        })
        assert tl.artifacts == []

    def test_from_json_non_list_rejected(self):
        with pytest.raises(ValueError, match="Artifacts"):
            Tasklist.from_json({
                "Tasks": {"A": {"type": "script", "script": "A"}},
                "Flow": "[A]",
                "Artifacts": "nope",
            })

    def test_to_dict_roundtrip_and_omits_empty(self):
        tl = self._tl([ArtifactDecl(name="deck", path="exports/*.pptx")])
        d = tl.to_dict()
        assert d["Artifacts"] == [{
            "name": "deck", "path": "exports/*.pptx",
            "kind": "intermediate", "pick": "all",
        }]
        assert Tasklist.from_json(d).artifacts[0].path == "exports/*.pptx"
        # 空声明不出现在序列化——旧格式 tasklist 往返零扰动
        assert "Artifacts" not in self._tl([]).to_dict()


# ── 收集器（infra/artifacts.py）───────────────────────────────────────


class TestCollectArtifacts:
    def test_glob_relative_resolved_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "exports").mkdir()
        (tmp_path / "exports" / "deck.pptx").write_bytes(b"PK")
        entries = collect_artifacts(
            [ArtifactDecl(name="deck", path="exports/*.pptx")])
        assert len(entries) == 1
        e = entries[0]
        assert e["path"] == str(tmp_path / "exports" / "deck.pptx")
        assert e["name"] == "deck"
        assert e["kind"] == "intermediate"
        assert e["size"] == 2
        assert "T" in e["modified"]  # ISO8601 本地时间

    def test_pick_all_sorted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "exports").mkdir()
        for n in ("b.pptx", "a.pptx"):
            (tmp_path / "exports" / n).write_bytes(b"x")
        entries = collect_artifacts(
            [ArtifactDecl(name="deck", path="exports/*.pptx")])
        assert [os.path.basename(e["path"]) for e in entries] == ["a.pptx", "b.pptx"]

    def test_pick_latest_by_mtime(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "exports").mkdir()
        old = tmp_path / "exports" / "deck_1.pptx"
        new = tmp_path / "exports" / "deck_2.pptx"
        old.write_bytes(b"old")
        new.write_bytes(b"new")
        os.utime(old, (1000000000, 1000000000))
        entries = collect_artifacts(
            [ArtifactDecl(name="deck", path="exports/*.pptx", pick="latest")])
        assert len(entries) == 1
        assert entries[0]["path"] == str(new)

    def test_zero_match_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert collect_artifacts(
            [ArtifactDecl(name="deck", path="exports/*.pptx")]) == []

    def test_directory_match_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "exports" / "sub").mkdir(parents=True)
        assert collect_artifacts(
            [ArtifactDecl(name="x", path="exports/*")]) == []

    def test_multiple_decls_accumulate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "exports").mkdir()
        (tmp_path / "notes").mkdir()
        (tmp_path / "exports" / "a.pptx").write_bytes(b"x")
        (tmp_path / "notes" / "total.md").write_text("# n", encoding="utf-8")
        entries = collect_artifacts([
            ArtifactDecl(name="deck", path="exports/*.pptx"),
            ArtifactDecl(name="notes", path="notes/*.md"),
        ])
        assert [e["name"] for e in entries] == ["deck", "notes"]


class TestWriteManifest:
    def test_writes_manifest_with_run_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_dir = tmp_path / ".specmodule" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (tmp_path / "exports").mkdir()
        (tmp_path / "exports" / "deck.pptx").write_bytes(b"PK")
        write_artifacts_manifest(
            "r1", [ArtifactDecl(name="deck", path="exports/*.pptx")])
        raw = json.loads(
            (run_dir / "artifacts.json").read_text(encoding="utf-8"))
        assert raw["run_id"] == "r1"
        assert len(raw["artifacts"]) == 1

    def test_zero_match_writes_empty_list(self, tmp_path, monkeypatch):
        """声明存在但零匹配 → 空清单（区分"收集过没产出"与"没收集"）。"""
        monkeypatch.chdir(tmp_path)
        run_dir = tmp_path / ".specmodule" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        write_artifacts_manifest("r1", [ArtifactDecl(name="x", path="nope/*")])
        raw = json.loads(
            (run_dir / "artifacts.json").read_text(encoding="utf-8"))
        assert raw["artifacts"] == []

    def test_no_decls_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_dir = tmp_path / ".specmodule" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        write_artifacts_manifest("r1", [])
        assert not (run_dir / "artifacts.json").exists()

    def test_no_run_dir_no_side_effect(self, tmp_path, monkeypatch):
        """纯内存模式（run 目录不存在）不落盘、不建目录。"""
        monkeypatch.chdir(tmp_path)
        write_artifacts_manifest("r1", [ArtifactDecl(name="x", path="nope/*")])
        assert not (tmp_path / ".specmodule").exists()

    def test_write_oserror_swallowed(self, tmp_path, monkeypatch):
        """写失败仅 log 不抛（对齐 _write_phase 哲学）。"""
        monkeypatch.chdir(tmp_path)
        run_dir = tmp_path / ".specmodule" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (tmp_path / "exports").mkdir()
        (tmp_path / "exports" / "deck.pptx").write_bytes(b"PK")

        import os as _os

        real_replace = _os.replace

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(_os, "replace", boom)
        write_artifacts_manifest(
            "r1", [ArtifactDecl(name="deck", path="exports/*.pptx")])
        monkeypatch.setattr(_os, "replace", real_replace)  # 保险（monkeypatch 亦会还原）
        assert not (run_dir / "artifacts.json").exists()
