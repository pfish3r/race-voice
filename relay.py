"""One sender and receiver, authenticated separately; no recording."""
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
        self.enabled = False
        self.started = time.time()
        self.connected_at = {}
        self.connections = {"sender":0,"receiver":0}
        self.stats = {"audio_bytes_in":0,"audio_bytes_out":0,"frames_forwarded":0,"frames_dropped":0,"announcements":0,"auth_rejections":0,"monitor_bytes_out":0,"monitor_frames_dropped":0}
        self.last_audio = None
        self.events = deque(maxlen=30)
        self.client_logs = deque(maxlen=100)
        self.client_log_rates = {}

    def snapshot(self):
        return {"uptime_seconds":round(time.time()-self.started),
                "sender_connected":"sender" in self.peers, "receiver_connected":"receiver" in self.peers,
                "receiver_enabled":self.enabled, "admin_listeners":len(self.monitors), "connected_at":self.connected_at.copy(),
                "connections":self.connections.copy(), "stats":self.stats.copy(),
                "last_audio":self.last_audio, "events":list(self.events.copy()),
                "client_logs":list(self.client_logs.copy())}

    def event(self, message):
        self.events.appendleft({"time":time.time(),"message":message})

    def client_diagnostic(self, role, data):
        """Store bounded metadata from authenticated clients, never speech or keys."""
        allowed = {"speech_queued", "speech_started", "speech_completed",
                   "speech_cancelled", "speech_duplicate", "speech_error",
                   "speech_rejected", "command_sent", "connected", "disconnected",
                   "speech_output"}
        if role not in ("sender", "receiver") or data.get("event") not in allowed:
            return False
        now = time.monotonic()
        since, count = self.client_log_rates.get(role, (now, 0))
        if now - since >= 10:
            since, count = now, 0
        if count >= 30:
            return False
        self.client_log_rates[role] = (since, count + 1)
        item = {"time":time.time(), "role":role, "event":data["event"]}
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
                q.get_nowait()
                self.stats["monitor_frames_dropped"] += 1
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
                    self.stats["monitor_frames_dropped"] += 1
                    continue
                await asyncio.wait_for(ws.send(message), .5)
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
        message = json.dumps({"type": "status", "receiver": "receiver" in self.peers, "enabled": self.enabled or bool(self.monitors), "receiver_enabled":self.enabled, "monitors":len(self.monitors)})
        for peer in list(self.peers.values()):
            try:
                await asyncio.wait_for(peer.send(message), 1)
            except (ConnectionClosed, TimeoutError):
                pass

    async def handler(self, ws):
        role = None
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
            if role in self.peers:
                await ws.close(1008, "Role already connected")
                return
            self.peers[role] = ws
            self.connected_at[role] = time.time()
            self.connections[role] += 1
            self.event(role.capitalize()+" connected")
            await self.status()
            async for message in ws:
                if isinstance(message, str):
                    data = json.loads(message)
                    if isinstance(data, dict) and data.get("type") == "diagnostic":
                        self.client_diagnostic(role, data)
                        continue
                if role == "receiver":
                    if isinstance(message, str):
                        data = json.loads(message)
                        if data.get("type") == "enabled":
                            self.enabled = data.get("value") is True
                            self.event("Receiver audio "+("enabled" if self.enabled else "disabled"))
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
                    self.stats["audio_bytes_in"] += len(message)-1
                    self.last_audio = time.time()
                self.broadcast_monitor(message)
                receiver = self.peers.get("receiver")
                if receiver and self.enabled:
                    try:
                        await asyncio.wait_for(receiver.send(message), .5)
                        if isinstance(message, bytes):
                            self.stats["audio_bytes_out"] += len(message)-1
                            self.stats["frames_forwarded"] += 1
                        elif data.get("type") == "say":
                            self.stats["announcements"] += 1
                    except (TimeoutError, ConnectionClosed):
                        if isinstance(message, bytes):
                            self.stats["frames_dropped"] += 1
                        await receiver.close(1013, "Receiver too slow")
                elif isinstance(message, bytes):
                    self.stats["frames_dropped"] += 1
        except (ConnectionClosed, TimeoutError, ValueError, TypeError, AttributeError):
            pass
        finally:
            if self.peers.get(role) is ws:
                del self.peers[role]
                self.connected_at.pop(role, None)
                self.event(role.capitalize()+" disconnected")
                if role == "receiver":
                    self.enabled = False
                await self.status()

async def main():
    sender, receiver = os.getenv("SENDER_KEY", ""), os.getenv("RECEIVER_KEY", "")
    if min(len(sender), len(receiver)) < 24 or sender == receiver:
        raise SystemExit("Set different SENDER_KEY and RECEIVER_KEY values, at least 24 characters")
    admin_password = os.getenv("ADMIN_PASSWORD","")
    if len(admin_password) < 24 or admin_password in (sender, receiver):
        raise SystemExit("Set a separate ADMIN_PASSWORD with at least 24 characters")
    relay = Relay(sender, receiver)
    from admin import start_admin
    threading.Thread(target=start_admin, args=(relay,), daemon=True).start()
    async with serve(relay.handler, os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8843")),
                     compression=None, max_size=4096, max_queue=4, ping_interval=10, ping_timeout=10):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
