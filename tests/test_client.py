"""Client integration regressions without Windows or real audio/network devices."""
import ast
import asyncio
import json
import queue
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from speech import AnnouncementQueue
from test_relay import MemoryLoop, Peer, Relay

SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
ENGINE_CODE = ast.Module(body=[node for node in TREE.body
    if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in ("Engine", "put_latest")], type_ignores=[])
NAMESPACE = dict(np=np, asyncio=asyncio, json=json, queue=queue, threading=threading,
                 time=time, uuid=uuid, AnnouncementQueue=AnnouncementQueue,
                 RATE=48000, FRAMES=960,
                 pythoncom=SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None))
exec(compile(ENGINE_CODE, "app.py", "exec"), NAMESPACE)
Engine, put_latest = NAMESPACE["Engine"], NAMESPACE["put_latest"]


def engine():
    instance = Engine()
    instance.traces = []
    instance.trace = lambda event, **fields: instance.traces.append((event, fields))
    return instance


class ClientTests(unittest.TestCase):
    def test_live_reset_preserves_speech_queue_generation_and_dedup(self):
        receiver = engine()
        receiver.queue_announcement("Current", "current-id")
        current = receiver.announcements.take(timeout=0)
        receiver.queue_announcement("Pending", "pending-id")
        receiver.outgoing.put_nowait((1, b"audio"))
        receiver.controls.put_nowait((1, {"type": "stop"}))
        receiver.clear_live()
        self.assertTrue(receiver.announcements.valid(current[0]))
        self.assertEqual(receiver.announcements.take(timeout=0)[1:], ("pending-id", "Pending"))
        self.assertFalse(receiver.queue_announcement("Current", "current-id"))
        self.assertTrue(receiver.outgoing.empty())
        self.assertTrue(receiver.controls.empty())

    def test_audio_disable_cancels_current_and_pending(self):
        receiver = engine()
        receiver.queue_announcement("Current", "current-id")
        current = receiver.announcements.take(timeout=0)
        receiver.queue_announcement("Pending", "pending-id")
        receiver.clear("audio_disabled")
        self.assertFalse(receiver.announcements.valid(current[0]))
        self.assertEqual(receiver.announcements.cancel_reason, "audio_disabled")
        self.assertIsNone(receiver.announcements.take(timeout=0))

    def test_manual_disconnect_cancels_speech(self):
        receiver = engine()
        receiver.queue_announcement("Current", "current-id")
        current = receiver.announcements.take(timeout=0)
        receiver.close()
        self.assertTrue(receiver.stop.is_set())
        self.assertFalse(receiver.announcements.valid(current[0]))
        self.assertEqual(receiver.announcements.cancel_reason, "manual_disconnect")

    def test_live_audio_overflow_cannot_evict_control_messages(self):
        sender = engine()
        sender.connected = sender.ready = True
        self.assertTrue(sender.send_command({"type": "say", "text": "Important announcement"}))
        for index in range(100):
            put_latest(sender.outgoing, (index, bytes([index])))
        self.assertEqual(sender.controls.qsize(), 1)
        self.assertEqual(sender.controls.get_nowait()[1]["text"], "Important announcement")
        self.assertEqual(sender.outgoing.qsize(), 8)

    def test_duplicate_click_is_not_a_second_control(self):
        sender = engine()
        sender.connected = sender.ready = True
        command = {"type": "say", "text": "Testing one two three four five"}
        self.assertTrue(sender.send_command(command))
        self.assertTrue(sender.send_command(command))
        self.assertEqual(sender.controls.qsize(), 1)

    def test_live_player_does_not_mix_or_consume_native_speech(self):
        receiver = engine()
        receiver.enabled = True
        receiver.queue_announcement("Native speech", "native-id")
        samples = np.linspace(0, .5, 960, dtype=np.float32)
        receiver.buffers[0].put_nowait((time.monotonic(), samples))
        writes = []
        class Player:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        old_sc, old_write = NAMESPACE.get("sc"), NAMESPACE.get("write_audio")
        NAMESPACE["sc"] = SimpleNamespace(get_speaker=lambda _: SimpleNamespace(player=lambda **kw: Player()))
        def write_audio(player, stereo, stop):
            writes.append(stereo.copy())
            stop.set()
        NAMESPACE["write_audio"] = write_audio
        try:
            receiver.play("chosen-device")
        finally:
            NAMESPACE["sc"], NAMESPACE["write_audio"] = old_sc, old_write
        self.assertFalse(receiver.playback_failed)
        self.assertEqual(len(writes), 1)
        np.testing.assert_allclose(writes[0][:, 0], samples * .8)
        self.assertEqual(receiver.announcements.take(timeout=0)[1:], ("native-id", "Native speech"))


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(loop_factory=MemoryLoop)

    async def test_sender_identifier_survives_relay_and_receiver_deduplicates(self):
        sender, receiver = engine(), engine()
        sender.connected = sender.ready = True
        text = "Qualifying round one starts in ten minutes. Prepare your cars."
        sender.send_command({"type": "say", "text": text})
        _, payload = sender.controls.get_nowait()
        relay = Relay("s" * 32, "r" * 32)
        sender_peer = Peer("sender", "s" * 32)
        receiver_peer = Peer("receiver", "r" * 32)
        relay.peers["receiver"] = receiver_peer
        relay.enabled = True
        sender_peer.push(payload)
        from test_relay import END
        sender_peer.inbox.put_nowait(END)
        await relay.handler(sender_peer)
        forwarded = [json.loads(value) for value in receiver_peer.sent if isinstance(value, str)]
        forwarded = [value for value in forwarded if value.get("type") == "say"]
        self.assertEqual(forwarded, [payload])
        command = forwarded[0]
        self.assertTrue(receiver.queue_announcement(command["text"], command["id"]))
        self.assertFalse(receiver.queue_announcement(command["text"], command["id"]))
        self.assertEqual(receiver.announcements.take(timeout=0)[1:], (payload["id"], text))
        self.assertIsNone(receiver.announcements.take(timeout=0))

    async def test_network_receive_duplicates_and_disconnect_keep_one_accepted_speech(self):
        receiver = engine()
        receiver.enabled = True
        text = "An announcement that should finish even after the network closes."
        class Socket:
            def __init__(self):
                self.messages = [json.dumps({"type": "say", "text": text, "id": "one-id"})] * 2
            async def send(self, value): pass
            async def recv(self): return json.dumps({"type": "status", "enabled": True})
            async def close(self): pass
            def __aiter__(self): return self
            async def __anext__(self):
                await asyncio.sleep(0)
                if self.messages: return self.messages.pop(0)
                raise StopAsyncIteration
        class Connection:
            async def __aenter__(self): return Socket()
            async def __aexit__(self, *args): receiver.stop.set()
        old_connect = NAMESPACE.get("connect")
        NAMESPACE["connect"] = lambda *args, **kwargs: Connection()
        try:
            await receiver.network("wss://relay", "secret", "receiver")
        finally:
            NAMESPACE["connect"] = old_connect
        item = receiver.announcements.take(timeout=0)
        self.assertEqual(item[1:], ("one-id", text))
        self.assertTrue(receiver.announcements.valid(item[0]))
        self.assertIsNone(receiver.announcements.take(timeout=0))
        self.assertIn("speech_duplicate", [event for event, _ in receiver.traces])


if __name__ == "__main__":
    unittest.main()
