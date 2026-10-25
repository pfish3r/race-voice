"""Supervise Caddy and expose two private, token-authenticated certificate operations."""
import hashlib, hmac, json, os, signal, socket, ssl, subprocess, tempfile, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from metrics import ContainerUsage

container_usage = ContainerUsage()

STATE = Path("/data/racevoice-renew.json")
lock = threading.RLock()
child = None
job = {"state":"idle", "message":"Automatic renewal managed by Caddy", "last_attempt":0}
cache = (0, {})
def save():
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(job))
    os.chmod(temp, 0o600)
    temp.replace(STATE)

def certificate():
    domain = os.environ["RELAY_DOMAIN"]
    trusted = True
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(("127.0.0.1",443), timeout=4) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                der = tls.getpeercert(binary_form=True)
                info = tls.getpeercert()
    except ssl.SSLCertVerificationError:
        trusted = False
        ctx = ssl._create_unverified_context()
        with socket.create_connection(("127.0.0.1",443), timeout=4) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                der = tls.getpeercert(binary_form=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
            name = f.name
        try:
            info = ssl._ssl._test_decode_cert(name)
        finally:
            Path(name).unlink(missing_ok=True)
    expires = ssl.cert_time_to_seconds(info["notAfter"])
    return {"available":True, "trusted":trusted, "expires_at":expires,
            "issued_at":ssl.cert_time_to_seconds(info["notBefore"]),
            "days_remaining":round((expires-time.time())/86400, 1),
            "issuer":", ".join(v for group in info["issuer"] for k,v in group if k in ("organizationName","commonName")),
            "names":[v for k,v in info.get("subjectAltName",[]) if k=="DNS"],
            "fingerprint":hashlib.sha256(der).hexdigest(),
            "checked_at":time.time()}

def status():
    global cache
    if time.time() - cache[0] > 15:
        try:
            data = certificate()
        except Exception:
            data = {"available":False, "message":"TLS certificate could not be read from Caddy", "checked_at":time.time()}
        cache = (time.time(), data)
    with lock:
        return {"tls":cache[1], "renewal":dict(job), "automatic_renewal":True,
                "caddy_running":child is not None and child.poll() is None,
                "next_manual_attempt":job.get("last_attempt",0)+86400}

def spawn():
    return subprocess.Popen(["caddy","run","--config","/etc/caddy/Caddyfile","--adapter","caddyfile"])

def renew():
    global child, cache
    try:
        before = certificate()["fingerprint"]
        result = subprocess.run(["/usr/bin/racevoice-renew"], timeout=900, stdout=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError("Renewal failed. Existing certificate retained. Check GoDaddy permissions and Caddy logs.")
        with lock:
            job.update(state="loading", message="Certificate issued; restarting Caddy. Clients will reconnect.")
            save()
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child = spawn()
        for _ in range(30):
            time.sleep(1)
            try:
                current = certificate()
                if current["trusted"] and current["fingerprint"] != before:
                    with lock:
                        job.update(state="succeeded", message="Renewed certificate verified on the TLS listener", completed_at=time.time())
                        save()
                    cache = (0,{})
                    return
            except Exception:
                pass
        raise RuntimeError("Issuance completed, but the new certificate could not be verified on Caddy. Inspect service logs.")
    except Exception as e:
        with lock:
            job.update(state="failed", message=str(e) if isinstance(e, RuntimeError) else "Renewal did not complete; inspect service logs", completed_at=time.time())
            save()
        cache = (0,{})

def request_renewal():
    with lock:
        if job["state"] in ("running","loading"):
            return 409, {"message":"Renewal already running"}
        if time.time()-job.get("last_attempt",0) < 86400:
            return 429, {"message":"Manual renewal is limited to one attempt per 24 hours. Automatic renewal continues."}
        job.update(state="running", message="Requesting Let's Encrypt renewal through GoDaddy DNS", last_attempt=time.time())
        save()
        threading.Thread(target=renew, daemon=True).start()
        return 202, {"message":"Renewal started. The dashboard will update automatically."}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def respond(self, code, data):
        body=json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def auth(self):
        expected="Bearer "+os.environ["ADMIN_PASSWORD"]
        if not hmac.compare_digest(self.headers.get("Authorization","").encode(),expected.encode()):
            self.respond(401, {"error":"Unauthorized"})
            return False
        return True
    def do_GET(self):
        if self.auth():
            if self.path == "/metrics":
                self.respond(200, container_usage.sample())
            elif self.path == "/status":
                self.respond(200, status())
            else:
                self.respond(404, {})
    def do_POST(self):
        if self.auth():
            code,data=request_renewal() if self.path=="/renew" else (404,{})
            self.respond(code,data)

def main():
    global child,job
    if len(os.getenv("ADMIN_PASSWORD",""))<24:
        raise SystemExit("ADMIN_PASSWORD must contain at least 24 characters")
    if STATE.exists():
        job.update(json.loads(STATE.read_text()))
        if job["state"] in ("running","loading"):
            job.update(state="interrupted", message="Previous renewal was interrupted. Check certificate status.")
    child=spawn()
    def shutdown(*args):
        with lock:
            child.terminate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    server=ThreadingHTTPServer(("0.0.0.0",8091),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    while True:
        time.sleep(1)
        with lock:
            if child.poll() is not None:
                raise SystemExit(child.returncode or 1)

if __name__=="__main__":
    main()
