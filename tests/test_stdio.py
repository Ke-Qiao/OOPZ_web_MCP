"""Real stdio protocol, mock-only audio. Run only after opting into functional tests."""

import asyncio
import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MOCK_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "mock.toml"


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_handshake_and_five_tools(self):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "oopz_mcp.server", "--config", str(MOCK_CONFIG)]
        )
        async with asyncio.timeout(15):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertEqual(
                        {tool.name for tool in tools.tools},
                        {
                            "voice_open",
                            "voice_exchange",
                            "voice_get_state",
                            "voice_stop_output",
                            "voice_close",
                        },
                    )
                    for tool in tools.tools:
                        self.assertIsNotNone(tool.outputSchema, tool.name)
                    opened = await client.call_tool("voice_open", {"profile": "mock"})
                    self.assertFalse(opened.isError)
                    assert opened.structuredContent is not None
                    sid = opened.structuredContent["session_id"]
                    waited = await client.call_tool(
                        "voice_exchange",
                        {
                            "session_id": sid,
                            "request_id": "stdio-1",
                            "ack_event_seq": 0,
                            "reply": None,
                            "wait_ms": 250,
                        },
                    )
                    self.assertFalse(waited.isError)
                    assert waited.structuredContent is not None
                    self.assertEqual(waited.structuredContent["reason"], "timeout")
                    state = await client.call_tool("voice_get_state", {"session_id": sid})
                    self.assertFalse(state.isError)
                    self.assertIsNotNone(state.structuredContent)
                    stopped = await client.call_tool("voice_stop_output", {"session_id": sid})
                    self.assertFalse(stopped.isError)
                    self.assertIsNotNone(stopped.structuredContent)
                    closed = await client.call_tool("voice_close", {"session_id": sid})
                    self.assertFalse(closed.isError)
                    self.assertIsNotNone(closed.structuredContent)


if __name__ == "__main__":
    unittest.main()
