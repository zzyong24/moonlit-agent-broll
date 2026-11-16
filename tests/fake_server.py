"""仅供本地行为测试的秘塔形态服务；不发起外部请求，不记录鉴权内容。

服务只绑定 127.0.0.1。测试媒体只有可读取时长和轨道标识的 MP4 容器，
没有真正编码的视频帧，不能用于播放、解码质量或视听验收。
"""

import json
import struct
import threading
import time
import zlib
from collections import deque
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def make_test_mp4(duration_seconds="6.75", audio=False):
    """独立构造测试容器；不依赖被测代码生成期望值，也不伪称可播放。"""
    timescale = 1000
    ticks = int(Decimal(str(duration_seconds)) * timescale)
    if ticks <= 0:
        raise ValueError("测试媒体时长须大于零")
    matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = (b"\x00\x00\x00\x00" + struct.pack(">4I", 0, 0, timescale, ticks)
            + struct.pack(">IHH", 0x10000, 0x100, 0) + b"\x00" * 8
            + matrix + b"\x00" * 24 + struct.pack(">I", 3 if audio else 2))

    def track(handler):
        hdlr = b"\x00" * 8 + handler + b"\x00" * 12 + b"Test track\x00"
        return _box(b"trak", _box(b"mdia", _box(b"hdlr", hdlr)))

    moov = _box(b"mvhd", mvhd) + track(b"vide")
    if audio:
        moov += track(b"soun")
    return (_box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isommp42")
            + _box(b"moov", moov) + _box(b"mdat", b"container-only-test"))


def make_test_png(rgb=(90, 120, 160)):
    """一像素自制 PNG，仅验证本地素材指纹变化，不含角色或私人图片。"""
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
            + chunk(b"IEND", b""))


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 超时后客户端已断开属于预期场景；禁止输出请求或异常中的鉴权。
        pass


class FakeMetasoServer:
    """可控制提交延迟、200 错误、查询状态和下载失败的本机假服务。"""

    def __init__(self, statuses=("queued", "running", "succeeded"),
                 submit_behavior="success", submit_delay=0,
                 query_behavior="success", download_behaviors=("valid",),
                 duration_seconds="6.75", on_submit=None):
        self.statuses = tuple(statuses)
        if not self.statuses:
            raise ValueError("假服务至少需要一个查询状态")
        self.submit_behavior = submit_behavior
        self.submit_delay = submit_delay
        self.query_behavior = query_behavior
        self.download_behaviors = deque(download_behaviors)
        self.media = make_test_mp4(duration_seconds)
        self.on_submit = on_submit
        self.lock = threading.Lock()
        self.submissions = []
        self.query_ids = []
        self.download_ids = []
        self.api_authorization_present = []
        self.download_authorization_absent = []
        self.observations = []
        self.observer_error = None
        self.tasks = {}
        self.httpd = _QuietServer(("127.0.0.1", 0), self._handler())
        self.url = "http://127.0.0.1:" + str(self.httpd.server_address[1])
        self.thread = None

    @property
    def submission_count(self):
        with self.lock:
            return len(self.submissions)

    @property
    def query_count(self):
        with self.lock:
            return len(self.query_ids)

    @property
    def download_count(self):
        with self.lock:
            return len(self.download_ids)

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                # 覆盖默认访问日志，任何请求头均不进入日志。
                pass

            def _send(self, status, data, content_type="application/json"):
                if isinstance(data, dict):
                    data = json.dumps(data).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def _error(self, reflected=False):
                # 反射仅存在于这一次内存响应中；不保存请求头或原始错误。
                detail = self.headers.get("Authorization", "") if reflected else "模拟接口错误"
                return {"type": "error", "error": {"type": "fake_error", "message": detail}}

            def do_POST(self):
                if urlsplit(self.path).path != "/v2/video_generation":
                    self._send(404, self._error())
                    return
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                except (ValueError, OSError):
                    self._send(400, self._error())
                    return
                with owner.lock:
                    owner.submissions.append(payload)
                    owner.api_authorization_present.append(self.headers.get("Authorization") is not None)
                    task_id = "fake-task-" + str(len(owner.submissions))
                    owner.tasks[task_id] = {"payload": payload, "cursor": 0}
                if owner.on_submit is not None:
                    try:
                        observation = owner.on_submit(payload, task_id)
                        with owner.lock:
                            owner.observations.append(observation)
                    except Exception as error:
                        owner.observer_error = type(error).__name__
                        self._send(500, self._error())
                        return
                if owner.submit_delay:
                    time.sleep(owner.submit_delay)
                behavior = owner.submit_behavior
                if behavior == "error":
                    self._send(200, self._error())
                elif behavior == "error_reflect_key":
                    self._send(200, self._error(reflected=True))
                elif behavior == "missing_id":
                    self._send(200, {"status": "accepted"})
                else:
                    self._send(200, {"task_id": task_id})

            def do_GET(self):
                path = urlsplit(self.path).path
                query_prefix = "/v2/query/video_generation/"
                file_prefix = "/files/"
                if path.startswith(query_prefix):
                    task_id = path[len(query_prefix):]
                    with owner.lock:
                        owner.query_ids.append(task_id)
                        owner.api_authorization_present.append(self.headers.get("Authorization") is not None)
                        task = owner.tasks.get(task_id)
                        if task:
                            status = owner.statuses[min(task["cursor"], len(owner.statuses) - 1)]
                            task["cursor"] += 1
                            duration = task["payload"].get("duration")
                    if not task:
                        self._send(404, self._error())
                        return
                    behavior = owner.query_behavior
                    if behavior in ("error", "error_reflect_key"):
                        self._send(200, self._error(reflected=behavior == "error_reflect_key"))
                        return
                    value = {"id": task_id, "status": status, "content": {},
                             "duration": duration, "usage": {"completion_tokens": 12}}
                    if status == "succeeded":
                        value["content"] = {"url": owner.url + file_prefix + task_id + ".mp4"}
                    if behavior == "task_error":
                        value.update(status="succeeded", error={"message": "模拟任务失败"})
                    elif behavior == "content_error":
                        value.update(status="succeeded", content={"type": "error", "error": {"message": "模拟内容错误"}})
                    self._send(200, {"task": value})
                    return
                if path.startswith(file_prefix):
                    task_id = path[len(file_prefix):].removesuffix(".mp4")
                    with owner.lock:
                        owner.download_ids.append(task_id)
                        owner.download_authorization_absent.append(self.headers.get("Authorization") is None)
                        behavior = owner.download_behaviors.popleft() if owner.download_behaviors else "valid"
                    if behavior == "http_error":
                        self._send(503, b"temporarily unavailable", "text/plain")
                    elif behavior == "empty":
                        self._send(200, b"", "video/mp4")
                    elif behavior == "html":
                        self._send(200, b"<!doctype html><html>error</html>", "text/html")
                    elif behavior == "html_mp4_mime":
                        self._send(200, b"<!doctype html><html>error</html>", "video/mp4")
                    elif behavior == "bad_container":
                        self._send(200, b"not-an-mp4", "video/mp4")
                    else:
                        self._send(200, owner.media, "video/mp4")
                    return
                self._send(404, self._error())

        return Handler

    def __enter__(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.thread is not None:
            self.httpd.shutdown()
            self.thread.join(timeout=2)
            self.thread = None
        self.httpd.server_close()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
