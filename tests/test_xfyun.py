"""Offline fixtures only; these tests are authored but not executed in this delivery."""

import asyncio
import base64
import hashlib
import hmac
import json
import queue
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import numpy as np

from oopz_mcp.config import ASRConfig, AudioConfig
from oopz_mcp.playback import Playback
from oopz_mcp.xfyun import (
    ASRError,
    Credentials,
    XFYunASR,
    load_credentials,
    parse_message,
    signed_url,
)


def result(text="你好。", *, segment=0, final=True, ended=False, end_ms=40):
    return json.dumps(
        {
            "msg_type": "result",
            "res_type": "asr",
            "data": {
                "seg_id": segment,
                "ls": ended,
                "cn": {
                    "st": {
                        "type": "0" if final else "1",
                        "bg": 0,
                        "ed": end_ms,
                        "rt": [{"ws": [{"cw": [{"w": text, "wp": "n"}, {"w": "unused"}]}]}],
                    }
                },
            },
        }
    )


class ProtocolTests(unittest.TestCase):
    def test_signing_matches_literal_canonical_query_and_encodes_timezone(self):
        config = ASRConfig()
        url = signed_url(
            config,
            Credentials("fixture-app", "fixture-key", "fixture-secret"),
            now=datetime(2026, 10, 23, tzinfo=timezone(timedelta(hours=8))),
            request_uuid="fixture-id",
        )
        canonical = (
            "accessKeyId=fixture-key&appId=fixture-app&audio_encode=pcm_s16le"
            "&lang=autodialect&role_type=0&samplerate=16000"
            "&utc=2026-10-23T00%3A00%3A00%2B0800&uuid=fixture-id"
        )
        expected = base64.b64encode(
            hmac.new(b"fixture-secret", canonical.encode(), hashlib.sha1).digest()
        ).decode()
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["signature"], [expected])
        self.assertEqual(query["utc"], ["2026-10-23T00:00:00+0800"])
        self.assertNotIn("fixture-secret", url)
        self.assertNotIn("fixture-key", repr(Credentials("a", "fixture-key", "s")))

    def test_credentials_file_and_environment_have_no_fallback_on_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.txt"
            path.write_text("APPID：fixture-app\nAPIKey: fixture-key\nAPISecret：fixture-secret\n")
            config = ASRConfig(credentials_file=str(path))
            self.assertEqual(load_credentials(config).api_key, "fixture-key")
            path.write_text("APPID: a\nAPPID: b\n")
            with self.assertRaisesRegex(ASRError, "duplicate_credential"):
                load_credentials(config)
            path.write_text("APPID: a\n")
            with self.assertRaisesRegex(ASRError, "missing_or_invalid"):
                load_credentials(config)
        with patch.dict(
            "os.environ",
            {"XFYUN_APP_ID": "a", "XFYUN_API_KEY": "k", "XFYUN_API_SECRET": "s"},
            clear=True,
        ):
            self.assertEqual(load_credentials(ASRConfig()).app_id, "a")

    def test_both_ready_envelopes_and_json_string_result_data(self):
        for raw in (
            '{"action":"started","code":"0","sid":"session-1"}',
            '{"msg_type":"action","data":{"sessionId":"session-1"}}',
        ):
            self.assertEqual(parse_message(raw), {"kind": "ready", "session_id": "session-1"})
        message = json.loads(result())
        raw = json.dumps({"action": "result", "data": json.dumps(message["data"])})
        self.assertEqual(parse_message(raw)["text"], "你好。")

    def test_stable_sentence_is_independent_of_stream_end(self):
        self.assertEqual(parse_message(result(ended=False))["kind"], "final")
        interim = parse_message(result(final=False))
        self.assertEqual(interim["kind"], "partial")
        self.assertFalse(interim["stream_final"])
        self.assertTrue(parse_message(result(ended=True))["stream_final"])

    def test_errors_and_malformed_data_are_sanitized(self):
        for raw, code in (
            ('{"code":35001,"desc":"private signed URL"}', "xfyun_35001"),
            (
                '{"msg_type":"result","res_type":"frc","data":{"normal":false}}',
                "xfyun_service_error",
            ),
            ('{"msg_type":"result","data":{}}', "xfyun_invalid_message"),
            ('["private content"]', "xfyun_invalid_message"),
        ):
            with self.assertRaisesRegex(ASRError, code) as captured:
                parse_message(raw)
            self.assertNotIn("private", str(captured.exception))


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self):
        self.events = []
        self.gate = Playback(32000, 20, 80)
        self.rx = queue.Queue(maxsize=50)
        self.adapter = XFYunASR(
            ASRConfig(),
            AudioConfig(rx_device="RX", tx_device="TX"),
            self.rx,
            self.gate,
            self.events.append,
        )
        self.socket = AsyncMock()
        self.adapter.connection = self.socket
        self.adapter.session_id = "real-service-session-fixture"
        return self.adapter

    async def test_only_stable_results_emit_text_and_duplicate_final_does_not_repeat(self):
        adapter = self.make_adapter()
        adapter.sent_frames = 25
        self.socket.recv.side_effect = [result(final=False), result(), result(), ASRError("done")]
        with self.assertRaisesRegex(ASRError, "done"):
            await adapter._receive()
        texts = [e["text"] for e in self.events if e["type"] == "transcript"]
        self.assertEqual(texts, ["你好。"])
        self.assertTrue(self.gate.speech_pending)

    async def test_conflicting_final_and_unexpected_end_are_explicit(self):
        for messages, code in (
            ([result(), result("different")], "final_conflict"),
            ([result(), result(ended=True)], "stream_ended"),
        ):
            adapter = self.make_adapter()
            adapter.sent_frames = 25
            self.socket.recv.side_effect = messages
            with self.assertRaisesRegex(ASRError, code):
                await adapter._receive()

    async def test_sentence_end_waits_for_silence_and_cannot_clear_later_voice(self):
        adapter = self.make_adapter()
        adapter.input_open = adapter.completion_needed = True
        adapter.last_final_ms = 100
        adapter.last_voice_ms = 80
        adapter.last_voice_at = 10
        adapter.last_capture_at = 10.1
        self.gate.voice_evidence(10)
        adapter._complete_if_ready()
        self.assertEqual(self.events, [])
        adapter.last_capture_at = 10.4
        self.gate.voice_evidence(10.5)
        adapter._complete_if_ready()
        self.assertEqual(self.events, [])
        adapter.last_capture_at = 11
        adapter.last_voice_ms = 120
        adapter._complete_if_ready()
        self.assertEqual(self.events, [])
        adapter.last_final_ms = 140
        adapter._complete_if_ready()
        self.assertEqual(self.events[0]["type"], "speech_end")
        self.assertTrue(self.events[0]["notify"])
        adapter._complete_if_ready()
        self.assertEqual(len(self.events), 1)

    async def test_pcm_is_1280_byte_binary_frames_and_stop_uses_service_session(self):
        adapter = self.make_adapter()
        for _ in range(4):
            self.rx.put_nowait((np.zeros(320, dtype="float32"), False, time.monotonic()))
        sent = asyncio.Event()
        frames = []

        async def record(data):
            if isinstance(data, bytes):
                frames.append(data)
                if len(frames) == 2:
                    sent.set()

        self.socket.send.side_effect = record
        task = asyncio.create_task(adapter._send())
        adapter.tasks = [task]
        try:
            await asyncio.wait_for(sent.wait(), 1)
            self.assertEqual([len(frame) for frame in frames], [1280, 1280])
        finally:
            await adapter.close()
        end = json.loads(self.socket.send.call_args.args[0])
        self.assertEqual(end, {"end": True, "sessionId": "real-service-session-fixture"})
        self.socket.close.assert_awaited_once()

    async def test_partial_without_local_energy_still_has_a_final_deadline(self):
        adapter = self.make_adapter()
        self.socket.recv.side_effect = [result(final=False), ASRError("done")]
        with self.assertRaisesRegex(ASRError, "done"):
            await adapter._receive()
        self.assertGreater(adapter.pending_since, 0)
        adapter.pending_since = time.monotonic() - 20
        self.rx.put_nowait((np.zeros(320, dtype="float32"), False, time.monotonic()))
        with self.assertRaisesRegex(ASRError, "final_timeout"):
            await adapter._send()

    async def test_old_result_does_not_time_new_voice_as_its_endpoint(self):
        adapter = self.make_adapter()
        adapter.sent_frames = 25
        adapter.last_voice_ms = 100
        adapter.last_voice_at = time.monotonic()
        self.socket.recv.side_effect = [result(end_ms=40), ASRError("done")]
        with self.assertRaisesRegex(ASRError, "done"):
            await adapter._receive()
        self.assertEqual(self.events[0]["last_voice_at"], 0)
        self.assertFalse(self.events[0]["final"])

    async def test_close_attempts_socket_close_when_end_marker_fails(self):
        adapter = self.make_adapter()
        adapter.sent_frames = 1
        self.socket.send.side_effect = OSError("fixture failure")
        await adapter.close()
        self.socket.close.assert_awaited_once()

    async def test_listener_receives_stable_results_but_partial_cannot_pause_playback(self):
        adapter = self.make_adapter()
        finals = []
        adapter.listening = SimpleNamespace(feed_final=finals.append)
        adapter.sent_frames = 25
        self.gate.begin()
        self.socket.recv.side_effect = [result(final=False), result(), result(), ASRError("done")]
        with self.assertRaisesRegex(ASRError, "done"):
            await adapter._receive()
        self.assertFalse(self.gate.speech_pending)
        self.assertTrue(self.gate.active)
        self.assertEqual(self.events, [])
        self.assertEqual(len(finals), 1)
        self.assertEqual(
            (finals[0]["start_ms"], finals[0]["end_ms"], finals[0]["text"]), (0, 40, "你好。")
        )
        self.assertFalse(adapter.input_open)
        self.assertEqual(adapter.pending_since, 0.0)

    async def test_listener_receives_original_frame_timeline_without_asr_playback_control(self):
        adapter = self.make_adapter()
        frames = []
        sent = asyncio.Event()
        adapter.listening = SimpleNamespace(feed_frame=lambda *args: frames.append(args))
        for index in range(2):
            self.rx.put_nowait((np.full(320, 0.2, dtype="float32"), True, 10.0 + index * 0.02))

        async def record(_):
            sent.set()

        self.socket.send.side_effect = record
        task = asyncio.create_task(adapter._send())
        adapter.tasks = [task]
        try:
            await asyncio.wait_for(sent.wait(), 1)
            self.assertEqual(
                [(frame[1], frame[2], frame[3], frame[4]) for frame in frames],
                [(True, 10.0, 0, 20), (True, 10.02, 20, 40)],
            )
            self.assertFalse(self.gate.speech_pending)
            self.assertFalse(adapter.input_open)
            self.assertEqual(self.events, [])
        finally:
            await adapter.close()
