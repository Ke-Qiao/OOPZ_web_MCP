"""Simulated host delays; these measurements are never labelled live-room results."""

import asyncio
import time
import unittest

from oopz_mcp.backends import MockBackend
from oopz_mcp.config import Exchange, Profile, Reply, Settings
from oopz_mcp.session import Session


class SimulatedHostDelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_half_two_and_five_second_host_delay_cannot_be_hidden(self):
        for host_delay in (0.5, 2.0, 5.0):
            settings = Settings()
            backend = MockBackend(Profile(), settings.limits)
            session = Session("mock", Profile(), settings, backend)
            await session.start()
            try:
                backend.inject("模拟问题")
                first = await session.exchange(
                    Exchange(
                        session_id=session.id, request_id="listen", ack_event_seq=0, wait_ms=250
                    )
                )
                result_at = time.monotonic()
                await asyncio.sleep(host_delay)
                reply = Reply(
                    reply_id="answer",
                    reply_to_turn_id="u1",
                    based_on_input_revision=1,
                    text="模拟的完整回复",
                )
                await session.exchange(
                    Exchange(
                        session_id=session.id,
                        request_id="answer",
                        ack_event_seq=first["next_event_seq"],
                        reply=reply,
                        wait_ms=250,
                    )
                )
                at = session.playback.first_output_at
                self.assertIsNotNone(at)
                assert at is not None
                self.assertGreaterEqual(at - result_at, host_delay)
                self.assertEqual(backend.synthesis_count, 1)
            finally:
                await session.close()


if __name__ == "__main__":
    unittest.main()
