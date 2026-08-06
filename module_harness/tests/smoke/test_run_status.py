# module_harness/tests/smoke/test_run_status.py
"""运行状态查询真实 LLM 端到端（1 次 LLM 调用）。

Module.run() 期间与结束后，用 query_run_status 跨"进程"查询当前状态
（本测试同进程模拟，但走的是与跨进程相同的磁盘通道 status.json + run.sqlite）。
探针模式——通过即说明端到端通，失败记录问题不阻塞。
"""

import asyncio

import pytest

from module_harness.config import HarnessConfig, OutputFormat
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist
from module_harness.status import query_run_status

pytestmark = pytest.mark.smoke


@pytest.mark.xfail(
    reason="Task 11: query_run_status 迁移到 firings 后修复",
    strict=False,
)
@pytest.mark.asyncio
async def test_run_status_during_and_after_run(llm_client, tmp_path, monkeypatch):
    """运行中 phase=running（可轮询），运行后 phase=done + tick 级快照完整。

    xfail：快照已最小化（S3），outputs/node_states 改由 firings 提供
    （Task 11 迁移后移除 xfail）。
    """
    monkeypatch.chdir(tmp_path)

    reg = HarnessRegistry(llm_client=llm_client)
    reg.harness("translate", HarnessConfig(
        prompt_core=(
            "将以下英文翻译为中文，只输出 JSON: "
            '{"translation": "..."}：{text}'
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))
    tl = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                inputs={"text": "{spec.source_text}"},
            ),
        },
        flow="[A]",
    )
    mod = Module(
        spec={"source_text": "Hello world, this is a run-status smoke test."},
        tasklist=tl,
        llm_client=llm_client,
        registry=reg,
        review_harness=None,
        module_id="smoke_status",
    )

    task = asyncio.create_task(mod.run())

    # 运行中轮询：真实 LLM 调用有延迟，应能观察到 running 阶段
    observed_running = False
    for _ in range(200):  # 最多 10s
        st = query_run_status("smoke_status", base_dir=tmp_path)
        if st is not None and st.phase == "running":
            observed_running = True
            break
        await asyncio.sleep(0.05)

    assert observed_running, "运行中未能观察到 phase=running（LLM 过快或 status.json 未写）"

    await task

    # 运行后：phase=done + tick 级快照叠加（真实快照，run_state 嵌套结构）
    st = query_run_status("smoke_status", base_dir=tmp_path)
    assert st is not None
    assert st.phase == "done"
    assert st.tick is not None
    assert st.status == "idle"          # 运行结束后 runner status
    assert "A" in st.outputs, f"快照 outputs 应含节点 A: {st.outputs!r}"
    assert st.outputs["A"]["translation"], f"A 输出应含 translation: {st.outputs['A']!r}"
    assert "_llm_raw" in st.node_states["A"], "审计链 _llm_raw 应存在于节点状态"

    # status.json 文件本体字段完整
    import json
    from pathlib import Path

    status_path = tmp_path / ".specmodule" / "runs" / "smoke_status" / "status.json"
    assert status_path.exists()
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["module_id"] == "smoke_status"
    assert data["phase"] == "done"
    assert data["updated_at"] > 0
