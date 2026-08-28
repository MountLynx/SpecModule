# example/test_ppt_cli.py
"""渲染 CLI 测试（4.3）：mock 冒烟跑通（run + status/review 可查）+
缺失 python-pptx 时的命令级报错路径（monkeypatch 模拟子进程导入失败）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from module_harness.cli import main

MODULES_DIR = Path(__file__).parent / "modules"

# 内置 3 页 spec 的 CLI 版（output 指向临时目录）
def _spec(output: str) -> str:
    return json.dumps({
        "title": "CLI 冒烟",
        "output": output,
        "sections": [{"id": "s", "title": "引言", "defaults": {"layout": "content"}}],
        "pages": [
            {"index": 1, "section": "s", "title": "CLI 标题", "layout": "title"},
            {"index": 2, "section": "s", "title": "要点页",
             "content": {"points": ["冒烟要点一", "冒烟要点二"]}},
            {"index": 3, "title": "谢谢", "layout": "thanks"},
        ],
    }, ensure_ascii=False)


def test_cli_mock_smoke_render(tmp_path: Path, monkeypatch, capsys):
    """specmodule run --module ppt_writer --mock 产出 pptx；status/review 可查。"""
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "cli_deck.pptx"
    rc = main([
        "run", "--module", "ppt_writer", "--mock",
        "--modules-dir", str(MODULES_DIR),
        "--run-id", "ppt_cli_smoke",
        "--spec", _spec(str(out)),
    ])
    assert rc == 0, capsys.readouterr().err
    capsys.readouterr()  # 清掉 run 的 stdout（结束汇总）
    assert out.exists()

    from pptx import Presentation
    assert len(list(Presentation(str(out)).slides)) == 3

    rc = main(["status", "--run-id", "ppt_cli_smoke", "--json"])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["phase"] == "done"

    rc = main(["review", "--run-id", "ppt_cli_smoke", "--json"])
    assert rc == 0
    timeline = json.loads(capsys.readouterr().out)
    nodes = {e["node"] for e in timeline["entries"]}
    assert "Render" in nodes and "Report" in nodes


def test_cli_missing_pptx_command_level_error(tmp_path: Path):
    """缺失 python-pptx → 命令级报错：exit 2 + stderr 含安装指引。

    用 PYTHONPATH 前置一个 import 即抛 ImportError 的假 pptx 包模拟缺失。
    """
    from example.ppt_writer import normalize
    fake = tmp_path / "fake_pptx"
    (fake / "pptx").mkdir(parents=True)
    (fake / "pptx" / "__init__.py").write_text(
        "raise ImportError('blocked for test')", encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake) + os.pathsep + env.get("PYTHONPATH", "")

    # 信封照常写（翻译器不依赖 pptx），渲染命令子进程导入失败
    normalize.write_envelope("render", {
        "pages": normalize.normalize({"pages": [{"index": 1, "title": "t"}]}),
        "output": str(tmp_path / "x.pptx"), "font": "微软雅黑",
    })
    r = subprocess.run(
        [sys.executable, str(normalize.MODULE_DIR / "render_deck.py"),
         "--envelope", str(normalize._envelope_path("render"))],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert r.returncode == 2
    assert "python-pptx" in r.stderr and "pip install" in r.stderr
