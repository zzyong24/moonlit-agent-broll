"""标准库行为测试：只调用 127.0.0.1 假服务，不读取真实 Key。

命令：python3 -B -m unittest discover -s skill/tests -p 'test_h3.py' -v
运行会在系统临时目录创建隔离项目，结束后自动清理；不写 skill 运行数据。
假 MP4 仅验证容器时长和视频轨，不是真正可播放的视频；本测试不证明解码、
实际鉴权、模型生成质量、连续观看或声音验收通过。
"""

import sys
sys.dont_write_bytecode = True

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import tempfile
import unittest
from decimal import Decimal
from collections import deque
from unittest import mock

TEST_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = TEST_ROOT.parent
SCRIPT = SKILL_ROOT / "scripts" / "h3.py"
sys.path.insert(0, str(TEST_ROOT))
from fake_server import FakeMetasoServer, make_test_mp4, make_test_png

spec = importlib.util.spec_from_file_location("agent_broll_h3_under_test", SCRIPT)
h3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h3)


class H3BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="agent-broll-test-"))
        self.project = Path(self.temporary) / "project"
        self.project.mkdir()
        # 动态生成且只放内存/测试环境；源文件和任何记录都没有凭据常量。
        self.key = "fake-" + secrets.token_hex(24)
        self.key_env = "AGENT_BROLL_FAKE_KEY"
        self.stack.enter_context(mock.patch.dict(os.environ, {self.key_env: self.key}))
        self.server = self.stack.enter_context(FakeMetasoServer())
        self.client = self.new_client()

    def tearDown(self):
        self.server.close()
        # 每一个测试都检查所有项目/报价/尝试/结果文件和所捕获的输出。
        secret = self.key.encode("utf-8")
        for path in Path(self.temporary).rglob("*"):
            if path.is_file():
                self.assertFalse(secret in path.read_bytes(), "临时项目文件中出现凭据")
        self.assertFalse(self.key in self.stdout.getvalue(), "标准输出中出现凭据")
        self.assertFalse(self.key in self.stderr.getvalue(), "标准错误或日志中出现凭据")
        self.assertTrue(all(self.server.download_authorization_absent), "结果下载携带了 Authorization")

    def new_client(self):
        return h3.Client(self.project, base_url=self.server.url, test_server=True,
                         key_env=self.key_env, timeout=0.2)

    def request(self, identifier="shot-1", **changes):
        value = {"id": identifier, "anchor": "文本锚点 " + identifier + "：一次错误牵动后续返工",
                 "model": self.client.provider["model"], "mode": "text_to_video",
                 "prompt": "一颗积木倾倒，碰到相邻积木，整排依次倒下，最后全部停住。固定镜头。",
                 "duration": 5, "resolution": "768P", "ratio": "16:9"}
        value.update(changes)
        return value

    def quote(self, requests=None, confirmed=True, budget="20"):
        if requests is None:
            requests = [self.request()]
        value = self.client.estimate(requests, budget)
        if confirmed:
            self.client.confirm(value["quote_id"], "我确认本批逐条估价、未知计费项与预算，可以提交。")
        return value["quote_id"]

    def submit(self, **changes):
        request = self.request(**changes)
        quote_id = self.quote([request])
        return self.client.submit(quote_id, request["id"])

    def complete(self, record, client=None):
        client = client or self.client
        for _ in range(5):
            record = client.query(record["attempt_id"])
            if record["state"] == "generated":
                return record
        self.fail("原任务未按假服务预设状态完成")

    def stored(self, attempt_id):
        path = self.project / ".agent-broll" / "attempts" / (attempt_id + ".json")
        return json.loads(path.read_text(encoding="utf-8"))

    def run_cli(self, *command):
        # 不复制宿主环境，以免子进程继承任意真实凭据。命令行没有 Key 值。
        env = {self.key_env: self.key, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--project", str(self.project),
             "--key-env", self.key_env, "--timeout", "0.2", "--test-server", self.server.url,
             *command], env=env, capture_output=True, encoding="utf-8", timeout=10)
        self.stdout.write(result.stdout)
        self.stderr.write(result.stderr)
        self.assertEqual(result.returncode, 0, "本地子进程操作应当成功")
        output = result.stdout
        if output.startswith("本次提交前报价："):
            output = output.partition("\n")[2]
        return json.loads(output)

    def test_unconfirmed_quote_refuses_submission(self):
        quote_id = self.quote(confirmed=False)
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_id, "shot-1")
        self.assertEqual(self.server.submission_count, 0)
        self.assertEqual(self.client.records(), [])

    def test_normal_lifecycle_downloads_once_with_complete_record(self):
        record = self.submit()
        self.assertEqual(record["state"], "queued")
        self.assertEqual(record["task_id"], "fake-task-1")
        self.assertEqual(self.client.query(record["attempt_id"])["state"], "queued")
        self.assertEqual(self.client.query(record["attempt_id"])["state"], "running")
        self.assertEqual(self.client.query(record["attempt_id"])["state"], "generated")
        result = self.client.download(record["attempt_id"])
        self.assertEqual(result["state"], "downloaded")
        output = self.project / result["local_file"]
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(result["media"]["actual_duration_seconds"], "6.75")
        self.assertNotEqual(result["media"]["actual_duration_seconds"], "5")
        self.assertEqual(result["media"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        stored = self.stored(record["attempt_id"])
        for field in ("request", "payload", "estimate", "quote_id", "created_at", "updated_at", "request_hash"):
            self.assertIn(field, stored)
        self.assertEqual(stored["request"]["anchor"], record["anchor"])
        self.assertEqual(stored["remote_requested_duration"], 5)
        self.assertIsNone(result["actual_fee"]["cny"])
        self.assertEqual(result["actual_fee"]["status"], "实际费用未核实")
        self.assertEqual(result["checks"]["decode"], "未检查")
        self.assertEqual(result["checks"]["continuous_watch"], "未看完")
        self.assertEqual(result["checks"]["audio_listen"], "未听审")
        self.assertEqual(self.client.records(record["attempt_id"])["state"], "downloaded")
        self.assertEqual(len(self.client.records()), 1)
        self.assertEqual((self.server.submission_count, self.server.query_count, self.server.download_count), (1, 3, 1))
        self.assertTrue(all(self.server.api_authorization_present))
        self.assertEqual(self.server.download_authorization_absent, [True])

    def test_submission_timeout_is_unknown_and_never_resubmits(self):
        self.server.submit_delay = 0.6
        quote_id = self.quote()
        record = self.client.submit(quote_id, "shot-1")
        self.assertEqual(record["state"], "submission_unknown")
        self.assertIsNone(record["task_id"])
        self.assertEqual(self.stored(record["attempt_id"])["state"], "submission_unknown")
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_id, "shot-1")
        with self.assertRaises(h3.BrollError):
            self.client.query(record["attempt_id"])
        other_quote = self.quote()
        with self.assertRaises(h3.BrollError):
            self.client.submit(other_quote, "shot-1")
        self.assertEqual(self.server.submission_count, 1)
        self.assertEqual(self.server.query_count, 0)

    def test_restart_after_received_id_queries_the_original_task(self):
        quote_id = self.quote()
        record = self.run_cli("submit", "--quote", quote_id, "--job", "shot-1")
        self.assertEqual(self.stored(record["attempt_id"])["task_id"], "fake-task-1")
        # 提交进程已退出；另启进程按落盘编号查询。
        resumed = self.run_cli("query", "--attempt", record["attempt_id"])
        self.assertEqual(resumed["task_id"], record["task_id"])
        recovered = self.new_client()
        complete = self.complete(resumed, recovered)
        self.assertEqual(recovered.download(complete["attempt_id"])["state"], "downloaded")
        with self.assertRaises(h3.BrollError):
            recovered.submit(quote_id, "shot-1")
        self.assertEqual(self.server.submission_count, 1)
        self.assertTrue(all(identifier == record["task_id"] for identifier in self.server.query_ids))

    def test_download_failure_retries_download_not_submission(self):
        self.server.download_behaviors = deque(("http_error", "valid"))
        complete = self.complete(self.submit())
        failed = self.client.download(complete["attempt_id"])
        self.assertEqual(failed["state"], "download_failed")
        queries_before = self.server.query_count
        resumed = self.new_client()
        self.assertEqual(resumed.download(complete["attempt_id"])["state"], "downloaded")
        self.assertEqual(self.server.query_count, queries_before)
        self.assertEqual((self.server.submission_count, self.server.download_count), (1, 2))

    def test_http_200_submit_error_is_failed_not_success(self):
        self.server.submit_behavior = "error"
        record = self.submit()
        self.assertEqual(record["state"], "failed")
        self.assertIsNone(record["task_id"])
        self.assertNotIn("local_file", record)
        self.assertEqual(self.server.submission_count, 1)

    def test_http_200_query_error_never_marks_success(self):
        record = self.submit()
        self.server.query_behavior = "error"
        result = self.client.query(record["attempt_id"])
        self.assertNotIn(result["state"], ("generated", "downloaded"))
        self.assertTrue(result["last_error"])
        with self.assertRaises(h3.BrollError):
            self.client.download(record["attempt_id"])
        self.assertEqual(self.server.submission_count, 1)
        self.server.query_behavior = "success"
        recovered = self.complete(record)
        self.assertEqual(self.client.download(recovered["attempt_id"])["state"], "downloaded")
        self.assertEqual(self.server.submission_count, 1)

    def test_task_error_even_with_succeeded_status_is_failed(self):
        self.server.query_behavior = "task_error"
        record = self.submit()
        result = self.client.query(record["attempt_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.server.submission_count, 1)
        self.assertEqual(self.server.download_count, 0)

    def test_content_error_even_with_succeeded_status_is_failed(self):
        self.server.query_behavior = "content_error"
        record = self.submit()
        result = self.client.query(record["attempt_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.server.submission_count, 1)
        self.assertEqual(self.server.download_count, 0)

    def assert_bad_download(self, behavior):
        self.server.download_behaviors = deque((behavior,))
        record = self.complete(self.submit())
        result = self.client.download(record["attempt_id"])
        self.assertEqual(result["state"], "download_failed")
        self.assertNotIn("local_file", result)
        self.assertFalse((self.project / ".agent-broll" / "results" / (record["attempt_id"] + ".mp4")).exists())
        self.assertEqual((self.server.submission_count, self.server.download_count), (1, 1))

    def test_empty_download_fails(self):
        self.assert_bad_download("empty")

    def test_html_download_fails(self):
        self.assert_bad_download("html")

    def test_html_with_mp4_mime_still_fails(self):
        self.assert_bad_download("html_mp4_mime")

    def test_invalid_mp4_container_fails(self):
        self.assert_bad_download("bad_container")

    def test_server_reflected_key_is_absent_from_output_and_records(self):
        self.server.submit_behavior = "error_reflect_key"
        record = self.submit()
        self.assertEqual(record["state"], "failed")
        self.stdout.write(json.dumps(self.client.records(), ensure_ascii=False))
        # tearDown 全文件及 stdout/stderr 扫描，断言失败信息也不输出 Key 本身。
        self.assertEqual(self.server.submission_count, 1)

    def test_query_reflected_key_is_not_saved_or_printed(self):
        record = self.submit()
        self.server.query_behavior = "error_reflect_key"
        result = self.client.query(record["attempt_id"])
        self.assertNotIn(result["state"], ("generated", "downloaded"))
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.server.submission_count, 1)

    def test_unusual_key_in_prompt_or_anchor_is_rejected_before_quote_write(self):
        # 假 Key 没有 mk-/sk-/Bearer 特征，必须按显式凭据值做精确比对。
        for field in ("prompt", "anchor"):
            with self.subTest(field=field):
                request = self.request(**{field: "误放凭据 " + self.key})
                with self.assertRaises(h3.BrollError):
                    self.client.estimate([request], "20")
        self.assertFalse((self.project / ".agent-broll" / "quotes").exists())
        self.assertEqual(self.server.submission_count, 0)

    def test_unusual_key_in_confirmation_is_rejected_without_saving_it(self):
        quote_id = self.quote(confirmed=False)
        with self.assertRaises(h3.BrollError):
            self.client.confirm(quote_id, "误放凭据 " + self.key)
        path = self.project / ".agent-broll" / "quotes" / (quote_id + ".json")
        self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["confirmation"])
        self.assertEqual(self.server.submission_count, 0)

    def test_estimation_does_not_require_a_key(self):
        with mock.patch.dict(os.environ, {self.key_env: ""}):
            client = self.new_client()
            quote = client.estimate([self.request()], "20")
        self.assertFalse(quote["confirmed"])
        self.assertEqual(quote["total_display_cny"], "0.45")
        self.assertEqual(self.server.submission_count, 0)

    def test_mp4_metadata_reflected_key_is_rejected_without_writing_media(self):
        # 给合法测试容器追加独立 free 盒；只有凭据防泄漏检查应该拒绝它。
        key_bytes = self.key.encode("utf-8")
        self.server.media += struct.pack(">I4s", len(key_bytes) + 8, b"free") + key_bytes
        record = self.complete(self.submit())
        resumed = self.new_client()
        result = resumed.download(record["attempt_id"])
        self.assertEqual(result["state"], "download_failed")
        self.assertNotIn("local_file", result)
        self.assertFalse((self.project / ".agent-broll" / "results").exists())
        self.assertEqual((self.server.submission_count, self.server.download_count), (1, 1))

    def test_download_does_not_require_a_key_or_send_authorization(self):
        record = self.complete(self.submit())
        with mock.patch.dict(os.environ, {self.key_env: ""}):
            resumed = self.new_client()
            result = resumed.download(record["attempt_id"])
        self.assertEqual(result["state"], "downloaded")
        self.assertEqual(self.server.download_authorization_absent, [True])

    def test_estimate_matches_provider_all_supported_billing_items(self):
        request = self.request(mode="reference", duration=7, resolution="2K",
                               images=[{"url": "https://example.test/image-" + str(i) + ".png",
                                        "role": "reference_image"} for i in range(7)],
                               videos=[{"url": "https://example.test/input.mp4", "role": "reference_video",
                                        "duration_seconds": "2.5"}],
                               audios=[{"url": "https://example.test/input.wav", "role": "reference_audio"}])
        quote = self.client.estimate([request], "20")
        pricing = self.client.provider["pricing"]
        output = pricing["output_per_second"]["2K"]
        extra = pricing["input_images"]["excess_per_image"]
        video = pricing["input_video_per_second"]["2K"]
        expected_credits = (Decimal("7") * Decimal(output["credits"])
                            + Decimal("2") * Decimal(extra["credits"])
                            + Decimal("2.5") * Decimal(video["credits"]))
        expected_cny = (Decimal("7") * Decimal(output["display_cny"])
                        + Decimal("2") * Decimal(extra["display_cny"])
                        + Decimal("2.5") * Decimal(video["display_cny"]))
        estimate = quote["jobs"][0]["estimate"]
        self.assertEqual(Decimal(estimate["credits"]), expected_credits)
        self.assertEqual(Decimal(estimate["display_cny"]), expected_cny)
        quantities = [row["quantity"] for row in estimate["items"]]
        self.assertEqual(quantities, ["7", "2", "2.5", "1", "0"])
        self.assertEqual(estimate["actual_fee_status"], "实际费用未核实")
        self.assertEqual(quote["remaining_cny_after_batch"], str(Decimal("20") - expected_cny))
        self.assertEqual((self.server.submission_count, self.server.query_count, self.server.download_count), (0, 0, 0))

    def test_image_free_boundary_and_excess_charge(self):
        pricing = self.client.provider["pricing"]
        for count in (5, 6):
            with self.subTest(images=count):
                request = self.request(mode="reference", images=[
                    {"url": "https://example.test/image-" + str(i) + ".png", "role": "reference_image"}
                    for i in range(count)])
                quote = self.client.estimate([request], "20")
                expected = (Decimal("5") * Decimal(pricing["output_per_second"]["768P"]["display_cny"])
                            + Decimal(max(0, count - pricing["input_images"]["free_count"]))
                            * Decimal(pricing["input_images"]["excess_per_image"]["display_cny"]))
                self.assertEqual(Decimal(quote["total_display_cny"]), expected)
        self.assertEqual(self.server.submission_count, 0)

    def test_approved_parameter_change_refuses_submission(self):
        quote_id = self.quote()
        path = self.project / ".agent-broll" / "quotes" / (quote_id + ".json")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["jobs"][0]["duration"] = 8
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_id, "shot-1")
        self.assertEqual(self.server.submission_count, 0)
        self.assertEqual(self.client.records(), [])

    def test_quote_change_before_confirmation_is_rejected(self):
        quote_id = self.quote(confirmed=False)
        path = self.project / ".agent-broll" / "quotes" / (quote_id + ".json")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["jobs"][0]["duration"] = 8
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(h3.BrollError):
            self.client.confirm(quote_id, "确认被改过的文件")
        self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["confirmation"])
        self.assertEqual(self.server.submission_count, 0)

    def test_approved_local_asset_change_refuses_submission(self):
        image = self.project / "first-frame.png"
        image.write_bytes(make_test_png())
        request = self.request(mode="first_frame", ratio="adaptive",
                               images=[{"path": "first-frame.png", "role": "first_frame"}])
        quote_id = self.quote([request])
        image.write_bytes(make_test_png((10, 20, 30)))
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_id, "shot-1")
        self.assertEqual(self.server.submission_count, 0)
        self.assertEqual(self.client.records(), [])

    def test_approved_pricing_rule_change_refuses_submission(self):
        quote_id = self.quote()
        changed = copy.deepcopy(self.client.provider)
        changed["pricing"]["output_per_second"]["768P"]["display_cny"] = "0.11"
        path = Path(self.temporary) / "changed-provider.json"
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(h3, "PROVIDER_PATH", path):
            revised_client = self.new_client()
        with self.assertRaises(h3.BrollError):
            revised_client.submit(quote_id, "shot-1")
        self.assertEqual(self.server.submission_count, 0)

    def test_batch_confirmation_once_submits_in_explicit_order(self):
        requests = [self.request("shot-" + str(i), duration=4 + i) for i in range(1, 4)]
        quote_id = self.quote(requests)
        results = [self.client.submit(quote_id, row["id"]) for row in requests]
        self.assertEqual([row["job_id"] for row in results], ["shot-1", "shot-2", "shot-3"])
        self.assertEqual([row["task_id"] for row in results], ["fake-task-1", "fake-task-2", "fake-task-3"])
        self.assertEqual([row["duration"] for row in self.server.submissions], [5, 6, 7])
        self.assertEqual(self.server.submission_count, 3)
        self.assertEqual(len(self.client.records()), 3)
        self.assertEqual(self.stdout.getvalue().count("本次提交前报价："), 3)
        with self.assertRaises(h3.BrollError):
            self.client.confirm(quote_id, "重复确认同一报价")

    def test_crash_with_prewrite_but_no_id_recovers_conservatively(self):
        quote_id = self.quote()
        attempt_id = quote_id + "--shot-1"
        # 模拟写出 submitting 后、发送函数入口处进程退出，不伪造尝试记录。
        with mock.patch.object(self.client, "_json_request", side_effect=SystemExit("模拟退出")):
            with self.assertRaises(SystemExit):
                self.client.submit(quote_id, "shot-1")
        self.assertEqual(self.stored(attempt_id)["state"], "submitting")
        recovered = self.new_client()
        self.assertEqual(recovered.records(attempt_id)["state"], "submission_unknown")
        self.assertEqual(self.stored(attempt_id)["state"], "submission_unknown")
        with self.assertRaises(h3.BrollError):
            recovered.submit(quote_id, "shot-1")
        with self.assertRaises(h3.BrollError):
            recovered.query(attempt_id)
        self.assertEqual((self.server.submission_count, self.server.query_count), (0, 0))

    def test_normal_submission_observes_prewrite_and_immediate_id_persistence(self):
        quote_id = self.quote()
        attempt_id = quote_id + "--shot-1"

        def observe(payload, task_id):
            record = self.stored(attempt_id)
            return {"state": record["state"], "task_id": record["task_id"],
                    "request_present": bool(record.get("request")),
                    "estimate_present": bool(record.get("estimate")),
                    "payload_matches": record.get("payload") == payload}

        self.server.on_submit = observe
        record = self.client.submit(quote_id, "shot-1")
        self.assertIsNone(self.server.observer_error)
        self.assertEqual(self.server.observations, [{"state": "submitting", "task_id": None,
                         "request_present": True, "estimate_present": True, "payload_matches": True}])
        self.assertEqual(self.stored(attempt_id)["task_id"], record["task_id"])
        self.assertEqual(self.stored(attempt_id)["state"], "queued")
        self.assertEqual(self.server.submission_count, 1)

    def test_downloaded_result_reuse_and_requery_never_resubmits(self):
        record = self.complete(self.submit())
        result = self.client.download(record["attempt_id"])
        self.assertEqual(self.client.download(record["attempt_id"])["state"], "downloaded")
        self.assertEqual(self.client.query(record["attempt_id"])["state"], "downloaded")
        self.assertEqual(self.new_client().download(record["attempt_id"])["media"], result["media"])
        self.assertEqual((self.server.submission_count, self.server.download_count), (1, 1))

    def test_no_id_success_envelope_is_unknown_not_retryable(self):
        self.server.submit_behavior = "missing_id"
        record = self.submit()
        self.assertEqual(record["state"], "submission_unknown")
        with self.assertRaises(h3.BrollError):
            self.client.submit(record["quote_id"], record["job_id"])
        self.assertEqual(self.server.submission_count, 1)

    def test_unknown_attempt_cannot_be_blindly_redone_with_new_quote(self):
        self.server.submit_behavior = "missing_id"
        record = self.submit()
        request = self.request("shot-2", redo_of=record["attempt_id"])
        quote_id = self.quote([request])
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_id, "shot-2")
        self.assertEqual(self.server.submission_count, 1)

    def test_unknown_a_cannot_use_successful_b_as_a_redo_escape(self):
        request_a = self.request("shot-a")
        self.server.submit_behavior = "missing_id"
        quote_a = self.quote([request_a])
        unknown_a = self.client.submit(quote_a, "shot-a")
        self.assertEqual(unknown_a["state"], "submission_unknown")
        self.server.submit_behavior = "success"
        request_b = self.request("shot-b")
        quote_b = self.quote([request_b])
        successful_b = self.client.submit(quote_b, "shot-b")
        self.assertEqual(successful_b["state"], "queued")
        forged_redo = dict(request_a, redo_of=successful_b["attempt_id"])
        quote_redo = self.quote([forged_redo])
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_redo, "shot-a")
        self.assertEqual(self.server.submission_count, 2)

    def test_redo_of_requires_the_original_job_and_anchor(self):
        original = self.submit()
        for request in (self.request("shot-2", redo_of=original["attempt_id"]),
                        self.request(anchor="另一个原文锚点", redo_of=original["attempt_id"])):
            with self.subTest(job=request["id"]):
                quote_id = self.quote([request])
                with self.assertRaises(h3.BrollError):
                    self.client.submit(quote_id, request["id"])
        self.assertEqual(self.server.submission_count, 1)

    def test_budget_overrun_and_paid_optimization_are_rejected_locally(self):
        with self.assertRaises(h3.BrollError):
            self.client.estimate([self.request()], "0.01")
        with self.assertRaises(h3.BrollError):
            self.client.estimate([self.request(prompt_optimization=True)], "20")
        self.assertEqual(self.server.submission_count, 0)

    def test_local_reference_video_prices_actual_container_duration(self):
        video = self.project / "input.mp4"
        video.write_bytes(make_test_mp4("2.5"))
        request = self.request(mode="reference", videos=[{"path": "input.mp4", "role": "reference_video"}])
        quote = self.client.estimate([request], "20")
        estimate = quote["jobs"][0]["estimate"]
        self.assertEqual(estimate["items"][2]["quantity"], "2.5")
        rate = Decimal(self.client.provider["pricing"]["output_per_second"]["768P"]["display_cny"])
        input_rate = Decimal(self.client.provider["pricing"]["input_video_per_second"]["768P"]["display_cny"])
        self.assertEqual(Decimal(estimate["display_cny"]), Decimal("5") * rate + Decimal("2.5") * input_rate)
        self.assertEqual(self.server.submission_count, 0)

    def test_mm_file_reference_is_estimated_without_network_access(self):
        request = self.request(mode="reference", images=[
            {"url": "mm_file://fake-file-123", "role": "reference_image"}])
        quote = self.client.estimate([request], "20")
        self.assertEqual(quote["jobs"][0]["mode"], "reference")
        self.assertEqual(quote["total_display_cny"], "0.45")
        self.assertEqual((self.server.submission_count, self.server.query_count, self.server.download_count), (0, 0, 0))

    def test_record_root_symlink_cannot_write_outside_project(self):
        outside = Path(self.temporary) / "outside"
        outside.mkdir()
        (self.project / ".agent-broll").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(h3.BrollError):
            self.new_client()
        self.assertEqual(list(outside.iterdir()), [])

    def test_attempts_symlink_cannot_write_outside_records(self):
        quote_id = self.quote()
        outside = Path(self.temporary) / "outside"
        outside.mkdir()
        (self.project / ".agent-broll" / "attempts").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(h3.BrollError):
            self.client.submit(quote_id, "shot-1")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.server.submission_count, 0)

    def test_quote_file_symlink_cannot_be_updated_outside_records(self):
        quote_id = self.quote(confirmed=False)
        path = self.project / ".agent-broll" / "quotes" / (quote_id + ".json")
        outside = Path(self.temporary) / "outside-quote.json"
        original = path.read_bytes()
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(h3.BrollError):
            self.client.confirm(quote_id, "确认此报价")
        self.assertEqual(outside.read_bytes(), original)
        self.assertEqual(self.server.submission_count, 0)

    def test_results_symlink_cannot_write_outside_records(self):
        record = self.complete(self.submit())
        outside = Path(self.temporary) / "outside"
        outside.mkdir()
        (self.project / ".agent-broll" / "results").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(h3.BrollError):
            self.client.download(record["attempt_id"])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.server.submission_count, self.server.download_count), (1, 0))

    def test_modified_downloaded_file_is_not_automatically_overwritten(self):
        record = self.complete(self.submit())
        result = self.client.download(record["attempt_id"])
        output = self.project / result["local_file"]
        edited = b"user-edited-test-file"
        output.write_bytes(edited)
        with self.assertRaises(h3.BrollError):
            self.new_client().download(record["attempt_id"])
        self.assertEqual(output.read_bytes(), edited)
        self.assertEqual((self.server.submission_count, self.server.download_count), (1, 1))

    def test_test_server_rejects_nonlocal_address(self):
        with self.assertRaises(h3.BrollError):
            h3.Client(self.project, base_url="http://example.test", test_server=True, key_env=self.key_env)
        self.assertEqual(self.server.submission_count, 0)


if __name__ == "__main__":
    unittest.main()
