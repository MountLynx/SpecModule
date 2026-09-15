"""测试辅助：覆盖全条件段的样例 spec（10 页、ai 图像、notes 开）。

10 页 > 6 → 早门全链（EarlyGate/EarlyRepair/EarlyDispatch + 批2 扇出）；
images.sources 非空 → ImageAcquire + ImageReadiness；production 缺省 →
speaker_notes=True → NotesGen/SplitNotes/ppt_export。全部条件段一次覆盖。
"""

from __future__ import annotations

from typing import Any


def sample_spec() -> dict[str, Any]:
    return {
        "project": "support",
        "source": {"kind": "files", "paths": ["a.md"]},
        "images": {"sources": ["ai", "user"]},
        "roster": [{"id": f"p{i:02d}", "title": f"页{i}"} for i in range(1, 11)],
    }
