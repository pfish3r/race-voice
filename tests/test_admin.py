import base64, importlib.util, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin import authorized, valid_renew_request, make_handler
spec=importlib.util.spec_from_file_location("controller",Path(__file__).resolve().parents[1]/"server"/"controller.py")
controller=importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)

class AdminTests(unittest.TestCase):
    def test_keys_require_admin_and_only_return_requested_role(self):
        relay=Mock(keys={"sender":"sender-test-key","receiver":"receiver-test-key"})
        cls=make_handler(relay,"admin-test-password","https://audio.example:8843")
        handler=object.__new__(cls)
        handler.reply=Mock()
        for role in ("sender","receiver"):
            handler.path="/admin/api/keys/"+role
            for credentials in ("", "Basic "+base64.b64encode(b"sender:sender-test-key").decode()):
                handler.headers={"Authorization":credentials}
                handler.do_GET()
                self.assertEqual(handler.reply.call_args.args[0],401)
                self.assertNotIn("key",handler.reply.call_args.args[1])
            handler.headers={"Authorization":"Basic "+base64.b64encode(b"admin:admin-test-password").decode()}
            handler.do_GET()
            handler.reply.assert_called_with(200,{"role":role,"key":relay.keys[role]})
        handler.path="/admin/api/keys/admin"
        handler.do_GET()
        handler.reply.assert_called_with(404,{"error":"Not found"})

    def test_monitor_ticket_endpoint_requires_admin(self):
        relay=Mock()
        relay.issue_monitor_ticket.return_value="one-use-ticket"
        cls=make_handler(relay,"password","https://example")
        handler=object.__new__(cls)
        handler.path="/admin/api/monitor-ticket"
        handler.headers={}
        handler.reply=Mock()
        handler.do_GET()
        relay.issue_monitor_ticket.assert_not_called()
        handler.reply.assert_called_with(401,{"error":"Administrator login required"})
        handler.headers={"Authorization":"Basic "+base64.b64encode(b"admin:password").decode()}
        handler.do_GET()
        handler.reply.assert_called_with(200,{"ticket":"one-use-ticket","expires_in":30})

    def test_sensitive_responses_are_not_cached(self):
        cls=make_handler(Mock(),"password","https://example")
        handler=object.__new__(cls)
        handler.send_response=Mock()
        handler.send_header=Mock()
        handler.end_headers=Mock()
        handler.wfile=Mock()
        handler.reply(200,{"key":"test-value"})
        handler.send_header.assert_any_call("Cache-Control","no-store")


    def test_only_correct_admin_credentials(self):
        def header(value):
            return "Basic "+base64.b64encode(value.encode()).decode()
        self.assertTrue(authorized(header("admin:secret"),"secret"))
        for value in ["sender:secret","admin:wrong",":secret"]:
            self.assertFalse(authorized(header(value),"secret"))
        for value in ["","Bearer x","Basic !!!","Basic /w=="]:
            self.assertFalse(authorized(value,"secret"))
    def test_renewal_requires_same_origin_and_explicit_header(self):
        expected="https://audio.example.com:8843"
        self.assertTrue(valid_renew_request(expected,"renew",expected))
        self.assertFalse(valid_renew_request("https://evil.example","renew",expected))
        self.assertFalse(valid_renew_request(expected,None,expected))
        self.assertFalse(valid_renew_request(None,"renew",expected))

    def test_cooldown_blocks_repeated_attempt(self):
        with patch.object(controller,"job",{"state":"succeeded","last_attempt":time.time()}):
            self.assertEqual(controller.request_renewal()[0],429)
    def test_running_job_not_duplicated(self):
        with patch.object(controller,"job",{"state":"running","last_attempt":0}):
            self.assertEqual(controller.request_renewal()[0],409)
    def test_request_persisted_before_worker(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            with patch.object(controller,"STATE",Path(folder)/"state.json"),patch.object(controller,"job",{"state":"idle","last_attempt":0}),patch.object(controller.threading,"Thread") as thread:
                self.assertEqual(controller.request_renewal()[0],202)
                self.assertIn('"state": "running"',controller.STATE.read_text())
                thread.return_value.start.assert_called_once()
    def test_failed_renewal_never_restarts_caddy(self):
        process=Mock()
        with patch.object(controller,"child",process),patch.object(controller,"save"),patch.object(controller,"certificate",return_value={"fingerprint":"old"}),patch.object(controller.subprocess,"run",return_value=Mock(returncode=1)),patch.object(controller,"job",{"state":"running","last_attempt":0}):
            controller.renew()
            process.terminate.assert_not_called()
            self.assertEqual(controller.job["state"],"failed")
    def test_success_requires_verified_changed_certificate(self):
        process=Mock()
        certs=[{"fingerprint":"old"},{"fingerprint":"new","trusted":True}]
        with patch.object(controller,"child",process),patch.object(controller,"save"),patch.object(controller,"certificate",side_effect=certs),patch.object(controller.subprocess,"run",return_value=Mock(returncode=0)),patch.object(controller,"spawn",return_value=Mock()),patch.object(controller.time,"sleep"),patch.object(controller,"job",{"state":"running","last_attempt":0}):
            controller.renew()
            process.terminate.assert_called_once()
            self.assertEqual(controller.job["state"],"succeeded")
    def test_stale_certificate_is_not_success(self):
        with patch.object(controller,"child",Mock()),patch.object(controller,"save"),patch.object(controller,"certificate",return_value={"fingerprint":"old","trusted":True}),patch.object(controller.subprocess,"run",return_value=Mock(returncode=0)),patch.object(controller,"spawn",return_value=Mock()),patch.object(controller.time,"sleep"),patch.object(controller,"job",{"state":"running","last_attempt":0}):
            controller.renew()
            self.assertEqual(controller.job["state"],"failed")
