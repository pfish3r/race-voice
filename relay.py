"""One sender broadcasts to independent receivers; no recording."""
import asyncio, hmac, json, os, time, threading, secrets, re, math
from collections import deque
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

class Relay:
    def __init__(self, sender_key, receiver_key):
        self.keys = {"sender": sender_key, "receiver": receiver_key}
        self.peers = {}
        self.monitors = {}
        self.monitor_tickets = {}
        self.ticket_lock = threading.Lock()
        self.receivers = {}
        self.started = time.time()
        self.connected_at = {}
        self.connections = {"sender":0,"receiver":0}
        self.stats = {"audio_bytes_in":0,"audio_bytes_out":0,"frames_forwarded":0,"frames_dropped":0,"announcements":0,"auth_rejections":0,"monitor_bytes_out":0,"monitor_frames_dropped":0}
        self.stats.update(payload_bytes_in=0, payload_bytes_out=0, messages_in=0,
                          messages_out=0, audio_frames_in=0, events_received=0,
                          events_forwarded=0, announcements_received=0,
                          stops_received=0, monitor_events_out=0, monitor_events_dropped=0,
                          speech_completed=0, speech_errors=0)
        self.last_audio = None
        self.events = deque(maxlen=30)
        self.client_logs = deque(maxlen=100)
        self.client_log_rates = {}

    @property
    def enabled(self):
        return any(state["enabled"] for state in list(self.receivers.values()))

    async def receiver_writer(self, ws, state):
        """Bounded per-room output: a slow room never blocks sender capture."""
        try:
            while True:
                await state["wake"].wait()
                if state["overflow"]:
                    raise TimeoutError
                if not state["queue"]:
                    state["wake"].clear()
                    continue
                stamp, message = state["queue"].popleft()
                if not state["queue"]:
                    state["wake"].clear()
                if not state["enabled"] or (isinstance(message, bytes) and time.monotonic()-stamp > .25):
                    if isinstance(message, bytes):
                        self.stats["frames_dropped"] += 1
                    continue
                await asyncio.wait_for(ws.send(message), .5)
                self.sent(message)
                if isinstance(message, bytes):
                    self.stats["audio_bytes_out"] += len(message)-1
                    self.stats["frames_forwarded"] += 1
                else:
                    self.stats["events_forwarded"] += 1
                    if json.loads(message)["type"] == "say":
                        self.stats["announcements"] += 1
        except (TimeoutError, ConnectionClosed):
            # Stop admitting output while this connection closes.
            state["enabled"] = False
            try:
                await asyncio.wait_for(ws.close(1013, "Receiver too slow"), 1)
            except (TimeoutError, ConnectionClosed):
                pass

    def broadcast_receivers(self, message):
        targets = list(self.receivers.values())
        if not targets and isinstance(message, bytes):
            self.stats["frames_dropped"] += 1
        for state in targets:
            if not state["enabled"]:
                if isinstance(message, bytes):
                    self.stats["frames_dropped"] += 1
                continue
            q = state["queue"]
            if len(q) >= 32:
                # Evict queued audio first. Never silently discard a speech command.
                old_audio = next((item for item in q if isinstance(item[1], bytes)), None)
                if old_audio is not None:
                    q.remove(old_audio)
                    self.stats["frames_dropped"] += 1
                elif isinstance(message, bytes):
                    self.stats["frames_dropped"] += 1
                    continue
                else:
                    state["overflow"] = True
                    state["enabled"] = False
                    state["wake"].set()
                    continue
            q.append((time.monotonic(), message))
            state["wake"].set()

    def received(self, message):
        self.stats["messages_in"] += 1
        self.stats["payload_bytes_in"] += len(message if isinstance(message, bytes) else message.encode("utf-8"))

    def sent(self, message):
        self.stats["messages_out"] += 1
        self.stats["payload_bytes_out"] += len(message if isinstance(message, bytes) else message.encode("utf-8"))

    def snapshot(self):
        return {"uptime_seconds":round(time.time()-self.started),
                "sender_connected":"sender" in self.peers, "receiver_connected":bool(self.receivers),
                "receiver_count":len(self.receivers),
                "receiver_enabled_count":sum(state["enabled"] for state in list(self.receivers.values())),
                "receiver_enabled":self.enabled, "admin_listeners":len(self.monitors), "connected_at":self.connected_at.copy(),
                "connections":self.connections.copy(), "stats":self.stats.copy(),
                "last_audio":self.last_audio, "events":list(self.events.copy()),
                "client_logs":list(self.client_logs.copy())}

    def event(self, message):
        self.events.appendleft({"time":time.time(),"message":message})

    def client_diagnostic(self, role, data, client_id=None):
        """Store bounded metadata from authenticated clients, never speech or keys."""
        allowed = {"speech_queued", "speech_started", "speech_completed",
                   "speech_cancelled", "speech_duplicate", "speech_error",
                   "speech_rejected", "command_sent", "connected", "disconnected",
                   "speech_output"}
        if role not in ("sender", "receiver") or data.get("event") not in allowed:
            return False
        now = time.monotonic()
        rate_key = client_id or role
        since, count = self.client_log_rates.get(rate_key, (now, 0))
        if now - since >= 10:
            since, count = now, 0
        if count >= 30:
            return False
        self.client_log_rates[rate_key] = (since, count + 1)
        item = {"time":time.time(), "role":role, "event":data["event"]}
        if client_id:
            item["client_id"] = client_id
        private = [*self.keys.values(), os.getenv("ADMIN_PASSWORD", "")]
        def safe(value):
            return not any(secret and secret in value for secret in private)
        ident = data.get("id")
        if isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident) and safe(ident):
            item["id"] = ident
        chars = data.get("characters")
        if type(chars) is int and 0 <= chars <= 1000:
            item["characters"] = chars
        seconds = data.get("seconds")
        if type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 3600:
            item["seconds"] = round(seconds, 2)
        detail = data.get("detail")
        reasons = {"duplicate", "full", "invalid", "completed", "stop_command",
                   "audio_disabled", "manual_disconnect", "shutdown",
                   "device_unavailable", "selected_output", "expired"}
        if isinstance(detail, str) and safe(detail) and (
                detail in reasons or re.fullmatch(r"[A-Za-z_]{1,60}(?:Error|Exception)", detail)):
            item["detail"] = detail
        version = data.get("version")
        if isinstance(version, str) and re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){1,3}", version) and safe(version):
            item["version"] = version
        if role == "receiver" and data["event"] in ("speech_completed", "speech_error"):
            key = "speech_completed" if data["event"] == "speech_completed" else "speech_errors"
            self.stats[key] += 1
        self.client_logs.appendleft(item)
        return True

    def issue_monitor_ticket(self):
        with self.ticket_lock:
            now = time.monotonic()
            self.monitor_tickets = {key:expiry for key,expiry in self.monitor_tickets.items() if expiry > now}
            if len(self.monitor_tickets) >= 16:
                raise ValueError("Too many pending listener requests; wait 30 seconds")
            token = secrets.token_urlsafe(32)
            self.monitor_tickets[token] = now + 30
            return token

    def consume_monitor_ticket(self, token):
        if not isinstance(token, str):
            return False
        with self.ticket_lock:
            return self.monitor_tickets.pop(token, 0) > time.monotonic()

    def broadcast_monitor(self, message):
        for q in list(self.monitors.values()):
            if q.full():
                _, dropped = q.get_nowait()
                key = "monitor_frames_dropped" if isinstance(dropped, bytes) else "monitor_events_dropped"
                self.stats[key] += 1
            q.put_nowait((time.monotonic(), message))

    async def monitor(self, ws, ticket):
        if not self.consume_monitor_ticket(ticket):
            self.stats["auth_rejections"] += 1
            await ws.close(1008, "Invalid or expired listening ticket")
            return
        if len(self.monitors) >= 2:
            await ws.close(1008, "Maximum two admin listeners")
            return
        q = asyncio.Queue(maxsize=10)
        self.monitors[ws] = q
        self.event("Admin listener connected")
        async def write():
            while True:
                stamp, message = await q.get()
                if time.monotonic()-stamp > .25:
                    key = "monitor_frames_dropped" if isinstance(message, bytes) else "monitor_events_dropped"
                    self.stats[key] += 1
                    continue
                await asyncio.wait_for(ws.send(message), .5)
                self.sent(message)
                if isinstance(message, str):
                    self.stats["monitor_events_out"] += 1
                if isinstance(message, bytes):
                    self.stats["monitor_bytes_out"] += len(message)-1
        async def read():
            async for message in ws:
                await ws.close(1008, "Admin listener is read-only")
                return
        tasks = []
        try:
            await asyncio.wait_for(ws.send(json.dumps({"type":"monitor_ready"})), 1)
            await self.status()
            tasks = [asyncio.create_task(write()), asyncio.create_task(read())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if any(isinstance(r, Exception) for r in results):
                await ws.close(1013, "Listener connection interrupted")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.monitors.pop(ws, None)
            self.event("Admin listener disconnected")
            await self.status()

    async def status(self):
        message = json.dumps({"type": "status", "receiver": bool(self.receivers), "receiver_count":len(self.receivers), "receiver_enabled_count":sum(state["enabled"] for state in list(self.receivers.values())), "enabled": self.enabled or bool(self.monitors), "receiver_enabled":self.enabled, "monitors":len(self.monitors)})
        for peer in [*list(self.peers.values()), *list(self.receivers)]:
            try:
                await asyncio.wait_for(peer.send(message), 1)
                self.sent(message)
            except (ConnectionClosed, TimeoutError):
                pass

    async def handler(self, ws):
        role = None
        writer = None
        client_id = None
        try:
            auth = json.loads(await asyncio.wait_for(ws.recv(), 5))
            role = auth.get("role") if isinstance(auth, dict) else None
            if not isinstance(role, str):
                role = None
            if role == "monitor":
                await self.monitor(ws, auth.get("key"))
                return
            if role not in self.keys or not hmac.compare_digest(str(auth.get("key", "")), self.keys[role]):
                self.stats["auth_rejections"] += 1
                await ws.close(1008, "Invalid credentials")
                return
            if role == "sender" and role in self.peers:
                await ws.close(1008, "Role already connected")
                return
            if role == "receiver" and len(self.receivers) >= 32:
                await ws.close(1008, "Maximum 32 receivers")
                return
            if role == "receiver":
                client_id = secrets.token_hex(4)
                state = {"enabled":False, "queue":deque(), "wake":asyncio.Event(),
                         "overflow":False, "id":client_id}
                self.receivers[ws] = state
                writer = asyncio.create_task(self.receiver_writer(ws, state))
                self.connected_at.setdefault(role, time.time())
            else:
                self.peers[role] = ws
                self.connected_at[role] = time.time()
            self.connections[role] += 1
            self.event(role.capitalize()+(" "+client_id if client_id else "")+" connected")
            await self.status()
            async for message in ws:
                self.received(message)
                if isinstance(message, str):
                    data = json.loads(message)
                    if isinstance(data, dict) and data.get("type") == "diagnostic":
                        self.client_diagnostic(role, data, client_id)
                        continue
                if role == "receiver":
                    if isinstance(message, str):
                        data = json.loads(message)
                        if data.get("type") == "enabled":
                            state["enabled"] = data.get("value") is True
                            if not state["enabled"]:
                                self.stats["frames_dropped"] += sum(isinstance(item[1], bytes) for item in state["queue"])
                                state["queue"].clear()
                            self.event("Receiver "+client_id+" audio "+("enabled" if state["enabled"] else "disabled"))
                            await self.status()
                    continue
                if isinstance(message, bytes):
                    if len(message) != 1921 or message[0] not in (0, 1):
                        await ws.close(1008, "Invalid audio")
                        break
                else:
                    data = json.loads(message)
                    if data.get("type") not in ("say", "stop"):
                        continue
                    if data.get("type") == "say" and (not isinstance(data.get("text"), str) or len(data["text"]) > 1000):
                        await ws.close(1008, "Invalid announcement")
                        break
                if isinstance(message, bytes):
                    self.stats["audio_frames_in"] += 1
                    self.stats["audio_bytes_in"] += len(message)-1
                    self.last_audio = time.time()
                if isinstance(message, str):
                    self.stats["events_received"] += 1
                    self.stats["announcements_received" if data["type"] == "say" else "stops_received"] += 1
                self.broadcast_monitor(message)
                self.broadcast_receivers(message)
        except (ConnectionClosed, TimeoutError, ValueError, TypeError, AttributeError):
            pass
        finally:
            if writer:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
            if ws in self.receivers:
                state = self.receivers.pop(ws)
                self.stats["frames_dropped"] += sum(isinstance(item[1], bytes) for item in state["queue"])
                self.client_log_rates.pop(client_id, None)
                if not self.receivers:
                    self.connected_at.pop("receiver", None)
                self.event("Receiver "+client_id+" disconnected")
                await self.status()
            elif self.peers.get(role) is ws:
                del self.peers[role]
                self.connected_at.pop(role, None)
                self.event(role.capitalize()+" disconnected")
                await self.status()

async def main():
    sender, receiver = os.getenv("SENDER_KEY", ""), os.getenv("RECEIVER_KEY", "")
    if min(len(sender), len(receiver)) < 24 or sender == receiver:
        raise SystemExit("Set different SENDER_KEY and RECEIVER_KEY values, at least 24 characters")
    admin_password = os.getenv("ADMIN_PASSWORD","")
    if len(admin_password) < 24 or admin_password in (sender, receiver):
        raise SystemExit("Set a separate ADMIN_PASSWORD with at least 24 characters")
    relay = Relay(sender, receiver)
    from metrics import Metrics
    relay.metrics = Metrics(relay)
    relay.metrics.start()
    from admin import start_admin
    threading.Thread(target=start_admin, args=(relay,), daemon=True).start()
    async with serve(relay.handler, os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8843")),
                     compression=None, max_size=4096, max_queue=4, ping_interval=10, ping_timeout=10):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
