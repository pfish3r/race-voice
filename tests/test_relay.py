"""Isolated relay logic tests. Stubs transport only if websockets isn't installed."""
import asyncio
import importlib.util
import json
import sys
import types
import unittest
import selectors
import time

class MemorySelector(selectors.BaseSelector):
    def register(self, *args, **kwargs):
        raise RuntimeError("No network I/O in isolated tests")
    def unregister(self, *args):
        raise KeyError()
    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, .01))
        return []
    def get_map(self):
        return {}
    def close(self):
        pass

class MemoryLoop(asyncio.SelectorEventLoop):
    """No sockets needed for in-memory protocol tests in restricted environments."""
    def __init__(self):
        super().__init__(MemorySelector())
    def _make_self_pipe(self):
        pass
    def _close_self_pipe(self):
        pass
    def _write_to_self(self):
        pass

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if importlib.util.find_spec("websockets") is None:
    for name in ["websockets", "websockets.asyncio", "websockets.asyncio.server", "websockets.exceptions"]:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["websockets.asyncio.server"].serve = None
    sys.modules["websockets.exceptions"].ConnectionClosed = type("ConnectionClosed", (Exception,), {})
from relay import Relay

END = object()

class Peer:
    def __init__(self, role, key):
        self.inbox = asyncio.Queue()
        self.inbox.put_nowait(json.dumps({"role": role, "key": key}))
        self.sent = []
        self.closed = None
    async def recv(self):
        return await self.inbox.get()
    async def send(self, data):
        self.sent.append(data)
    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        self.inbox.put_nowait(END)
    def __aiter__(self):
        return self
    async def __anext__(self):
        data = await self.inbox.get()
        if data is END:
            raise StopAsyncIteration
        return data
    def push(self, value):
        self.inbox.put_nowait(value if isinstance(value, (bytes, str)) else json.dumps(value))

class RelayTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        self._asyncioRunner = asyncio.Runner(loop_factory=MemoryLoop)

    async def asyncSetUp(self):
        self.relay = Relay("s"*32, "r"*32)
        self.tasks = []
    async def add(self, role, key=None):
        p = Peer(role, key or ("s"*32 if role == "sender" else "r"*32))
        self.tasks.append(asyncio.create_task(self.relay.handler(p)))
        await self.settle()
        return p
    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0)
    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
    async def test_stats_count_forwarding_without_disclosing_keys(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        audio = b"\0" + b"\0"*1920
        sender.push(audio)
        await self.settle()
        receiver.push({"type":"enabled","value":True})
        await self.settle()
        sender.push(audio)
        await self.settle()
        snapshot = self.relay.snapshot()
        self.assertEqual(snapshot["stats"]["audio_bytes_in"],3840)
        self.assertEqual(snapshot["stats"]["audio_bytes_out"],1920)
        self.assertEqual(snapshot["stats"]["frames_dropped"],1)
        self.assertEqual(snapshot["connections"]["sender"],1)
        self.assertNotIn("s"*32,json.dumps(snapshot))

    async def test_metrics_distinguish_received_events_from_forwarding_and_utf8_payload(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        sender.push({"type":"say","text":"not forwarded"})
        await self.settle()
        self.assertEqual(self.relay.stats["events_received"],1)
        self.assertEqual(self.relay.stats["events_forwarded"],0)
        receiver.push({"type":"enabled","value":True})
        await self.settle()
        command = json.dumps({"type":"say","text":"Pratique caf\u00e9"}, ensure_ascii=False)
        sender.push(command)
        await self.settle()
        sender.push({"type":"stop"})
        await self.settle()
        sender.push(b"\0"+b"\0"*1920)
        await self.settle()
        stats = self.relay.stats
        self.assertEqual(stats["events_received"],3)
        self.assertEqual(stats["events_forwarded"],2)
        self.assertEqual(stats["announcements_received"],2)
        self.assertEqual(stats["announcements"],1)
        self.assertEqual(stats["stops_received"],1)
        self.assertEqual(stats["audio_frames_in"],1)
        self.assertEqual(stats["payload_bytes_out"],sum(len(m.encode("utf-8") if isinstance(m,str) else m) for p in (sender,receiver) for m in p.sent))
        self.assertEqual(stats["messages_out"],len(sender.sent)+len(receiver.sent))
        self.assertIn(command,receiver.sent)
        self.assertGreater(stats["payload_bytes_in"],stats["audio_bytes_in"])

    async def test_unsuccessful_send_not_counted_as_forwarded_event(self):
        from unittest.mock import AsyncMock
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        receiver.push({"type":"enabled","value":True})
        await self.settle()
        receiver.send = AsyncMock(side_effect=TimeoutError)
        sender.push({"type":"say","text":"test"})
        await self.settle()
        self.assertEqual(self.relay.stats["events_received"],1)
        self.assertEqual(self.relay.stats["events_forwarded"],0)
        self.assertEqual(self.relay.stats["announcements"],0)

    async def test_browser_can_listen_without_receiver(self):
        sender = await self.add("sender")
        ticket = self.relay.issue_monitor_ticket()
        monitor = await self.add("monitor", ticket)
        status = json.loads(sender.sent[-1])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["monitors"],1)
        self.assertFalse(status["receiver_enabled"])
        audio = b"\0" + b"\0"*1920
        sender.push(audio)
        await self.settle()
        self.assertIn(audio,monitor.sent)
        self.assertEqual(self.relay.stats["monitor_bytes_out"],1920)
        sender.push({"type":"say","text":"Practice in ten minutes"})
        await self.settle()
        self.assertIn(json.dumps({"type":"say","text":"Practice in ten minutes"}),monitor.sent)
        await monitor.close()
        await self.settle()
        self.assertFalse(json.loads(sender.sent[-1])["enabled"])

    async def test_browser_does_not_replace_receiver(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        receiver.push({"type":"enabled","value":True})
        monitor = await self.add("monitor",self.relay.issue_monitor_ticket())
        audio = b"\1" + b"\0"*1920
        sender.push(audio)
        await self.settle()
        self.assertIn(audio,receiver.sent)
        self.assertIn(audio,monitor.sent)
        self.assertIs(self.relay.peers["receiver"],receiver)

    async def test_monitor_is_read_only(self):
        sender = await self.add("sender")
        monitor = await self.add("monitor",self.relay.issue_monitor_ticket())
        monitor.push({"type":"say","text":"Not allowed"})
        await self.settle()
        self.assertEqual(monitor.closed[0],1008)
        self.assertNotIn(json.dumps({"type":"say","text":"Not allowed"}),sender.sent)

    async def test_monitor_tickets_are_short_lived_single_use(self):
        ticket = self.relay.issue_monitor_ticket()
        first = await self.add("monitor",ticket)
        second = await self.add("monitor",ticket)
        self.assertIsNone(first.closed)
        self.assertEqual(second.closed[0],1008)
        expired = self.relay.issue_monitor_ticket()
        self.relay.monitor_tickets[expired] = 0
        third = await self.add("monitor",expired)
        self.assertEqual(third.closed[0],1008)
        wrong = await self.add("monitor","s"*32)
        self.assertEqual(wrong.closed[0],1008)

    async def test_listener_limit_and_bounded_queue(self):
        await self.add("monitor",self.relay.issue_monitor_ticket())
        await self.add("monitor",self.relay.issue_monitor_ticket())
        third = await self.add("monitor",self.relay.issue_monitor_ticket())
        self.assertEqual(third.closed[0],1008)
        q=asyncio.Queue(maxsize=2)
        self.relay.monitors["fake"]=q
        for i in range(20):
            self.relay.broadcast_monitor(str(i))
        self.assertEqual(q.qsize(),2)
        self.assertEqual(q.get_nowait()[1],"18")
        self.assertEqual(q.get_nowait()[1],"19")
        del self.relay.monitors["fake"]

    async def test_bad_key_and_role_rejected(self):
        p = await self.add("sender", "wrong")
        self.assertEqual(p.closed[0], 1008)
        p = await self.add(["invalid"])
        self.assertEqual(p.closed[0], 1008)
        self.assertEqual(self.relay.peers, {})
    async def test_second_sender_cannot_replace_first(self):
        first = await self.add("sender")
        second = await self.add("sender")
        self.assertEqual(second.closed[0], 1008)
        self.assertIs(self.relay.peers["sender"], first)
    async def test_audio_requires_enable_and_is_never_replayed(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        audio = b"\0" + b"\0"*1920
        sender.push(audio)
        await self.settle()
        self.assertNotIn(audio, receiver.sent)
        receiver.push({"type":"enabled","value":True})
        await self.settle()
        self.assertNotIn(audio, receiver.sent)
        sender.push(audio)
        await self.settle()
        self.assertEqual(receiver.sent.count(audio), 1)
        receiver.push({"type":"enabled","value":False})
        await self.settle()
        sender.push(audio)
        await self.settle()
        self.assertEqual(receiver.sent.count(audio), 1)
    async def test_announcements_and_stop_forwarded(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        receiver.push({"type":"enabled","value":True})
        await self.settle()
        for command in [{"type":"say","text":"Practice in 10 minutes"},{"type":"stop"}]:
            sender.push(command)
            await self.settle()
            self.assertIn(json.dumps(command), receiver.sent)
    async def test_receiver_cannot_send_audio_to_sender(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        audio = b"\0" + b"\0"*1920
        receiver.push(audio)
        await self.settle()
        self.assertNotIn(audio, sender.sent)
    async def test_bad_audio_rejected(self):
        sender = await self.add("sender")
        sender.push(b"\0bad")
        await self.settle()
        self.assertEqual(sender.closed[0], 1008)
    async def test_oversized_speech_rejected(self):
        sender = await self.add("sender")
        sender.push({"type":"say","text":"a"*1001})
        await self.settle()
        self.assertEqual(sender.closed[0], 1008)
    async def test_disconnect_clears_receiver_ready(self):
        await self.add("sender")
        receiver = await self.add("receiver")
        receiver.push({"type":"enabled","value":True})
        await self.settle()
        self.assertTrue(self.relay.enabled)
        await receiver.close()
        await self.settle()
        self.assertFalse(self.relay.enabled)
        self.assertNotIn("receiver", self.relay.peers)

    async def test_client_diagnostics_authenticated_and_not_forwarded(self):
        sender = await self.add("sender")
        receiver = await self.add("receiver")
        receiver.push({"type":"enabled","value":True})
        ticket = self.relay.issue_monitor_ticket()
        monitor = await self.add("monitor",ticket)
        await self.settle()
        before = [len(p.sent) for p in (sender,receiver,monitor)]
        sender.push({"type":"diagnostic","event":"command_sent","id":"abc-123",
                     "characters":60,"version":"0.3.6","text":"Never log this text","key":"secret"})
        receiver.push({"type":"diagnostic","event":"speech_completed","id":"abc-123",
                       "seconds":9.5,"detail":"completed","version":"0.3.6"})
        await self.settle()
        logs = self.relay.snapshot()["client_logs"]
        self.assertEqual(len(logs),2)
        self.assertEqual(logs[0]["role"],"receiver")
        self.assertEqual(logs[0]["event"],"speech_completed")
        self.assertEqual(logs[0]["seconds"],9.5)
        self.assertEqual(logs[1]["characters"],60)
        self.assertNotIn("text",logs[1])
        self.assertNotIn("key",logs[1])
        self.assertEqual(before,[len(p.sent) for p in (sender,receiver,monitor)])
        bad = await self.add("receiver","wrong")
        bad.push({"type":"diagnostic","event":"speech_started"})
        await self.settle()
        self.assertEqual(len(self.relay.client_logs),2)

    async def test_client_diagnostics_sanitize_and_limit(self):
        log = self.relay.client_diagnostic
        self.assertFalse(log("monitor",{"event":"connected"}))
        self.assertFalse(log("receiver",{"event":"arbitrary"}))
        self.assertTrue(log("receiver",{"event":"speech_error","id":"r"*32,
             "characters":True,"seconds":float("inf"),"detail":"arbitrary error with keys",
             "version":"<script>","text":"private","key":"r"*32}))
        item=self.relay.client_logs[0]
        self.assertEqual(set(item),{"time","role","event"})
        for _ in range(100):
            log("receiver",{"event":"speech_started","id":"test"})
        self.assertEqual(len(self.relay.client_logs),30)
        for _ in range(5):
            self.relay.client_log_rates.clear()
            for _ in range(30):
                log("sender",{"event":"speech_started"})
        self.assertEqual(len(self.relay.client_logs),100)
        self.assertTrue(all(e["role"]=="sender" for e in self.relay.client_logs))

if __name__ == "__main__":
    unittest.main()
