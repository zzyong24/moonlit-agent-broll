"""muse.py 本地假服务测试：只绑定 127.0.0.1，不连真实 Muse。

其中 KEY 仅为测试夹具（fixture），不是真实密钥，只用于对照本文件里的假服务。
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import make_test_mp4, make_test_png  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "muse.py"
KEY = "museTestKey123456789"  # TEST FIXTURE only; not a real API key


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.submits, self.payloads, self.auth, self.media_auth = 0, [], [], []
        self.quota_pct, self.final_status, self.result_url = 10, "completed", "/v1/media/r.mp4"
        self.polls = 0


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, value, ctype="application/json"):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        STATE.auth.append(self.headers.get("Authorization"))
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self._send(401, {"detail": "bad key"})
        if self.path.startswith("/admin/accounts/") and self.path.endswith("/quota"):
            return self._send(200, {"found": True, "plan": "Free plan", "weekly_used_pct": STATE.quota_pct, "weekly_reset": "Nov 10"})
        if self.path == "/v1/videos":
            STATE.submits += 1
            STATE.payloads.append(data)
            return self._send(200, {"id": "task_abc", "task_id": "task_abc", "status": "queued"})
        self._send(404, {})

    def do_GET(self):
        if self.path.startswith("/v1/media/"):
            STATE.media_auth.append(self.headers.get("Authorization"))
            return self._send(200, make_test_mp4("4.0", audio=True), "video/mp4")
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self._send(401, {"detail": "bad key"})
        if self.path == "/v1/videos/task_abc":
            STATE.polls += 1
            if STATE.polls < 2:
                return self._send(200, {"id": "task_abc", "status": "processing"})
            if STATE.final_status == "failed":
                return self._send(200, {"id": "task_abc", "status": "failed", "error": "moderation"})
            return self._send(200, {"id": "task_abc", "status": "completed", "url": STATE.result_url})
        self._send(404, {})


class MuseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d/v1" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        STATE.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        self.env_file = self.project / "fake.env"
        self.env_file.write_text("OTHER=1\nMUSE2API_KEY=%s\n" % KEY)
        (self.project / "f.png").write_bytes(make_test_png())
        self.request = self.project / "BR01.json"
        self.request.write_text(json.dumps({"id": "BR01", "anchor": "原句", "model": "muse-video", "mode": "first_frame",
                                            "prompt": "积木人转身", "duration": 4,
                                            "images": [{"path": "f.png", "role": "first_frame"}]}, ensure_ascii=False))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "MUSE2API_KEY"}
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--project", str(self.project),
                                 "--env-file", str(self.env_file), "--test-server", self.base, "--timeout", "5", *args],
                                capture_output=True, text=True, env=env)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        return result

    def quote(self, confirm=True):
        out = self.cli("estimate", "--request", str(self.request))
        self.assertEqual(out.returncode, 0, out.stderr)
        quote_id = json.loads(out.stdout)["quote_id"]
        if confirm:
            self.assertEqual(self.cli("confirm", "--quote", quote_id, "--user-confirmation", "测试确认").returncode, 0)
        return quote_id

    def records_have_no_key(self):
        for path in (self.project / ".agent-broll").rglob("*.json"):
            self.assertNotIn(KEY, path.read_text())

    def test_unconfirmed_refuses_submission(self):
        quote_id = self.quote(confirm=False)
        out = self.cli("submit", "--quote", quote_id, "--job", "BR01")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(STATE.submits, 0)

    def test_lifecycle_quota_payload_wait_download(self):
        quote_id = self.quote()
        out = self.cli("submit", "--quote", quote_id, "--job", "BR01")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("提交前额度", out.stdout)
        payload = STATE.payloads[0]
        self.assertEqual(payload["aspect_ratio"], "16:9")  # 省略 ratio 时默认横屏
        self.assertEqual(payload["duration"], 4)
        self.assertTrue(payload["image"].startswith("data:image/png;base64,"))
        self.assertNotIn("content", payload)
        attempt = quote_id + "--BR01"
        out = self.cli("wait", "--attempt", attempt, "--interval", "0.1")
        self.assertEqual(out.returncode, 0, out.stderr)
        value = json.loads(out.stdout)
        self.assertEqual(value["state"], "downloaded")
        self.assertTrue((self.project / value["local_file"]).is_file())
        self.assertEqual(STATE.media_auth, [None])
        self.assertEqual(self.cli("quota-after", "--attempt", attempt).returncode, 0)
        self.assertEqual(self.cli("submit", "--quote", quote_id, "--job", "BR01").returncode, 2)
        self.assertEqual(STATE.submits, 1)
        self.records_have_no_key()

    def test_quota_over_limit_refuses_without_submitting(self):
        STATE.quota_pct = 96
        quote_id = self.quote()
        out = self.cli("submit", "--quote", quote_id, "--job", "BR01")
        self.assertEqual(out.returncode, 2)
        self.assertIn("拒绝提交", out.stderr)
        self.assertEqual(STATE.submits, 0)

    def test_failed_task_is_failed(self):
        STATE.final_status = "failed"
        quote_id = self.quote()
        self.cli("submit", "--quote", quote_id, "--job", "BR01")
        value = json.loads(self.cli("wait", "--attempt", quote_id + "--BR01", "--interval", "0.1").stdout)
        self.assertEqual(value["state"], "failed")

    def test_foreign_result_url_is_not_downloaded(self):
        STATE.result_url = "https://evil.example/r.mp4"
        quote_id = self.quote()
        self.cli("submit", "--quote", quote_id, "--job", "BR01")
        value = json.loads(self.cli("wait", "--attempt", quote_id + "--BR01", "--interval", "0.1", "--max-wait", "1").stdout)
        self.assertNotEqual(value["state"], "downloaded")
        self.assertEqual(STATE.media_auth, [])

    def test_ratio_and_duration_validation(self):
        data = json.loads(self.request.read_text())
        data["duration"] = 3
        self.request.write_text(json.dumps(data))
        self.assertEqual(self.cli("estimate", "--request", str(self.request)).returncode, 2)
        data["duration"], data["ratio"] = 4, "adaptive"
        self.request.write_text(json.dumps(data))
        self.assertEqual(self.cli("estimate", "--request", str(self.request)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
