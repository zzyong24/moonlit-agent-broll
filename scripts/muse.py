#!/usr/bin/env python3
"""本机 muse2api（Muse muse-video）的记录、确认、提交、轮询和下载工具。

复用 h3.py 的确认关卡、落盘、防重投、防泄 Key 和 MP4 检查，只替换协议差异：
扁平请求体（prompt/duration/aspect_ratio/image）、扁平查询结果（status/url）、
本机 http 结果地址、按免费周额度而非按秒计价、提交前实时查额度。
Key 只从环境变量读取；--env-file 只在进程内载入指定变量，不打印、不进命令行。
"""

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote as url_quote, urljoin, urlparse
from urllib.request import ProxyHandler, build_opener

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import h3  # noqa: E402
from h3 import BrollError, check_name, fingerprint, fmt, number, now, read_json  # noqa: E402

h3.PROVIDER_PATH = h3.SKILL_ROOT / "providers" / "muse2api.json"
DEFAULT_ENV_FILE = ""  # 公开副本不内置本机路径；默认只读环境变量，可用 --env-file 指定用户自己的文件
DEFAULT_KEY_ENV = "MUSE2API_KEY"
DEFAULT_RATIO = "16:9"
DEFAULT_FIRST_FRAME_SIZE = "1536x1024"


def load_env_var(path, name):
    """只把 .env 里指定的一个变量载入本进程环境；已存在则不覆盖。不回显任何值。"""
    if os.environ.get(name, "").strip() or not path:
        return
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise BrollError("无法读取指定 .env 文件") from None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ[name] = value
            return


