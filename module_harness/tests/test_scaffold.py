# module_harness/tests/test_scaffold.py
"""specmodule init 脚手架测试：validate_module_name / scaffold / 冒烟。"""

from __future__ import annotations

import json

import pytest

from module_harness.cli import main
from module_harness.scaffold import (
    ScaffoldResult,
    build_module_source,
    scaffold,
    validate_module_name,
)

PROJECT_FILES = [
    "config.json",
    ".env.example",
    ".gitignore",
    "spec.example.json",
    "README.md",
]


def _all_files(root) -> set[str]:
    return {str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()}


class TestValidateModuleName:
    @pytest.mark.parametrize(
        "name", ["hello", "my_module", "_x", "a1", "translate_en"]
    )
    def test_valid_name(self, name):
        assert validate_module_name(name)

    @pytest.mark.parametrize(
        "name", ["", "my-module", "论文优化", "hello world", "1abc", "hello.py", "a b"]
    )
    def test_invalid_name(self, name):
        assert not validate_module_name(name)


class TestModuleSource:
    def test_compiles_and_embeds_name(self):
        src = build_module_source("hello", "示例描述")
        compile(src, "modules/hello.py", "exec")
        assert 'name="hello"' in src
        assert "示例描述" in src

    def test_description_escaped_as_literal(self, tmp_path):
        # 描述含双引号时仍须生成合法源码
        src = build_module_source("hello", '含"引号"的描述')
        compile(src, "modules/hello.py", "exec")
        assert '含\\"引号\\"的描述' in src


class TestScaffold:
    def test_full_tree(self, tmp_path):
        r = scaffold("hello", base_dir=tmp_path, description="示例")
        assert isinstance(r, ScaffoldResult)
        created = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in r.created}
        assert "modules/hello.py" in created
        for f in PROJECT_FILES:
            assert f in created, f"缺少 {f}"
        assert (tmp_path / "modules" / "hello.py").exists()
        for f in PROJECT_FILES:
            assert (tmp_path / f).exists()

    def test_config_json_shape(self, tmp_path):
        scaffold("hello", base_dir=tmp_path)
        cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert cfg["providers"][0]["api_key_env"] == "OPENAI_API_KEY"
        assert cfg["models"] == []

    def test_env_example_not_gitignored_conflict(self, tmp_path):
        scaffold("hello", base_dir=tmp_path)
        gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gi          # 密钥文件被排除
        assert (tmp_path / ".env.example").exists()  # 占位可提交

    def test_invalid_name_rejected_and_nothing_written(self, tmp_path):
        with pytest.raises(ValueError):
            scaffold("my-module", base_dir=tmp_path)
        assert not (tmp_path / "modules").exists()
        for f in PROJECT_FILES:
            assert not (tmp_path / f).exists()

    def test_existing_module_without_force_raises(self, tmp_path):
        scaffold("hello", base_dir=tmp_path)
        with pytest.raises(ValueError, match="--force"):
            scaffold("hello", base_dir=tmp_path)

    def test_force_overwrites_module_only(self, tmp_path):
        scaffold("hello", base_dir=tmp_path)
        (tmp_path / "modules" / "hello.py").write_text("OLD", encoding="utf-8")
        # 项目文件已存在 → 第二次（force）只覆盖模块，项目文件跳过
        r = scaffold("hello", base_dir=tmp_path, force=True)
        assert (tmp_path / "modules" / "hello.py").read_text(encoding="utf-8") != "OLD"
        skipped = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in r.skipped}
        for f in PROJECT_FILES:
            assert f in skipped, f"项目文件 {f} 应跳过而非覆盖"

    def test_idempotent_second_run_raises(self, tmp_path):
        scaffold("hello", base_dir=tmp_path)
        with pytest.raises(ValueError):
            scaffold("hello", base_dir=tmp_path)
        # 项目文件未被重复创建破坏（内容仍为脚手架生成）
        assert "OPENAI_API_KEY" in (tmp_path / ".env.example").read_text(encoding="utf-8")


class TestCliInit:
    def test_init_via_cli(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        code = main(["init", "hello", "--description", "示例"])
        assert code == 0
        out = capsys.readouterr().out
        assert "modules\\hello.py" in out or "modules/hello.py" in out
        assert "冒烟验收" in out
        assert (tmp_path / "modules" / "hello.py").exists()

    def test_init_invalid_name_exit_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        code = main(["init", "my-module"])
        assert code == 1
        assert "不是合法 Python 标识符" in capsys.readouterr().err
        assert not (tmp_path / "modules").exists()

    def test_init_existing_module_exit_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["init", "hello"]) == 0
        code = main(["init", "hello"])
        assert code == 1
        assert "--force" in capsys.readouterr().err


class TestSmoke:
    def test_run_mock_after_init(self, tmp_path, monkeypatch, capsys):
        """验收：init 生成的模块可被发现，run --mock 完整跑通（harness→script 流水线）。"""
        monkeypatch.chdir(tmp_path)
        assert main(["init", "hello"]) == 0
        code = main(
            ["run", "--module", "hello", "--mock", "--modules-dir", str(tmp_path / "modules")]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "运行完成" in out
        assert "Translate" in out
        assert "Echo" in out
        assert "mock output" in out