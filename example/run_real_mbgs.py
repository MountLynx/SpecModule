# example/run_real_mbgs.py
"""真实 LLM 全链 runner：MBGS-SNDPR 论文 → 9 页 deck（AI 封面图 + 5 张用户图置入）。

用法（repo 根目录）：python example/run_real_mbgs.py
前置：config.json / .env（deepseek 文本 + bianxie gpt-image-1-mini 生图）；
用户图（论文 Fig.1-5）由本脚本按 spec 契约清单预置进工作区 images/——
user 来源的应有意涵就是"素材在规划前就位"（upstream Generate 同语义）。
产物：workspace 全套（svg_output/exports/validation）+ projects/mbgs_v6_deck_run/firings.json。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from example.ppt_master.module import run_generate

HERE = Path(__file__).resolve().parent
SPEC = json.loads((HERE / "spec.mbgs_v6.json").read_text(encoding="utf-8"))

ARTICLE_FIGS = Path("E:/Index/Research/1fe1d8be9b9c/Draft/v8/file")
USER_FILES = {
    "fig1.png": "fig1_performance.png",
    "fig2.png": "fig2_morphology.png",
    "fig3.png": "fig3_function.png",
    "fig4.png": "fig4_mechanism.png",
    "fig5.png": "fig5_community.png",
}
OUT_ROOT = Path(SPEC["output"]["dir"])
RUN_DIR = HERE.parent / "projects" / "mbgs_v6_deck_run"


def _preset_user_images() -> None:
    (OUT_ROOT / "images").mkdir(parents=True, exist_ok=True)
    for src, dst in USER_FILES.items():
        shutil.copy2(ARTICLE_FIGS / src, OUT_ROOT / "images" / dst)
    print(f"[preset] user images -> {OUT_ROOT / 'images'}", flush=True)


def main() -> None:
    _preset_user_images()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    firings = asyncio.run(run_generate(SPEC, persist=False))

    rows = []
    for f in firings:
        out = f.output
        rows.append({"node": f.node, "output": out})
        brief = out if isinstance(out, dict) else str(out)
        print(f"[fired] {f.node}: {json.dumps(brief, ensure_ascii=False, default=str)[:400]}",
              flush=True)
    (RUN_DIR / "firings.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[done] firings -> {RUN_DIR / 'firings.json'}", flush=True)


if __name__ == "__main__":
    main()
