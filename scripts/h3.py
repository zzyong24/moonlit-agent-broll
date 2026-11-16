#!/usr/bin/env python3
"""秘塔 H3 的本地报价、授权绑定和单次提交工具，仅使用 Python 标准库。"""

import argparse
import base64
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.dont_write_bytecode = True
SKILL_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PATH = SKILL_ROOT / "providers" / "metaso_h3.json"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
SECRET_PATTERN = re.compile(r"(?i)(?:bearer\s+[A-Za-z0-9_.-]{12,}|(?:mk|sk)-[A-Za-z0-9_-]{8,})")
MAX_RESPONSE = 2 * 1024 * 1024
MAX_DOWNLOAD = 256 * 1024 * 1024  # 本地内存保护，不代表供应商媒体上限。


class BrollError(Exception):
    """只携带本地安全说明，不包含凭据或原始服务端响应。"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value):
    if isinstance(value, bool):
        raise BrollError("金额或时长不能是布尔值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BrollError("金额或时长须为有效数值") from None
    if not result.is_finite() or result < 0:
        raise BrollError("金额或时长须为非负有限数值")
    return result


def fmt(value):
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise BrollError("无法读取有效的本地 JSON；请检查指定文件") from None


def atomic_json(path, value, exclusive=False):
    """先写完整临时文件并 fsync，再原子替换；首次尝试用独占链接防止重投。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(canonical(value) + "\n")
            out.flush()
            os.fsync(out.fileno())
        if exclusive:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise BrollError("该尝试已有记录；只能恢复查询或下载") from None
            temporary.unlink()
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def check_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise BrollError("记录编号只能使用字母、数字、下划线或连字符")
    return value


def guard_text(value):
    if SECRET_PATTERN.search(canonical(value)):
        raise BrollError("输入疑似包含凭据；请移除凭据，禁止写进请求或确认文字")


def api_error(value):
    if not isinstance(value, dict):
        return True
    if value.get("type") == "error" or value.get("error"):
        return True
    if value.get("errCode") not in (None, 0, "0"):
        return True
    base = value.get("base_resp")
    return isinstance(base, dict) and base.get("status_code") not in (None, 0, "0")


