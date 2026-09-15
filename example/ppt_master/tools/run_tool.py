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
    parser.add_argument("args", nargs="*")
    ns = parser.parse_args(argv)

    try:
        envelope = json.loads(Path(ns.envelope).read_text(encoding="utf-8"))
        root = envelope["output_dir"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        parser.error(f"信封缺失或损坏: {ns.envelope}（{e}）")

    tool = Path(__file__).resolve().parent.parent / "vendor" / "ppt_master" / "scripts" / ns.tool
    cmd = [sys.executable, str(tool), root, *ns.args]
    result = subprocess.run(cmd, encoding="utf-8", errors="replace")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