class MuseClient(h3.Client):
    def __init__(self, project, base_url=None, test_server=False, key_env=DEFAULT_KEY_ENV,
                 key_file=None, timeout=30):
        super().__init__(project, base_url=base_url, test_server=test_server,
                         key_env=key_env, key_file=key_file, timeout=timeout)
        if not test_server:
            missing = [name for name, value in (("api.base_url", self.provider["api"].get("base_url")),
                                                ("api.quota_account_id", self.provider["api"].get("quota_account_id")))
                       if not (value or "").strip()]
            if missing:
                raise BrollError("请在 providers/muse2api.json 填写 %s 后再使用；公开副本不内置服务地址或账户标识。本地假服务测试请用 --test-server" % "、".join(missing))
        # 本机服务不走系统代理；仍拒绝重定向。
        self.opener = build_opener(ProxyHandler({}), h3.NoRedirect())
        self.origin = "{0.scheme}://{0.netloc}".format(urlparse(self.base_url))

    # ---------- 请求冻结与估价 ----------
    def _freeze(self, request):
        allowed = {"id", "anchor", "model", "mode", "prompt", "duration", "resolution", "ratio",
                   "images", "prompt_optimization", "redo_of"}
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
        low, high = cap["duration_seconds"]["executor_minimum"], cap["duration_seconds"]["executor_maximum"]
        if type(job["duration"]) is not int or not low <= job["duration"] <= high:
            raise BrollError("时长须为 %d–%d 整数秒" % (low, high))
        if job["ratio"] is None:
            job["ratio"] = DEFAULT_RATIO  # 月明的视频都是横屏
        if job["resolution"] is None:
            job["resolution"] = cap["resolutions"][0]
        if job["resolution"] not in cap["resolutions"] or job["ratio"] not in cap["ratios"]:
            raise BrollError("分辨率或画面比例不受支持")
        if request.get("prompt_optimization", False) is not False:
            raise BrollError("不支持提示词优化")
        job["prompt_optimization"] = False
        items = request.get("images", [])
        if not isinstance(items, list) or len(items) > cap["reference_limits"]["images"]:
            raise BrollError("参考图数量超出范围")
        job["images"] = [self._asset(item, "images") for item in items]
        for asset in job["images"]:
            if "url" in asset:
                raise BrollError("muse2api 接入只接受本地图片 path")
        job["videos"], job["audios"] = [], []
        roles = [item["role"] for item in job["images"]]
        if job["mode"] == "text_to_video":
            valid = not roles
        else:
            valid = roles[:1] == ["first_frame"] and roles.count("first_frame") == 1
        if not valid:
            raise BrollError("模式与参考图用途不匹配：first_frame 模式须且仅有第一张为 first_frame")
        if request.get("redo_of") is not None:
            job["redo_of"] = check_name(request["redo_of"])
        job["request_hash"] = fingerprint({key: value for key, value in job.items() if key not in ("id", "redo_of")})
        return job

    def _cost(self, job):
        pricing = self.provider["pricing"]
        return {"items": [{"item": "Muse 免费档视频 1 段", "quantity": "1", "unit_display_cny": "0",
                           "display_cny": "0", "credits": "0"}],
                "credits": "0", "display_cny": "0", "reference_exchange_cny": "0",
                "quota_note": pricing["basis"], "actual_fee_status": "不扣现金；额度占用以提交前后额度查询为准",
                "checked_on": self.provider["checked_on"], "unknowns": self.provider["unknowns"]}

    # ---------- 协议 ----------
    def _payload(self, job):
        payload = {"model": job["model"], "prompt": job["prompt"], "duration": job["duration"],
                   "aspect_ratio": job["ratio"]}
        images = []
        for asset in job["images"]:
            try:
                data = Path(asset["path"]).read_bytes()
            except OSError:
                raise BrollError("批准的素材已丢失，须重新准备和确认") from None
            if h3.hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise BrollError("素材在确认后已改变，须重新确认")
            images.append("data:" + asset["mime"] + ";base64," + base64.b64encode(data).decode("ascii"))
        if len(images) == 1:
            payload["image"] = images[0]
        elif images:
            payload["images"] = images
        return payload

    def quota(self):
        api = self.provider["api"]
        # 额度接口在服务根路径（/admin），不在 /v1 下。
        base, self.base_url = self.base_url, self.origin
        try:
            status, value = self._json_request(api["quota_path"].format(account_id=url_quote(api["quota_account_id"], safe="")), {})
        finally:
            self.base_url = base
        if status >= 400 or not value.get("found"):
            raise BrollError("额度查询失败（HTTP %s）；未提交" % status)
        return {key: value.get(key) for key in ("plan", "weekly_used_pct", "weekly_reset", "extra_left", "checked_at")}

    def submit(self, quote_id, job_id, check_quota=True):
        quota_before = None
        if check_quota:
            quota_before = self.quota()
            print("提交前额度：" + json.dumps(quota_before, ensure_ascii=False), flush=True)
            limit = self.provider["pricing"]["max_weekly_used_pct_before_submit"]
            used = quota_before.get("weekly_used_pct")
            if not isinstance(used, (int, float)) or used >= limit:
                raise BrollError("周额度已用 %s%%（上限 %s%%）或读不到，拒绝提交" % (used, limit))
        result = super().submit(quote_id, job_id)
        if quota_before is not None:
            record = self._load(result["attempt_id"])
            record["quota_before"] = quota_before
            self._save(record)
        return result

    def query(self, attempt_id):
        record = self._load(attempt_id)
        task_id = record.get("task_id")
        if not task_id:
            raise BrollError("没有任务编号，无法可靠查询；请核实，不得重投")
        try:
            status, task = self._json_request(self.provider["api"]["query_path"].format(task_id=url_quote(task_id, safe="")))
        except BrollError:
            record["last_error"] = "查询暂时失败；只能继续查原编号，不重新提交"
            self._save(record)
            return self.public_record(record)
        mapped = self.provider["api"]["status_map"].get(task.get("status"))
        if self._has_secret(task) or status >= 400 or str(task.get("id")) != task_id:
            record["last_error"] = "查询响应错误、编号不匹配或含敏感内容；没有确认生成成功"
        elif mapped == "failed":
            record.update(state="failed", last_error="原任务失败：" + str(task.get("error") or "未知")[:200])
        elif mapped == "succeeded":
            url = task.get("url") or (task.get("video") or {}).get("url")
            if not isinstance(url, str) or not url:
                record["last_error"] = "原任务完成但缺少结果地址；仅重查原编号"
            else:
                url = urljoin(self.origin + "/", url)
                try:
                    self._result_url(url)
                except BrollError:
                    record["last_error"] = "结果地址不在本机 muse2api；拒绝下载"
                else:
                    if record["state"] != "downloaded":
                        record["state"] = "generated"
                    record.update(result_url=url, remote_elapsed=task.get("elapsed"), last_error=None)
        elif mapped in ("queued", "running"):
            if record["state"] not in ("generated", "downloaded", "download_failed"):
                record["state"] = mapped
            record["last_error"] = None
        else:
            record["last_error"] = "未知远端状态；只查询原任务"
        self._save(record)
        return self.public_record(record)

    def _result_url(self, url):
        h3.guard_text(url)
        parsed = urlparse(url)
        if parsed.username or parsed.password or "{0.scheme}://{0.netloc}".format(parsed) != self.origin:
            raise BrollError("结果地址须来自本机 muse2api")

    def wait(self, attempt_id, interval=15, max_wait=1800):
        deadline = time.time() + max_wait
        while True:
            record = self.query(attempt_id)
            if record["state"] in ("generated", "failed", "downloaded"):
                break
            if time.time() > deadline:
                return record
            time.sleep(interval)
        if record["state"] == "generated":
            record = self.download(attempt_id)
        return record

    def after_quota(self, attempt_id):
        record = self._load(attempt_id)
        record["quota_after"] = self.quota()
        self._save(record)
        return {"quota_before": record.get("quota_before"), "quota_after": record["quota_after"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="用户项目目录；不能是 skill 安装目录")
    parser.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="仅指定变量名，不提供 Key 值")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="可选；变量未设置时从此 .env 进程内载入。默认空，只读环境变量；传空串同样禁用")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--test-server", help="仅本机 http://127.0.0.1:端口 假服务")
    commands = parser.add_subparsers(dest="command", required=True)
    estimate = commands.add_parser("estimate", help="冻结请求并生成待确认清单（免费档金额为 0）")
    estimate.add_argument("--request", action="append", required=True)
    estimate.add_argument("--budget-cny", default="0")
    confirm = commands.add_parser("confirm", help="记录用户对本批的确认")
    confirm.add_argument("--quote", required=True)
    confirm.add_argument("--user-confirmation", required=True)
    submit = commands.add_parser("submit", help="查额度后最多发一次创建请求")
    submit.add_argument("--quote", required=True)
    submit.add_argument("--job", required=True)
    for command in ("query", "download", "wait", "quota-after"):
        entry = commands.add_parser(command, help="只按原任务恢复，不创建新生成")
        entry.add_argument("--attempt", required=True)
        if command == "wait":
            entry.add_argument("--interval", type=float, default=15)
            entry.add_argument("--max-wait", type=float, default=1800)
    commands.add_parser("quota", help="实时查询免费档额度")
    records = commands.add_parser("records")
    records.add_argument("--attempt")
    args = parser.parse_args(argv)
    try:
        load_env_var(args.env_file, args.key_env)
        client = MuseClient(args.project, base_url=args.test_server, test_server=bool(args.test_server),
                            key_env=args.key_env, timeout=args.timeout)
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
        elif args.command == "wait":
            value = client.wait(args.attempt, args.interval, args.max_wait)
        elif args.command == "quota-after":
            value = client.after_quota(args.attempt)
        elif args.command == "quota":
            value = client.quota()
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
        print("停止：本地文件或记录结构异常；未展示原始异常，禁止盲目重投", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
