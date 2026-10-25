"""Bounded, in-memory relay history and read-only Linux container accounting."""
import json
import os
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path


class ContainerUsage:
    """CPU is percent of one core. Charged memory includes file cache.
    Missing accounting is unavailable, never silently replaced by host usage.
    """
    def __init__(self, root="/sys/fs/cgroup", membership="/proc/self/cgroup", clock=time.monotonic):
        self.root = Path(root)
        self.membership = Path(membership)
        self.clock = clock
        self.previous = None
        self.lock = threading.Lock()

    def groups(self):
        groups = {}
        try:
            for line in self.membership.read_text().splitlines():
                _, controllers, path = line.split(":", 2)
                if ".." in Path(path).parts:
                    continue
                for controller in controllers.split(","):
                    groups[controller] = path.lstrip("/")
        except (OSError, ValueError):
            pass
        return groups

    def read(self, filename, controller=""):
        groups = self.groups()
        if controller not in groups:
            raise OSError("Container membership unavailable")
        relative = groups[controller]
        bases = [self.root] if not controller else [self.root / controller]
        if controller == "cpuacct":
            bases += [self.root / "cpu,cpuacct", self.root / "cpuacct,cpu"]
        for base in bases:
            for directory in (base / relative,):
                try:
                    return (directory / filename).read_text().strip()
                except OSError:
                    pass
        raise OSError("Container accounting unavailable")

    def sample(self):
        with self.lock:
            now = self.clock()
            cpu = memory = limit = None
            version = None
            limit_known = False
            try:
                stat = dict(line.split() for line in self.read("cpu.stat").splitlines())
                cpu = int(stat["usage_usec"]) / 1_000_000
                version = 2
            except (OSError, ValueError, KeyError):
                try:
                    cpu = int(self.read("cpuacct.usage", "cpuacct")) / 1_000_000_000
                    version = 1
                except (OSError, ValueError):
                    pass
            try:
                if version == 2:
                    memory = int(self.read("memory.current"))
                    raw = self.read("memory.max")
                    limit = None if raw == "max" else int(raw)
                    limit_known = True
                else:
                    memory = int(self.read("memory.usage_in_bytes", "memory"))
                    raw = int(self.read("memory.limit_in_bytes", "memory"))
                    limit = raw if raw < (1 << 60) else None
                    limit_known = True
            except (OSError, ValueError):
                pass
            percent = None
            if cpu is not None:
                if self.previous is not None:
                    stamp, usage = self.previous
                    if now > stamp and cpu >= usage:
                        percent = round((cpu - usage) / (now - stamp) * 100, 2)
                self.previous = (now, cpu)
            else:
                self.previous = None
            return {"available": cpu is not None or memory is not None,
                    "cpu_percent": percent, "memory_bytes": memory,
                    "memory_limit_bytes": limit, "memory_limit_known": limit_known,
                    "cgroup_version": version}


def caddy_usage():
    req = urllib.request.Request("http://caddy:8091/metrics", headers={
        "Authorization": "Bearer " + os.environ.get("ADMIN_PASSWORD", "")})
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.load(response)
        if not isinstance(data, dict) or not isinstance(data.get("available"), bool):
            raise ValueError("Invalid accounting response")
        return data
    except (OSError, ValueError):
        return {"available": False, "cpu_percent": None, "memory_bytes": None,
                "memory_limit_bytes": None}


class Metrics:
    INTERVAL = 5
    CAPACITY = 721

    def __init__(self, relay, local=None, remote=caddy_usage, clock=time.monotonic, wall=time.time):
        self.relay = relay
        self.local = local or ContainerUsage()
        self.remote = remote
        self.clock, self.wall = clock, wall
        self.samples = deque(maxlen=self.CAPACITY)
        self.previous = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def collect(self):
        now = self.clock()
        counters = self.relay.stats.copy()
        sample = {"time": self.wall(), "counters": counters,
                  "rates": {}, "containers": {}}
        if self.previous:
            stamp, old = self.previous
            elapsed = now - stamp
            if elapsed > 0:
                for key, value in counters.items():
                    sample["rates"][key] = max(0, value - old.get(key, value)) / elapsed
        self.previous = (now, counters)
        sample["containers"]["relay"] = self.local.sample()
        sample["containers"]["caddy"] = self.remote()
        with self.lock:
            self.samples.append(sample)
            while self.samples and self.samples[0]["time"] < sample["time"] - 3600:
                self.samples.popleft()

    def snapshot(self, after=None):
        with self.lock:
            samples = list(self.samples)
            reset = bool(samples and after is not None and after > samples[-1]["time"])
            if after is not None and not reset:
                samples = [sample for sample in samples if sample["time"] > after]
            return {"started_at": self.relay.started, "interval_seconds": self.INTERVAL,
                    "retention_seconds": 3600, "reset": reset, "samples": samples}

    def run(self):
        while not self.stopped.is_set():
            start = self.clock()
            try:
                self.collect()
            except Exception:
                pass  # Monitoring must never interrupt audio or kill its sampler.
            self.stopped.wait(max(.1, self.INTERVAL - (self.clock() - start)))

    def start(self):
        thread = threading.Thread(target=self.run, name="relay-metrics", daemon=True)
        thread.start()
        return thread
