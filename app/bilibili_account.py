"""Private QR login and durable, at-most-once Bilibili interactions."""
import asyncio
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp


class BilibiliApiError(RuntimeError):
    def __init__(self, code):
        self.business_code = code if isinstance(code, int) else None
        super().__init__("B站业务错误")


class BilibiliAccount:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.credentials_path = self.directory / "bilibili_account.json"
        self.journal_path = self.directory / "bilibili_actions.json"
        self.pending = {}
        self.lock = asyncio.Lock()

    @staticmethod
    def atomic_json(path, data, private=False):
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        if private:
            os.chmod(path, 0o600)

    def cookies(self):
        if os.getenv("BILIBILI_SESSDATA"):
            return {key: value for key, value in {
                "SESSDATA": os.getenv("BILIBILI_SESSDATA"), "bili_jct": os.getenv("BILIBILI_BILI_JCT"),
                "buvid3": os.getenv("BILIBILI_BUVID3"), "DedeUserID": os.getenv("BILIBILI_DEDEUSERID")}.items() if value}
        if self.credentials_path.exists():
            values = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            return {key: str(values[key]) for key in ("SESSDATA", "bili_jct", "buvid3", "DedeUserID") if values.get(key)}
        return {}

    async def request(self, host, path, *, params=None, form=None, cookies=None):
        if host not in ("api.bilibili.com", "passport.bilibili.com"):
            raise ValueError("Unexpected API host")
        from bilibili_feed import USER_AGENT, bounded_json
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True,
                cookies=cookies or {}, headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}) as session:
            async with session.request("POST" if form is not None else "GET", "https://" + host + path,
                    params=params, data=form, allow_redirects=False) as response:
                response.raise_for_status()
                body = await bounded_json(response)
                if body.get("code") != 0:
                    # Never include raw platform responses, cookies or redirect URLs in errors.
                    raise BilibiliApiError(body.get("code"))
                return body.get("data") or {}, {key: value.value for key, value in response.cookies.items()}

    async def start_login(self, owner):
        if os.getenv("BILIBILI_SESSDATA"):
            raise ValueError("当前使用环境变量凭据，请先移除它们再扫码更换账号。")
        data, _ = await self.request("passport.bilibili.com", "/x/passport-login/web/qrcode/generate")
        url = data["url"]
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in (
                "passport.bilibili.com", "passport.bilibili.tv", "account.bilibili.com"):
            raise ValueError("登录二维码地址异常")
        self.pending[str(owner)] = {"key": data["qrcode_key"], "expires": time.time() + 180}
        import qrcode
        buffer = io.BytesIO()
        qrcode.make(url).save(buffer, format="PNG")
        return buffer.getvalue()

    async def poll_login(self, owner):
        pending = self.pending.get(str(owner))
        if not pending or pending["expires"] < time.time():
            return "二维码已过期，请重新发送 B站登录。"
        data, cookies = await self.request("passport.bilibili.com", "/x/passport-login/web/qrcode/poll",
                                          params={"qrcode_key": pending["key"]})
        code = data.get("code")
        if code in (86101, 86090):
            return "尚未扫码。" if code == 86101 else "已扫码，请在B站App确认登录后再发送 B站登录确认。"
        if code != 0:
            self.pending.pop(str(owner), None)
            return "二维码未完成登录，请重新发送 B站登录。"
        # Some versions return the cookies in the successful redirect URL.
        query = parse_qs(urlsplit(data.get("url", "")).query)
        for key in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3"):
            if not cookies.get(key) and query.get(key):
                cookies[key] = query[key][0]
        if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
            raise ValueError("登录响应缺少会话或CSRF凭据，未保存。")
        async with self.lock:
            if self.pending.get(str(owner)) is not pending:
                return "登录请求已更新，请重新发送 B站登录。"
            self.atomic_json(self.credentials_path, {key: cookies[key] for key in (
                "SESSDATA", "bili_jct", "DedeUserID", "buvid3") if cookies.get(key)}, private=True)
            self.pending.clear()
        return "B站登录已保存。开启自动刷后，会按群设置自主决定点赞和评论。"

    async def status(self):
        cookies = self.cookies()
        if not cookies.get("SESSDATA"):
            return "B站尚未登录；请管理员私聊bot发送 B站登录。"
        data, _ = await self.request("api.bilibili.com", "/x/web-interface/nav", cookies=cookies)
        return "B站账号已登录；可读取该账号获准访问的字幕和执行互动。" if data.get("isLogin") else "B站会话已失效，请重新登录。"

    async def interact(self, bvid, like=False, comment="", active=lambda: True):
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            raise ValueError("无效BV号")
        if len(comment) > 120 or re.search(r"https?://|\[CQ:|@", comment, re.I):
            raise ValueError("评论须在120字以内，且不含链接或@提醒。")
        async with self.lock:
            cookies = self.cookies()
            if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
                return "未登录或缺少CSRF凭据，未执行互动"
            if not active():
                return "设置已改变，互动已取消"
            account = cookies.get("DedeUserID") or hashlib.sha256(cookies["SESSDATA"].encode()).hexdigest()[:16]
            identity = json.dumps(cookies, sort_keys=True)
            def current():
                return active() and json.dumps(self.cookies(), sort_keys=True) == identity
            journal = json.loads(self.journal_path.read_text(encoding="utf-8")) if self.journal_path.exists() else {}
            from bilibili_api import video
            aid = video.Video(bvid=bvid).get_aid()  # Local ID conversion, no network request.
            results = []
            for action in ("like", "comment"):
                if not (like if action == "like" else comment):
                    continue
                key = f"{account}:{bvid}:{action}"
                if key in journal:
                    results.append("点赞已记录，跳过" if action == "like" else "评论已有提交记录，跳过")
                    continue
                limit = 8 if action == "like" else 3
                recent = sum(1 for name, entry in journal.items() if name.startswith(str(account) + ":")
                             and name.endswith(":" + action) and entry.get("at", 0) >= time.time() - 86400)
                if recent >= limit:
                    results.append(("点赞" if action == "like" else "评论") + "已达账号24小时额度")
                    continue
                if not current():
                    results.append("设置已改变，剩余互动已取消")
                    break
                # Durable reservation comes BEFORE posting. Timeouts are uncertain,
                # never automatically retried, including after a process restart.
                journal[key] = {"state": "pending", "at": time.time()}
                self.atomic_json(self.journal_path, journal)
                form = {"csrf": cookies["bili_jct"]}
                if action == "like":
                    path = "/x/web-interface/archive/like"
                    form.update(aid=str(aid), like="1")
                else:
                    path = "/x/v2/reply/add"
                    form.update(oid=str(aid), type="1", message=comment, plat="1", gaia_source="main_web",
                                statistics=json.dumps({"appId": 100, "platform": 5}))
                try:
                    data, _ = await self.request("api.bilibili.com", path, form=form, cookies=cookies)
                    journal[key].update(state="accepted", reply_id=data.get("rpid") if isinstance(data, dict) else None)
                    results.append("已点赞" if action == "like" else "评论已提交")
                except Exception as exc:
                    if isinstance(exc, BilibiliApiError):
                        journal[key].update(state="rejected", business_code=exc.business_code)
                        results.append(("点赞" if action == "like" else "评论") + "被平台拒绝，业务码 " + str(exc.business_code))
                    else:
                        journal[key].update(state="uncertain", error=type(exc).__name__)
                        results.append(("点赞" if action == "like" else "评论") + "未确认成功，未自动重试")
                self.atomic_json(self.journal_path, journal)
            return "；".join(results) or "这轮选择不互动"
