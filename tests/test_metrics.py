"""Container accounting and retained telemetry with fake Linux cgroups."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metrics import ContainerUsage, Metrics, caddy_usage
from admin import make_handler


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.member = self.root / "membership"
        self.now = 10
        self.usage = ContainerUsage(self.root, self.member, lambda: self.now)

    def write(self, file, value):
        path = self.root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def test_v2_cpu_delta_memory_and_unlimited(self):
        self.member.write_text("0::/\n")
        self.write("cpu.stat", "usage_usec 1000000\nuser_usec 100000\n")
        self.write("memory.current", "10485760")
        self.write("memory.max", "max")
        first = self.usage.sample()
        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(first["memory_bytes"], 10485760)
        self.assertIsNone(first["memory_limit_bytes"])
        self.now += 5
        self.write("cpu.stat", "usage_usec 3500000")
        self.write("memory.max", "67108864")
        second = self.usage.sample()
        self.assertEqual(second["cpu_percent"], 50)
        self.assertEqual(second["memory_limit_bytes"], 67108864)

    def test_legacy_combined_mount_and_membership(self):
        self.member.write_text("2:cpu,cpuacct:/docker/example\n3:memory:/docker/example\n")
        self.write("cpu,cpuacct/docker/example/cpuacct.usage", "1000000000")
        self.write("memory/docker/example/memory.usage_in_bytes", "12345")
        self.write("memory/docker/example/memory.limit_in_bytes", str(1 << 62))
        first = self.usage.sample()
        self.assertTrue(first["available"])
        self.assertEqual(first["cgroup_version"], 1)
        self.assertEqual(first["memory_bytes"], 12345)
        self.assertIsNone(first["memory_limit_bytes"])
        self.now += 2
        self.write("cpu,cpuacct/docker/example/cpuacct.usage", "5000000000")
        self.assertEqual(self.usage.sample()["cpu_percent"], 200)

    def test_missing_namespace_does_not_fall_back_to_host_root(self):
        self.member.write_text("0::/inaccessible/container\n")
        self.write("cpu.stat", "usage_usec 9000000")
        self.write("memory.current", "99999999")
        self.write("memory.max", "max")
        result = self.usage.sample()
        self.assertFalse(result["available"])
        self.assertIsNone(result["cpu_percent"])
        self.assertIsNone(result["memory_bytes"])

    def test_counter_reset_is_gap_not_negative_cpu(self):
        self.member.write_text("0::/\n")
        self.write("cpu.stat", "usage_usec 9000000")
        self.usage.sample()
        self.now += 5
        self.write("cpu.stat", "usage_usec 1000000")
        self.assertIsNone(self.usage.sample()["cpu_percent"])

    def test_no_accounting_on_non_linux(self):
        self.assertFalse(self.usage.sample()["available"])

    def test_upstream_failure_returns_unavailable(self):
        with patch("metrics.urllib.request.urlopen", side_effect=TimeoutError):
            self.assertFalse(caddy_usage()["available"])


class HistoryTests(unittest.TestCase):
    def test_rates_real_elapsed_fanout_and_bounded_history(self):
        now = [0]
        relay = SimpleNamespace(started=100, stats={"payload_bytes_in":0, "payload_bytes_out":0, "events_received":0})
        metrics = Metrics(relay, local=Mock(sample=Mock(return_value={"available":True})),
                          remote=lambda: {"available":False}, clock=lambda:now[0], wall=lambda:100+now[0])
        metrics.collect()
        self.assertEqual(metrics.snapshot()["samples"][0]["rates"], {})
        relay.stats.update(payload_bytes_in=1000, payload_bytes_out=3000, events_received=2)
        now[0] = 10
        metrics.collect()
        sample = metrics.snapshot()["samples"][-1]
        self.assertEqual(sample["rates"]["payload_bytes_in"], 100)
        self.assertEqual(sample["rates"]["payload_bytes_out"], 300)
        self.assertEqual(sample["rates"]["events_received"], .2)
        relay.stats["payload_bytes_in"] = 0
        now[0] += 5
        metrics.collect()
        self.assertEqual(metrics.snapshot()["samples"][-1]["rates"]["payload_bytes_in"], 0)
        for _ in range(800):
            now[0] += 5
            metrics.collect()
        result = metrics.snapshot()
        self.assertEqual(len(result["samples"]), 721)
        self.assertEqual(result["samples"][-1]["time"]-result["samples"][0]["time"], 3600)
        self.assertEqual(result["samples"][0]["counters"]["payload_bytes_out"], 3000)
        self.assertNotIn("keys", json.dumps(result))
        last = result["samples"][-1]["time"]
        self.assertEqual(metrics.snapshot(after=last)["samples"], [])
        self.assertEqual(len(metrics.snapshot(after=last-5)["samples"]), 1)
        self.assertTrue(metrics.snapshot(after=last+100)["reset"])
        self.assertEqual(len(metrics.snapshot(after=last+100)["samples"]),721)

    def test_history_endpoint_requires_admin(self):
        relay = SimpleNamespace(metrics=Mock())
        relay.metrics.snapshot.return_value = {"samples":[]}
        handler = object.__new__(make_handler(relay, "password", "https://example"))
        handler.path = "/admin/api/metrics"
        handler.reply = Mock()
        handler.headers = {}
        handler.do_GET()
        relay.metrics.snapshot.assert_not_called()
        handler.reply.assert_called_with(401, {"error":"Administrator login required"})
        handler.headers = {"Authorization":"Basic "+base64.b64encode(b"admin:password").decode()}
        handler.do_GET()
        handler.reply.assert_called_with(200, {"samples":[]})

    def test_incremental_query_rejects_bad_timestamps(self):
        relay = SimpleNamespace(metrics=Mock())
        relay.metrics.snapshot.return_value = {"samples":[]}
        handler = object.__new__(make_handler(relay, "password", "https://example"))
        handler.reply = Mock()
        handler.headers = {"Authorization":"Basic "+base64.b64encode(b"admin:password").decode()}
        for value in ("NaN","inf","-1","nope",""):
            handler.path = "/admin/api/metrics?after="+value
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0],400)
        relay.metrics.snapshot.assert_not_called()
        handler.path = "/admin/api/metrics?after=123.5"
        handler.do_GET()
        relay.metrics.snapshot.assert_called_once_with(after=123.5)

    def test_caddy_metrics_are_authenticated_and_do_not_probe_certificate(self):
        from test_admin import controller
        handler = object.__new__(controller.Handler)
        handler.path = "/metrics"
        handler.headers = {}
        handler.respond = Mock()
        with patch.dict("os.environ", {"ADMIN_PASSWORD":"test-password"}), patch.object(controller,"container_usage") as usage, patch.object(controller,"certificate") as cert:
            handler.do_GET()
            usage.sample.assert_not_called()
            handler.respond.assert_called_with(401, {"error":"Unauthorized"})
            handler.headers = {"Authorization":"Bearer test-password"}
            usage.sample.return_value = {"available":True}
            handler.do_GET()
            handler.respond.assert_called_with(200, {"available":True})
            cert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
