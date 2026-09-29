# module_harness/tests/test_artifacts.py
"""run 产物清单：声明模型 / 收集器 / 终态挂点 / 查询读端 / CLI。"""

from __future__ import annotations

import pytest

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