def mp4_info(data):
    """检查 MP4 容器与视频轨，读真实容器时长；不声称已解码或看听完整视频。"""
    if not data or data.lstrip()[:20].lower().startswith((b"<!doctype", b"<html", b"<?xml")):
        raise BrollError("下载结果为空或网页错误页")

    def boxes(start, end):
        result = []
        while start < end:
            if end - start < 8:
                raise BrollError("MP4 容器截断")
            size, kind = struct.unpack_from(">I4s", data, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    raise BrollError("MP4 扩展头截断")
                size = struct.unpack_from(">Q", data, start + 8)[0]
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise BrollError("MP4 容器长度无效")
            result.append((kind, start + header, start + size))
            start += size
        return result

    top = boxes(0, len(data))
    types = {kind for kind, _, _ in top}
    if not {b"ftyp", b"moov", b"mdat"} <= types:
        raise BrollError("下载内容不是完整 MP4 容器")
    if not any(kind == b"mdat" and end > start for kind, start, end in top):
        raise BrollError("MP4 媒体数据为空")
    duration = None
    tracks = []
    for kind, start, end in top:
        if kind != b"moov":
            continue
        for inner, a, b in boxes(start, end):
            if inner == b"mvhd":
                version = data[a] if b > a else -1
                needed = 20 if version == 0 else 32 if version == 1 else 0
                if not needed or b - a < needed:
                    raise BrollError("MP4 时长字段无效")
                scale = struct.unpack_from(">I", data, a + (12 if version == 0 else 20))[0]
                ticks = struct.unpack_from(">I" if version == 0 else ">Q", data, a + (16 if version == 0 else 24))[0]
                if not scale or not ticks:
                    raise BrollError("MP4 时长为零或无效")
                duration = Decimal(ticks) / Decimal(scale)
            if inner == b"trak":
                for child, c, d in boxes(a, b):
                    if child == b"mdia":
                        for leaf, e, f in boxes(c, d):
                            if leaf == b"hdlr" and f - e >= 12:
                                tracks.append(data[e + 8:e + 12])
    if duration is None or b"vide" not in tracks:
        raise BrollError("MP4 缺少有效时长或视频轨")
    return {"actual_duration_seconds": fmt(duration), "audio_track_present": b"soun" in tracks,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class Client:
    """一个项目由一个操作者顺序执行；不实现跨 Agent 共享预算引擎。"""

    def __init__(self, project, base_url=None, test_server=False,
                 key_env="METASO_API_KEY", key_file=None, timeout=30):
        self.project = Path(project).expanduser().resolve()
        if self.project == SKILL_ROOT or SKILL_ROOT in self.project.parents:
            raise BrollError("项目不能放在 skill 安装目录内")
        self.root = self.project / ".agent-broll"
        resolved_root = self.root.resolve()
        if self.project not in resolved_root.parents or resolved_root == SKILL_ROOT or SKILL_ROOT in resolved_root.parents:
            raise BrollError("项目记录目录不能通过符号链接写到项目之外或 skill 内")
        self.provider = read_json(PROVIDER_PATH)
        self.provider_hash = fingerprint(self.provider)
        self.test_server = test_server
        self.base_url = (base_url or self.provider["api"]["base_url"]).rstrip("/")
        url = urlparse(self.base_url)
        if url.username or url.password or url.query or url.fragment:
            raise BrollError("API 地址不能含凭据、查询参数或片段")
        if test_server:
            if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise BrollError("测试模式只允许本机 HTTP 假服务")
        elif self.base_url != self.provider["api"]["base_url"].rstrip("/"):
            raise BrollError("此接入只能使用官方秘塔地址；更换供应商须新接入及新报价")
        self.key_env, self.key_file = key_env, key_file
        self.timeout = float(timeout)
        if not self.timeout > 0:
            raise BrollError("超时时间须大于零")
        self.opener = build_opener(ProxyHandler({}), NoRedirect()) if test_server else build_opener(NoRedirect())
        self._secret = None

    def _key(self, required=True):
        # 只读显式环境变量或用户指定文件；不扫描配置、目录或其他环境变量。
        if self.key_file is not None:
            try:
                value = Path(self.key_file).expanduser().read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise BrollError("无法读取指定凭据位置") from None
        else:
            value = os.environ.get(self.key_env, "").strip()
        if not value and not required:
            return None
        if not value or any(char.isspace() for char in value):
            raise BrollError("凭据未配置或格式无效；不要把 Key 放在命令行或聊天里")
        self._secret = value
        return value

    def _has_secret(self, value):
        return bool(self._secret and self._secret in canonical(value))

    def _guard(self, value):
        guard_text(value)
        # 报价不要求配置 Key；已配置时仅检查显式来源，避免无常见前缀的 Key 泄入草稿。
        self._key(required=False)
        if self._has_secret(value):
            raise BrollError("输入包含当前凭据，拒绝写入本地文件或输出")

    def _json_request(self, path, payload=None):
        key = self._key()
        body = canonical(payload).encode() if payload is not None else None
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.base_url + path, data=body, headers=headers,
                      method="POST" if body is not None else "GET")
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BrollError("接口响应过大，未保留原始响应")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise BrollError("接口未返回 JSON 对象")
            return status, value
        except (URLError, HTTPException, socket.timeout, TimeoutError, OSError, ValueError, UnicodeError):
            raise BrollError("接口连接、读取或 JSON 解析失败；未记录原始异常") from None

    def _path(self, kind, identifier):
        path = self.root / kind / (check_name(identifier) + ".json")
        if self.root.resolve() not in path.resolve().parents:
            raise BrollError("记录路径不能通过符号链接离开项目记录目录")
        return path

    def _all_attempts(self):
        directory = self.root / "attempts"
        if self.root.resolve() not in directory.resolve().parents:
            raise BrollError("尝试目录不能通过符号链接离开项目记录目录")
        return [read_json(path) for path in sorted(directory.glob("*.json"))]

    def _reserved(self):
        # 不把失败或结果不明的估价当作已退款；没有退款证据就保留占用。
        return sum((number(row["estimate"]["display_cny"]) for row in self._all_attempts()), Decimal(0))

    def _asset(self, item, kind):
        if not isinstance(item, dict) or set(item) - {"path", "url", "role", "duration_seconds"}:
            raise BrollError("素材字段不合法")
        if ("path" in item) == ("url" in item):
            raise BrollError("素材须且只能提供 path 或 url")
        if item.get("role") not in self.provider["capabilities"]["media_roles"][kind]:
            raise BrollError("素材用途不受当前模式支持")
        asset = {"role": item["role"]}
        if "path" in item:
            path = (self.project / Path(item["path"]).expanduser()).resolve()
            if path == SKILL_ROOT or SKILL_ROOT in path.parents:
                raise BrollError("不从 skill 安装目录读取用户素材")
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".mp4": "video/mp4", ".mp3": "audio/mpeg",
                    ".wav": "audio/wav", ".m4a": "audio/mp4"}.get(path.suffix.lower())
            category = {"images": "image/", "videos": "video/", "audios": "audio/"}[kind]
            if mime is None or not mime.startswith(category):
                raise BrollError("本地素材格式不在执行器支持范围")
            try:
                data = path.read_bytes()
            except OSError:
                raise BrollError("无法读取指定素材") from None
            if not data or len(data) > MAX_DOWNLOAD:
                raise BrollError("素材为空或超过执行器内存保护上限")
            asset.update(path=str(path), sha256=hashlib.sha256(data).hexdigest(), mime=mime)
            if kind == "videos":
                asset["duration_seconds"] = mp4_info(data)["actual_duration_seconds"]
                if "duration_seconds" in item and number(item["duration_seconds"]) != number(asset["duration_seconds"]):
                    raise BrollError("参考视频申报时长与容器时长不同，须重新核价")
        else:
            url = item["url"]
            parsed = urlparse(url) if isinstance(url, str) else None
            # 官方 mm_file 带下划线，不是 urlparse 所识别的 RFC scheme，须明确识别。
            mm_file = isinstance(url, str) and re.fullmatch(r"mm_file://[A-Za-z0-9_-]{1,120}", url)
            if parsed is None or parsed.username or parsed.password or not (
                (parsed.scheme == "https" and parsed.hostname) or
                mm_file):
                raise BrollError("素材地址只能使用无凭据的 HTTPS 或 mm_file 引用；本地素材用 path")
            asset.update(url=url, remote_content_version="未知；须由用户核对素材版本和传输许可")
            if kind == "videos":
                if "duration_seconds" not in item:
                    raise BrollError("URL 参考视频须提供已核对的输入时长，不能漏算费用")
                asset["duration_seconds"] = fmt(number(item["duration_seconds"]))
        if kind == "videos" and number(asset["duration_seconds"]) <= 0:
            raise BrollError("参考视频时长须大于零")
        return asset

    def _freeze(self, request):
        allowed = {"id", "anchor", "model", "mode", "prompt", "duration", "resolution", "ratio",
                   "images", "videos", "audios", "prompt_optimization", "redo_of"}
        if not isinstance(request, dict) or set(request) - allowed:
            raise BrollError("请求含未知字段，禁止透传凭据或供应商参数")
        self._guard(request)
        job = {key: request.get(key) for key in ("id", "anchor", "model", "mode", "prompt", "duration", "resolution", "ratio")}
        check_name(job["id"])
        if not all(isinstance(job[key], str) and job[key].strip() for key in ("anchor", "prompt")):
            raise BrollError("原文锚点和视频提示词不能为空")
        cap = self.provider["capabilities"]
        if job["model"] != self.provider["model"] or job["mode"] not in cap["modes"]:
            raise BrollError("模型或模式不受本接入支持")
        if type(job["duration"]) is not int or not cap["duration_seconds"]["executor_minimum"] <= job["duration"] <= cap["duration_seconds"]["executor_maximum"]:
            raise BrollError("时长须为 5–15 整数秒；4 秒资料冲突，不能默改参数")
        if job["resolution"] not in cap["resolutions"] or job["ratio"] not in cap["ratios"]:
            raise BrollError("分辨率或画面比例不受支持")
        if request.get("prompt_optimization", False) is not False:
            raise BrollError("本轮不执行收费提示词优化；不会忽略开启请求")
        job["prompt_optimization"] = False
        for kind in ("images", "videos", "audios"):
            items = request.get(kind, [])
            if not isinstance(items, list) or len(items) > cap["reference_limits"][kind]:
                raise BrollError("参考素材数量超出官方文档范围")
            job[kind] = [self._asset(item, kind) for item in items]
        roles = [item["role"] for item in job["images"]]
        if job["mode"] == "text_to_video":
            valid = not roles and not job["videos"] and not job["audios"]
        elif job["mode"] in ("first_frame", "first_last_frame"):
            valid = sorted(roles) == (["first_frame"] if job["mode"] == "first_frame" else ["first_frame", "last_frame"])
            valid = valid and not job["videos"] and not job["audios"] and job["ratio"] == cap["frame_mode_ratio"]
        else:
            valid = bool(roles or job["videos"] or job["audios"]) and all(role == "reference_image" for role in roles)
        if not valid:
            raise BrollError("模式、素材用途或比例组合不合法；首尾帧与多参考不得混用")
        if request.get("redo_of") is not None:
            job["redo_of"] = check_name(request["redo_of"])
        # 本地去重含原文锚点和素材指纹；不声称它是远端幂等键。
        job["request_hash"] = fingerprint({key: value for key, value in job.items() if key not in ("id", "redo_of")})
        return job

    def _cost(self, job):
        pricing = self.provider["pricing"]
        rows = []

        def add(label, quantity, rate):
            quantity = number(quantity)
            rows.append({"item": label, "quantity": fmt(quantity), "unit_credits": rate["credits"],
                         "unit_display_cny": rate["display_cny"],
                         "credits": fmt(quantity * number(rate["credits"])),
                         "display_cny": fmt(quantity * number(rate["display_cny"]))})

        add("输出视频秒数", job["duration"], pricing["output_per_second"][job["resolution"]])
        excess = max(0, len(job["images"]) - pricing["input_images"]["free_count"])
        add("超额输入图片张数", excess, pricing["input_images"]["excess_per_image"])
        seconds = sum((number(item["duration_seconds"]) for item in job["videos"]), Decimal(0))
        add("参考视频输入秒数", seconds, pricing["input_video_per_second"][job["resolution"]])
        add("输入音频份数", len(job["audios"]), pricing["input_audio"])
        add("提示词优化调用次数（关闭）", 0, pricing["prompt_optimization"]["per_call"])
        credits = sum((number(row["credits"]) for row in rows), Decimal(0))
        cny = sum((number(row["display_cny"]) for row in rows), Decimal(0))
        exchange = pricing["reference_exchange"]
        return {"items": rows, "credits": fmt(credits), "display_cny": fmt(cny),
                "reference_exchange_cny": fmt(credits * number(exchange["cny"]) / number(exchange["credits"])),
                "actual_fee_status": "实际费用未核实", "checked_on": self.provider["checked_on"],
                "sources": [source["url"] for source in self.provider["sources"][:2]],
                "unknowns": {key: pricing[key] for key in ("credit_rounding", "reference_video_time_rounding", "actual_account_exchange", "failed_task_charge_or_refund")}}

    def estimate(self, requests, budget_cny):
        if not isinstance(requests, list) or not requests:
            raise BrollError("报价须包含至少一个请求")
        jobs = [self._freeze(request) for request in requests]
        if len({job["id"] for job in jobs}) != len(jobs):
            raise BrollError("批内镜头编号重复")
        for job in jobs:
            job["estimate"] = self._cost(job)
        total = sum((number(job["estimate"]["display_cny"]) for job in jobs), Decimal(0))
        budget, reserved = number(budget_cny), self._reserved()
        if total + reserved > budget:
            raise BrollError("本批估价加已占用额度超过项目预算，未创建报价")
        result = {"quote_id": "q-" + secrets.token_hex(8), "created_at": now(), "base_url": self.base_url,
                  "provider_hash": self.provider_hash, "jobs": jobs, "total_display_cny": fmt(total),
                  "total_credits": fmt(sum((number(job["estimate"]["credits"]) for job in jobs), Decimal(0))),
                  "budget_cny": fmt(budget), "reserved_cny_at_quote": fmt(reserved),
                  "remaining_cny_after_batch": fmt(budget - reserved - total), "confirmation": None}
        result["content_hash"] = self._quote_hash(result)
        atomic_json(self._path("quotes", result["quote_id"]), result, exclusive=True)
        return self.quote_summary(result)

    @staticmethod
    def _quote_hash(value):
        return fingerprint({key: item for key, item in value.items() if key not in ("confirmation", "content_hash")})

    def _quote(self, quote_id):
        value = read_json(self._path("quotes", quote_id))
        if value.get("quote_id") != quote_id or value.get("base_url") != self.base_url or value.get("provider_hash") != self.provider_hash:
            raise BrollError("报价、接口或计价规则已变化，须重新列价确认")
        if value.get("content_hash") != self._quote_hash(value):
            raise BrollError("报价内容已改动，须重新估价并交用户确认")
        return value

    def confirm(self, quote_id, user_confirmation):
        if not isinstance(user_confirmation, str) or not user_confirmation.strip():
            raise BrollError("必须记录用户对这份报价及未知计费项的明确确认，不能用预算授权代替")
        self._guard(user_confirmation)
        value = self._quote(quote_id)
        if value.get("confirmation"):
            raise BrollError("报价已确认；需要修改就创建新报价")
        value["confirmation"] = {"at": now(), "user_confirmation": user_confirmation,
                                 "quote_hash": self._quote_hash(value)}
        atomic_json(self._path("quotes", quote_id), value)
        return self.quote_summary(value)

    @staticmethod
    def quote_summary(value):
        return {"quote_id": value["quote_id"], "confirmed": bool(value.get("confirmation")),
                "jobs": [{key: job[key] for key in ("id", "anchor", "model", "mode", "duration", "resolution", "ratio", "estimate")} for job in value["jobs"]],
                **{key: value[key] for key in ("total_display_cny", "total_credits", "budget_cny", "reserved_cny_at_quote", "remaining_cny_after_batch")}}

    def _payload(self, job):
        content = [{"type": "text", "text": job["prompt"]}]
        for kind, field in (("images", "image_url"), ("videos", "video_url"), ("audios", "audio_url")):
            for asset in job[kind]:
                if "path" in asset:
                    try:
                        data = Path(asset["path"]).read_bytes()
                    except OSError:
                        raise BrollError("批准的素材已丢失，须重新准备和确认") from None
                    if hashlib.sha256(data).hexdigest() != asset["sha256"]:
                        raise BrollError("素材在报价后已改变，须重新列价确认")
                    url = "data:" + asset["mime"] + ";base64," + base64.b64encode(data).decode("ascii")
                else:
                    url = asset["url"]
                content.append({"type": field, field: {"url": url}, "role": asset["role"]})
        return {"model": job["model"], "content": content, "resolution": job["resolution"],
                "duration": job["duration"], "ratio": job["ratio"]}

    def _save(self, record):
        record["updated_at"] = now()
        atomic_json(self._path("attempts", record["attempt_id"]), record)

    def _load(self, attempt_id):
        record = read_json(self._path("attempts", attempt_id))
        if record.get("base_url") != self.base_url:
            raise BrollError("原任务接口不同，不能改平台恢复")
        if record.get("state") == "submitting" and not record.get("task_id"):
            record.update(state="submission_unknown", last_error="遗留即将提交记录且没有远端编号；禁止重投，请人工核实")
            self._save(record)
        return record

    def submit(self, quote_id, job_id):
        value = self._quote(quote_id)
        confirmation = value.get("confirmation")
        if not confirmation or confirmation.get("quote_hash") != self._quote_hash(value):
            raise BrollError("没有用户确认过且未改变的报价，拒绝提交")
        job = next((row for row in value["jobs"] if row["id"] == job_id), None)
        if job is None:
            raise BrollError("镜头不在已确认报价内")
        attempt_id = check_name(quote_id + "--" + job_id)
        path = self._path("attempts", attempt_id)
        if path.exists():
            old = self._load(attempt_id)
            raise BrollError("该尝试已提交或提交结果不明；查看原记录，再用原编号查询或下载")
        previous = self._all_attempts()
        matching = [row for row in previous if row.get("request_hash") == job["request_hash"]]
        if any(not row.get("task_id") and row["state"] in ("submission_unknown", "submitting") for row in matching):
            raise BrollError("相同请求有提交结果不明的尝试，先人工核实，禁止借其他尝试重投")
        redo = job.get("redo_of")
        if redo:
            old = self._load(redo)
            if old["request"]["anchor"] != job["anchor"] or old["job_id"] != job["id"]:
                raise BrollError("重做来源不属于该镜头，不能用其他任务绕过恢复规则")
            if not old.get("task_id") and old["state"] in ("submission_unknown", "submitting"):
                raise BrollError("原尝试结果不明，先人工核实；新报价也不能作为盲目重投许可")
        if matching and not redo:
            raise BrollError("相同请求已有尝试；恢复原任务，确需重做则带 redo_of 重新报价确认")
        if job["estimate"] != self._cost(job):
            raise BrollError("报价计费项与请求不一致，拒绝提交")
        amount, reserved, budget = number(job["estimate"]["display_cny"]), self._reserved(), number(value["budget_cny"])
        if amount + reserved > budget:
            raise BrollError("剩余预算不足，拒绝提交")
        key = self._key()
        payload = self._payload(job)
        if self._has_secret(job) or self._has_secret(payload) or self._has_secret(confirmation):
            raise BrollError("请求或确认文字包含凭据，拒绝落盘和提交")
        record = {"attempt_id": attempt_id, "quote_id": quote_id, "job_id": job_id,
                  "created_at": now(), "base_url": self.base_url, "request_hash": job["request_hash"],
                  "request": job, "payload": payload, "estimate": job["estimate"], "state": "submitting",
                  "task_id": None, "remaining_cny_after_submit": fmt(budget - reserved - amount),
                  "actual_fee": {"status": "实际费用未核实", "credits": None, "cny": None},
                  "checks": {"container": "未检查", "decode": "未检查", "continuous_watch": "未看完", "audio_listen": "未听审", "user_acceptance": "未确认"}}
        # 落盘成功才发送；崩溃留下本记录时绝不自动重投。
        atomic_json(path, record, exclusive=True)
        print("本次提交前报价：" + canonical({"model": job["model"], "mode": job["mode"], "duration": job["duration"],
              "resolution": job["resolution"], "estimate": job["estimate"], "remaining_cny_after_submit": record["remaining_cny_after_submit"]}), flush=True)
        try:
            status, response = self._json_request(self.provider["api"]["submit_path"], payload)
        except BrollError:
            record.update(state="submission_unknown", last_error="提交后未取得可靠编号，提交结果不明；不自动重交，请核实原请求")
            self._save(record)
            return self.public_record(record)
        task_id = response.get("task_id")
        if isinstance(task_id, (str, int)) and not isinstance(task_id, bool):
            task_id = str(task_id)
            if NAME.fullmatch(task_id) and key not in task_id and not SECRET_PATTERN.search(task_id):
                record.update(task_id=task_id, state="queued")
                self._save(record)  # 编号先落盘，后续处理故障也能按原编号恢复。
        if self._has_secret(response) or api_error(response) or status >= 400:
            record.update(state="failed", last_error="接口返回错误或含敏感内容；未记为生成成功，未保存原始响应")
        elif not record["task_id"]:
            record.update(state="submission_unknown", last_error="接口未返回可靠任务编号，提交结果不明；禁止重新提交")
        self._save(record)
        return self.public_record(record)

    def query(self, attempt_id):
        record = self._load(attempt_id)
        task_id = record.get("task_id")
        if not task_id:
            raise BrollError("没有远端任务编号，无法可靠查询；请到平台核实，不得重投")
        try:
            status, response = self._json_request(self.provider["api"]["query_path"].format(task_id=url_quote(task_id, safe="")))
        except BrollError:
            record["last_error"] = "查询暂时失败；只能继续查原编号，不重新提交"
            self._save(record)
            return self.public_record(record)
        task = response.get("task")
        if self._has_secret(response) or status >= 400 or api_error(response) or not isinstance(task, dict) or str(task.get("id")) != task_id:
            record["last_error"] = "查询响应错误、编号不匹配或含敏感内容；没有确认生成成功"
            self._save(record)
            return self.public_record(record)
        if api_error(task) or (isinstance(task.get("content"), dict) and api_error(task["content"])):
            record.update(state="failed", last_error="原任务包含错误，不能按完成状态记成功")
        elif task.get("status") in ("failed", "cancelled"):
            record.update(state="failed", last_error="原任务失败或取消；扣费与退款未知")
        elif task.get("status") == "succeeded":
            content = task.get("content")
            url = content.get("url") if isinstance(content, dict) else None
            if not isinstance(url, str) or not url:
                record["last_error"] = "原任务完成但缺少结果地址；仅重查原编号"
            else:
                try:
                    self._result_url(url)
                except BrollError:
                    record["last_error"] = "结果地址不安全或不支持；仅重查原编号"
                else:
                    if record["state"] != "downloaded":
                        record["state"] = "generated"
                    record.update(result_url=url, remote_requested_duration=task.get("duration"), last_error=None)
        elif task.get("status") in ("queued", "running"):
            if record["state"] not in ("generated", "downloaded", "download_failed"):
                record["state"] = task["status"]
            record["last_error"] = None
        else:
            record["last_error"] = "未知远端状态；停止自动判断，只查询原任务"
        self._save(record)
        return self.public_record(record)

    def _result_url(self, url):
        guard_text(url)
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise BrollError("结果地址含凭据")
        if self.test_server:
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise BrollError("假服务结果只能来自本机 HTTP")
        elif parsed.scheme != "https" or not parsed.hostname:
            raise BrollError("结果地址须为 HTTPS")

    def download(self, attempt_id):
        record = self._load(attempt_id)
        if record["state"] not in ("generated", "download_failed", "downloaded") or not record.get("result_url"):
            raise BrollError("尚未查询到可下载结果；先查询原任务")
        output = self.root / "results" / (check_name(attempt_id) + ".mp4")
        if self.root.resolve() not in output.resolve().parents:
            raise BrollError("下载输出不能通过符号链接离开项目记录目录")
        if record["state"] == "downloaded" and output.is_file():
            if hashlib.sha256(output.read_bytes()).hexdigest() == record.get("media", {}).get("sha256"):
                return self.public_record(record)
            raise BrollError("本地结果与记录不同；保留现有文件，先核实改动，不自动覆盖")
        try:
            # 可选本地精确防泄漏比对；未配置 Key 也能下载，绝不用于下载请求鉴权。
            self._key(required=False)
            self._result_url(record["result_url"])
            # 独立请求，不携带生成鉴权；本轮保守拒绝全部重定向。
            request = Request(record["result_url"], headers={"Accept": "video/mp4", "User-Agent": "agent-broll/1"})
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.code != 200 or "text/html" in response.headers.get("Content-Type", "").lower():
                    raise BrollError("下载返回错误或网页")
                data = response.read(MAX_DOWNLOAD + 1)
                length = response.headers.get("Content-Length")
                if len(data) > MAX_DOWNLOAD or (length and int(length) != len(data)):
                    raise BrollError("下载不完整或超过执行器保护上限")
            if (self._secret and self._secret.encode() in data) or SECRET_PATTERN.search(data.decode("latin1")):
                raise BrollError("下载含疑似凭据，不落盘")
            info = mp4_info(data)
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix=".download-", dir=output.parent)
            temporary = Path(name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
            record.update(state="downloaded", local_file=str(output.relative_to(self.project)), media=info, last_error=None)
            record["checks"]["container"] = "通过；仅容器和视频轨检查，不等于解码或完整观看"
        except (BrollError, HTTPError, HTTPException, URLError, OSError, ValueError, TimeoutError):
            record.update(state="download_failed", last_error="下载或 MP4 容器检查失败；只重试下载，必要时重查原编号刷新地址")
        self._save(record)
        return self.public_record(record)

    @staticmethod
    def public_record(record):
        return {key: record[key] for key in ("attempt_id", "quote_id", "job_id", "state", "task_id", "estimate", "actual_fee", "checks", "remaining_cny_after_submit") if key in record} | {
            "anchor": record["request"]["anchor"], **{key: record[key] for key in ("local_file", "media", "last_error") if key in record}}

    def records(self, attempt_id=None):
        if attempt_id:
            return self.public_record(self._load(attempt_id))
        return [self.public_record(self._load(row["attempt_id"])) for row in self._all_attempts()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="用户项目目录；不能是 skill 安装目录")
    parser.add_argument("--key-env", default="METASO_API_KEY", help="仅指定变量名，不提供 Key 值")
    parser.add_argument("--key-file", help="用户指定的外部凭据文件；工具只读取，不创建或回写")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--test-server", help="仅本机 http://127.0.0.1:端口；测试不会选用官方地址")
    commands = parser.add_subparsers(dest="command", required=True)
    estimate = commands.add_parser("estimate", help="本地估价，生成可确认的整批报价")
    estimate.add_argument("--request", action="append", required=True, help="单请求 JSON，可重复指定组成批次")
    estimate.add_argument("--budget-cny", required=True, help="本项目人民币展示价预算总额")
    confirm = commands.add_parser("confirm", help="只有用户确认本批逐条报价后才使用")
    confirm.add_argument("--quote", required=True)
    confirm.add_argument("--user-confirmation", required=True, help="对本报价及未知计费项的用户确认原意和消息来源，不含凭据")
    submit = commands.add_parser("submit", help="新尝试最多发一次创建请求")
    submit.add_argument("--quote", required=True)
    submit.add_argument("--job", required=True)
    for command in ("query", "download"):
        entry = commands.add_parser(command, help="只恢复原任务，不创建生成请求")
        entry.add_argument("--attempt", required=True)
    records = commands.add_parser("records", help="脱敏查看记录，不显示提示词、素材或结果 URL")
    records.add_argument("--attempt")
    args = parser.parse_args(argv)
    try:
        client = Client(args.project, base_url=args.test_server, test_server=bool(args.test_server),
                        key_env=args.key_env, key_file=args.key_file, timeout=args.timeout)
        if args.command == "estimate":
            value = client.estimate([read_json(path) for path in args.request], args.budget_cny)
        elif args.command == "confirm":
            value = client.confirm(args.quote, args.user_confirmation)
        elif args.command == "submit":
            value = client.submit(args.quote, args.job)
        elif args.command == "query":
            value = client.query(args.attempt)
        elif args.command == "download":
            value = client.download(args.attempt)
        else:
            value = client.records(args.attempt)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        if isinstance(value, dict) and (value.get("state") in ("submission_unknown", "failed", "download_failed") or value.get("last_error")):
            return 2
        return 0
    except BrollError as error:
        print("停止：" + str(error), file=sys.stderr)
        return 2
    except (OSError, TypeError, KeyError, ValueError):
        print("停止：本地文件或记录结构异常；未展示原始异常，请先检查记录，禁止盲目重投", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
