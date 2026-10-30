"""Private dashboard; client keys are available only via authenticated on-demand requests."""
import base64, hmac, json, os, time, math, urllib.request, urllib.error
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def authorized(header, password):
    try:
        raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode()
        return header.startswith("Basic ") and hmac.compare_digest(raw.encode(), ("admin:" + password).encode())
    except (ValueError, IndexError, UnicodeError):
        return False

def valid_renew_request(origin, action, expected):
    return origin == expected and action == "renew"

def controller(method="GET"):
    req = urllib.request.Request("http://caddy:8091/" + ("renew" if method == "POST" else "status"),
        data=b"" if method == "POST" else None, method=method,
        headers={"Authorization": "Bearer " + os.environ["ADMIN_PASSWORD"]})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)
    except Exception:
        return 503, {"state":"unavailable", "message":"Certificate controller unreachable"}

def make_handler(relay, password, origin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, status, data, kind="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self' " + origin.replace("https://", "wss://") + "; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="Race Voice Admin", charset="UTF-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def auth(self):
            if not authorized(self.headers.get("Authorization", ""), password):
                self.reply(401, {"error":"Administrator login required"})
                return False
            return True
        def do_GET(self):
            if not self.auth():
                return
            path = self.path.split("?")[0]
            assets = {"/admin":"index.html", "/admin/":"index.html", "/admin/app.js":"app.js", "/admin/style.css":"style.css", "/admin/monitor.js":"monitor.js", "/admin/metrics.js":"metrics.js"}
            if path in assets:
                file = Path(__file__).parent / "dashboard" / assets[path]
                kind = {"html":"text/html; charset=utf-8", "js":"application/javascript", "css":"text/css"}[file.suffix[1:]]
                return self.reply(200, file.read_bytes(), kind)
            if path in ("/admin/api/keys/sender", "/admin/api/keys/receiver"):
                role = path.rsplit("/", 1)[1]
                return self.reply(200, {"role":role, "key":relay.keys[role]})
            if path == "/admin/api/monitor-ticket":
                try:
                    return self.reply(200, {"ticket":relay.issue_monitor_ticket(), "expires_in":30})
                except ValueError as e:
                    return self.reply(429, {"error":str(e)})
            if path == "/admin/api/metrics":
                query = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
                if "after" not in query:
                    return self.reply(200, relay.metrics.snapshot())
                try:
                    after = float(query["after"][0])
                    if not math.isfinite(after) or after < 0:
                        raise ValueError()
                except ValueError:
                    return self.reply(400, {"error":"Invalid metrics timestamp"})
                return self.reply(200, relay.metrics.snapshot(after=after))
            if path == "/admin/api/status":
                code, cert = controller()
                return self.reply(200, {"relay":relay.snapshot(), "certificate":cert,
                    "hostname":os.environ["RELAY_DOMAIN"], "port":int(os.getenv("PORT","8843")),
                    "version":"0.3.8", "controller_available":code == 200})
            self.reply(404, {"error":"Not found"})
        def do_POST(self):
            if not self.auth():
                return
            if self.path != "/admin/api/renew":
                return self.reply(404, {"error":"Not found"})
            if not valid_renew_request(self.headers.get("Origin"), self.headers.get("X-RaceVoice-Action"), origin):
                return self.reply(403, {"error":"Invalid request origin"})
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length","0") not in ("0",""):
                return self.reply(400, {"error":"Request must have an empty body"})
            code, result = controller("POST")
            self.reply(code, result)
    return Handler

def start_admin(relay):
    password = os.environ["ADMIN_PASSWORD"]
    port = os.getenv("PORT", "8843")
    origin = "https://" + os.environ["RELAY_DOMAIN"] + (":" + port if port != "443" else "")
    server = ThreadingHTTPServer(("0.0.0.0", 8090), make_handler(relay, password, origin))
    server.daemon_threads = True
    server.serve_forever()
