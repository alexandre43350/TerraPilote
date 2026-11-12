import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from clonix import web

from tests.fakes import FakeRunner


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runners = []

        def factory(reporter):
            r = FakeRunner(live_disk="/dev/sdu")
            r.reporter = reporter
            self.runners.append(r)
            return r

        self.app = web.App(self.tmp.name, runner_factory=factory, code="123456")
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, code="123456"):
        req = urllib.request.Request(self.base + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Clonix-Code": code, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def wait_job(self):
        for _ in range(200):
            _, s = self.call("/api/state")
            if s["job"]["state"] != "running":
                return s["job"]
            time.sleep(0.02)
        self.fail("tâche bloquée")

    def test_index_injects_code_for_local_screen(self):
        with urllib.request.urlopen(self.base + "/") as res:
            html = res.read().decode()
        self.assertIn('content="123456"', html)
        with urllib.request.urlopen(self.base + "/app.js") as res:
            self.assertIn("text/javascript", res.headers["Content-Type"])

    def test_static_path_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/../web.py")
        self.assertEqual(ctx.exception.code, 404)

    def test_requires_code(self):
        status, body = self.call("/api/state", code="000000")
        self.assertEqual(status, 401)

    def test_state(self):
        status, s = self.call("/api/state")
        self.assertEqual(status, 200)
        disks = {d["path"]: d for d in s["disks"]}
        self.assertEqual(disks["/dev/sdu"]["protected"], "la clé USB Clonix")
        self.assertFalse(disks["/dev/sdu"]["writable"])
        self.assertFalse(disks["/dev/sde"]["writable"])
        self.assertTrue(disks["/dev/sdc"]["writable"])

    def test_capture_then_deploy(self):
        status, job = self.call("/api/capture", {"disk": "/dev/sda", "name": "poste", "description": "d"})
        self.assertEqual(status, 200, job)
        job = self.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["progress"]["overall"], 1.0)
        _, s = self.call("/api/state")
        self.assertEqual([i["name"] for i in s["images"]], ["poste"])

        status, err = self.call("/api/deploy", {"image": "poste", "disk": "/dev/sdc", "confirm": "sda"})
        self.assertEqual(status, 400)
        self.assertIn("sdc", err["error"])
        status, err = self.call("/api/deploy", {"image": "poste", "disk": "/dev/sdu", "confirm": "sdu"})
        self.assertEqual(status, 400)
        self.assertIn("clé USB", err["error"])
        status, _ = self.call("/api/deploy", {"image": "poste", "disk": "/dev/sdc", "confirm": "sdc"})
        self.assertEqual(status, 200)
        self.assertEqual(self.wait_job()["state"], "done")

    def test_job_error_is_reported(self):
        self.call("/api/deploy", {"image": "absente", "disk": "/dev/sdc", "confirm": "sdc"})
        job = self.wait_job()
        self.assertEqual(job["state"], "error")
        self.assertIn("Aucune image", job["error"])

    def test_job_warnings_are_exposed(self):
        self.app.start("verify", "test", lambda r: r.warn("Désactivez VMD"))
        job = self.wait_job()
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["warnings"], ["Désactivez VMD"])

    def test_bad_image_name(self):
        status, err = self.call("/api/capture", {"disk": "/dev/sda", "name": "../x"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
