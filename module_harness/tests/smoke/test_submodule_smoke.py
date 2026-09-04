"""submodule 真实冒烟测试：CommitDigest module（command + script + harness + script 链）。

覆盖两层用户真实体验：
- 第一层（module 开发者）：类式定义 CommitDigest，直接运行
- 第二层（module 使用者）：pack() 导出 → ModuleLoader.load() → 只写 spec 运行
- 附加：自定义 tasklist → 一致性审核（真实 LLM）→ 执行

图中结构：A(command: git log) → B(script: 提取 stdout) → C(harness: LLM 摘要)
→ D(script: 格式化)。
"""

import pytest

from module_harness import (
    CommandConfig,
    HarnessConfig,
    ModuleLoader,
    OutputFormat,
    SpecSchema,
    SubModule,
    TaskDefinition,
    Tasklist,
    script,
)
from module_harness.model.submodule import SpecValidationError

pytestmark = pytest.mark.smoke


class CommitDigest(SubModule):
    """把最近的 git 提交总结成一份 digest 的 module。"""

    name = "commit_digest"
    version = "1.0.0"
    description = "把最近的 git 提交总结成一份 digest"

    spec_schema = SpecSchema(
        input={"style": "str"},
        output={"digest": "str", "highlights": "list"},
    )

    commands = [
        CommandConfig(name="git_log", command="git log --oneline -5", timeout=30),
    ]

    harnesses = [
        HarnessConfig(
            name="digest_commits",
            prompt_core=(
                "你是提交记录摘要器。根据以下 git log 输出 JSON："
                '{"digest": "一段话总结", "highlights": ["亮点1", "亮点2"]}。'
                "\n日志：\n{log}"
            ),
            prompt_modes={
                "concise": "摘要控制在 50 字以内",
                "detailed": "摘要可以展开到 200 字",
            },
            output_format=OutputFormat(type="json_object"),
            temperature=0.3,
        ),
    ]

    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(type="command", command="git_log", timeout=30),
            "B": TaskDefinition(type="script", script="extract_log", inputs={"data": "A"}),
            "C": TaskDefinition(
                type="harness", harness="digest_commits",
                promptmode="{spec.style}",
                inputs={"log": "B"},
                outputformat={"type": "json_object"},
            ),
            "D": TaskDefinition(type="script", script="format_digest", inputs={"data": "C"}),
        },
        flow="A --> B\nB --> C\nC --> D",
    )

    @script("extract_log")
    def extract_log(view):
        return view.A.value["stdout"]

    @script("format_digest")
    def format_digest(view):
        data = view.C.value
        return {"digest": data["digest"].strip(), "highlights": data.get("highlights", [])}


SPEC = {"style": "concise"}


def _assert_digest(firings):
    """断言四节点链全部成功且最终输出含 digest。"""
    assert len(firings) >= 4
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"
    final = firings[-1].output
    assert "digest" in final, f"输出缺 digest: {final}"
    assert "highlights" in final
    assert len(final["digest"]) > 0
    return final


@pytest.mark.asyncio
async def test_first_layer_direct_run(llm_client, event_bus):
    """第一层开发者体验：类式定义 + 完整模式（audit=True，事件全开）。"""
    module = CommitDigest(llm_client=llm_client, event_bus=event_bus)
    firings = await module.run(SPEC, audit=True, max_ticks=20)
    final = _assert_digest(firings)
    # 完整模式应录到 command / script 事件
    names = {type(e).__name__ for e in event_bus.recorded}
    assert "CommandCompleted" in names, f"缺 CommandCompleted: {names}"
    assert "ScriptCompleted" in names, f"缺 ScriptCompleted: {names}"
    print(f"\n[direct] digest={final['digest'][:40]}... highlights={len(final['highlights'])}")


@pytest.mark.asyncio
async def test_pack_then_second_layer_run(llm_client, tmp_path):
    """第二层用户体验：pack 导出 → ModuleLoader 加载 → 只写 spec 运行（嵌入模式）。"""
    out = CommitDigest().pack(tmp_path / "dist")
    module = ModuleLoader(llm_client=llm_client).load(out)
    firings = await module.run(SPEC, max_ticks=20)
    final = _assert_digest(firings)
    print(f"\n[loaded] digest={final['digest'][:40]}... highlights={len(final['highlights'])}")


@pytest.mark.asyncio
async def test_loaded_spec_validation_fails(llm_client, tmp_path):
    """第二层快速失败：spec 缺字段 → SpecValidationError（不触发 LLM）。"""
    out = CommitDigest().pack(tmp_path / "dist")
    module = ModuleLoader(llm_client=llm_client).load(out)
    with pytest.raises(SpecValidationError, match="style"):
        await module.run({})


@pytest.mark.asyncio
async def test_loaded_custom_tasklist_with_review(llm_client, tmp_path):
    """第二层自定义 tasklist：一致性审核（真实 LLM）通过后执行。

    自定义流程 A --> B --> C（去掉格式化节点 D）。
    """
    out = CommitDigest().pack(tmp_path / "dist")
    module = ModuleLoader(llm_client=llm_client).load(out)
    custom = Tasklist(
        tasks={
            "A": TaskDefinition(type="command", command="git_log", timeout=30),
            "B": TaskDefinition(type="script", script="extract_log", inputs={"data": "A"}),
            "C": TaskDefinition(
                type="harness", harness="digest_commits",
                promptmode="{spec.style}",
                inputs={"log": "B"},
                outputformat={"type": "json_object"},
            ),
        },
        flow="A --> B\nB --> C",
    )
    firings = await module.run(SPEC, tasklist=custom, max_ticks=20)
    assert any(f.node == "C" for f in firings)
    c_out = next(f.output for f in firings if f.node == "C")
    assert "digest" in c_out, f"输出缺 digest: {c_out}"
    print(f"\n[review+custom] digest={c_out['digest'][:40]}...")
