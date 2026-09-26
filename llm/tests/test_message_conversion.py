# llm/tests/test_message_conversion.py
"""chat() 多轮接口的消息转换：工具循环（assistant+tool_calls / tool 角色）必须
在两个后端都正确映射——Anthropic 走 tool_use / tool_result 内容块。

回归锚点：此前 Anthropic 侧 tool 消息原样透传（{"role": "tool", ...}），会被
Anthropic API 拒绝——ops agent 工具循环（webview 仓库）依赖此形状。
"""

from __future__ import annotations

from llm.client import AnthropicClient, Message, OpenAIClient


def _anthropic() -> AnthropicClient:
    # 跳过 __init__：_convert_messages 是纯函数，不依赖 SDK 安装/客户端就绪
    return AnthropicClient.__new__(AnthropicClient)


class TestAnthropicConversion:
    def test_tool_result_maps_to_user_block(self):
        msgs = [Message(role="tool", content='{"ok": true}', tool_call_id="tc_1")]
        _, out = _anthropic()._convert_messages(msgs)
        assert out == [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tc_1", "content": '{"ok": true}'}]}]

    def test_assistant_tool_calls_map_to_tool_use_blocks(self):
        msgs = [Message(role="assistant", content="查一下", tool_calls=[
            {"id": "tc_1", "name": "run_status", "arguments": {"run_id": "r1"}}])]
        _, out = _anthropic()._convert_messages(msgs)
        assert out == [{"role": "assistant", "content": [
            {"type": "text", "text": "查一下"},
            {"type": "tool_use", "id": "tc_1", "name": "run_status",
             "input": {"run_id": "r1"}}]}]

    def test_system_separated_and_plain_roundtrip(self):
        msgs = [Message(role="system", content="s"), Message(role="user", content="u")]
        system, out = _anthropic()._convert_messages(msgs)
        assert system == "s"
        assert out == [{"role": "user", "content": "u"}]

    def test_consecutive_tools_merge_into_one_user_message(self):
        # Anthropic 要求消息角色交替：连续 tool 消息必须聚合为一条 user 消息
        # （多个 tool_result 块），逐条拆开会被服务端 400（Bedrock 实测）。
        msgs = [
            Message(role="tool", content='{"phase": "done"}', tool_call_id="tc_1"),
            Message(role="tool", content='{"tick": 3}', tool_call_id="tc_2"),
        ]
        _, out = _anthropic()._convert_messages(msgs)
        assert out == [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tc_1", "content": '{"phase": "done"}'},
            {"type": "tool_result", "tool_use_id": "tc_2", "content": '{"tick": 3}'},
        ]}]

    def test_assistant_two_tool_calls_two_tool_use_blocks(self):
        msgs = [Message(role="assistant", content="并行查", tool_calls=[
            {"id": "tc_1", "name": "run_status", "arguments": {"run_id": "r1"}},
            {"id": "tc_2", "name": "run_timeline", "arguments": {"run_id": "r1"}},
        ])]
        _, out = _anthropic()._convert_messages(msgs)
        assert out == [{"role": "assistant", "content": [
            {"type": "text", "text": "并行查"},
            {"type": "tool_use", "id": "tc_1", "name": "run_status", "input": {"run_id": "r1"}},
            {"type": "tool_use", "id": "tc_2", "name": "run_timeline", "input": {"run_id": "r1"}},
        ]}]

    def test_assistant_empty_content_yields_only_tool_use_blocks(self):
        msgs = [Message(role="assistant", content="", tool_calls=[
            {"id": "tc_1", "name": "run_status", "arguments": {"run_id": "r1"}}])]
        _, out = _anthropic()._convert_messages(msgs)
        assert out == [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "tc_1", "name": "run_status",
             "input": {"run_id": "r1"}}]}]


class TestOpenAIConversion:
    def test_tool_roundtrip_keeps_ids(self):
        msgs = [
            Message(role="assistant", content="", tool_calls=[
                {"id": "tc_1", "name": "run_status", "arguments": {"run_id": "r1"}}]),
            Message(role="tool", content='{"phase": "done"}', tool_call_id="tc_1"),
        ]
        out = OpenAIClient.__new__(OpenAIClient)._convert_messages(msgs)
        assert out[0]["tool_calls"][0]["id"] == "tc_1"
        assert out[1] == {"role": "tool", "tool_call_id": "tc_1",
                          "content": '{"phase": "done"}'}
