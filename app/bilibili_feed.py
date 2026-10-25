"""Read-only Bilibili discovery and an opt-in, persistent per-group feed."""
import asyncio
import html
import json
import math
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

BV = re.compile(r"(?<![A-Za-z0-9])(BV[0-9A-Za-z]{10})(?![A-Za-z0-9])")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def subtitle_timeline(rows, limit=12000):
    """Preserve source times; never turn descriptions or summaries into dialogue."""
    lines, used, truncated = [], 0, False
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get('content') or '').strip()
        if not text:
            continue
        try:
            start, end = float(row['from']), float(row['to'])
            valid = math.isfinite(start) and math.isfinite(end) and 0 <= start <= end
        except (TypeError, ValueError, KeyError):
            valid = False
        line = ('[' + str(round(start, 2)) + '–' + str(round(end, 2)) + '秒] ' if valid else '[时间未知] ') + text
        if used + len(line) + 1 > limit:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    return '\n'.join(lines), truncated


def failure_label(exc):
    status = getattr(exc, "status", None)
    code = getattr(exc, "business_code", None)
    return (type(exc).__name__ + (f", HTTP {status}" if isinstance(status, int) and status else "")
            + (f", 业务码 {code}" if isinstance(code, int) else ""))


async def bounded_json(response, limit=2_000_000):
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError("B站响应过大")
    return json.loads(body)


def settings(config):
    raw = config.get("bilibili", {}) or {}
    return {"enabled": raw.get("enabled", True),
            "interval_seconds": max(600, int(raw.get("interval_seconds", 14400))),
            "daily_limit": max(0, min(24, int(raw.get("daily_limit", 3)))),
            "quiet_start": int(raw.get("quiet_start", 23)) % 24,
            "quiet_end": int(raw.get("quiet_end", 8)) % 24}


def allowed(group, config):
    groups = config.get("deepseek", {}).get("trigger_groups", [])
    return (settings(config)["enabled"] and config.get("deepseek", {}).get("enabled", False)
            and (not groups or str(group) in set(map(str, groups))))


def topics_from(text):
    return list(dict.fromkeys(t.strip()[:60] for t in re.split(r"[,，、;；\n]", text) if t.strip()))[:8]


class BilibiliReader:
    """Uses bilibili-api-python lazily, so a missing extra cannot stop QQ startup."""
    def __init__(self, account=None, cache_dir=None):
        from bilibili_account import BilibiliAccount
        self.account = account or BilibiliAccount(Path(__file__).parent)
        self.cache_dir = Path(cache_dir or Path(__file__).parent / "resources" / "bilibili_tmp")
        self.lock = asyncio.Lock()
        self.audio_enabled = lambda: False

    def api(self):
        from bilibili_api import Credential, video, search, select_client, request_settings
        select_client("aiohttp")
        # Like the direct-request DSH implementations, do not make the optional
        # device activation service a prerequisite for reading a public video.
        request_settings.set_enable_auto_buvid(False)
        values = self.account.cookies()
        credential = Credential(sessdata=values.get("SESSDATA"), bili_jct=values.get("bili_jct"),
                                buvid3=values.get("buvid3"), dedeuserid=values.get("DedeUserID"))
        return video, search, credential

    async def search(self, keyword):
        async with self.lock:
            # The public search endpoint works without the library's additional
            # cookie-bootstrap requests, which may fail independently.
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True,
                    headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}) as session:
                async with session.get("https://api.bilibili.com/x/web-interface/search/type",
                        params={"keyword": keyword[:100], "search_type": "video", "order": "pubdate", "page": 1},
                        allow_redirects=False) as response:
                    response.raise_for_status()
                    payload = await bounded_json(response)
                    if payload.get("code") != 0:
                        raise ValueError("B站搜索未成功")
                    result = payload.get("data") or {}
            return [r["bvid"] for r in result.get("result", [])
                    if BV.fullmatch(str(r.get("bvid", "")))][:10]

    async def resolve(self, text):
        found = BV.search(text)
        if found:
            return found.group(1)
        short = re.search(r"https?://b23\.tv/[A-Za-z0-9]+", text)
        if not short:
            return None
        url = short.group()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                         trust_env=True) as session:
            for _ in range(5):
                part = urlsplit(url)
                if part.scheme not in ("https", "http") or part.hostname not in (
                        "b23.tv", "www.bilibili.com", "m.bilibili.com", "bilibili.com"):
                    raise ValueError("非 B 站跳转地址")
                async with session.get(url, allow_redirects=False) as response:
                    if response.status not in (301, 302, 303, 307, 308):
                        break
                    from urllib.parse import urljoin
                    url = urljoin(url, response.headers.get("Location", ""))
                    found = BV.search(url)
                    if found and urlsplit(url).hostname in ("www.bilibili.com", "m.bilibili.com", "bilibili.com"):
                        return found.group(1)
        raise ValueError("短链接未解析出视频 BV 号")

    async def read(self, bvid):
        if not BV.fullmatch(bvid):
            raise ValueError("无效 BV 号")
        async with self.lock:
            return await asyncio.wait_for(self._read(bvid), 150)

    async def _read(self, bvid):
        video, _, credential = self.api()
        item = video.Video(bvid=bvid, credential=credential)
        metadata_error = None
        for attempt in range(2):
            try:
                info, _ = await self.account.request("api.bilibili.com", "/x/web-interface/view",
                    params={"bvid": bvid}, cookies=self.account.cookies())
                break
            except Exception as exc:
                if attempt == 0 and getattr(exc, "status", None) == 412:
                    # One bounded backoff, inspired by DSH's extractor; this
                    # does not claim to defeat risk control or login gates.
                    await asyncio.sleep(2)
                    continue
                metadata_error = failure_label(exc)
                parts, _ = await self.account.request("api.bilibili.com", "/x/player/pagelist", params={"bvid": bvid})
                if not isinstance(parts, list) or not parts:
                    raise
                info = {"title": "分P：" + str(parts[0].get("part", "")), "pages": parts,
                        "duration": parts[0].get("duration")}
                break
        result = {"bvid": bvid, "url": f"https://www.bilibili.com/video/{bvid}",
                  "title": html.unescape(str(info.get("title", "")))[:300],
                  "author": str(info.get("owner", {}).get("name", ""))[:100],
                  "description": str(info.get("desc", ""))[:2500],
                  "duration_seconds": info.get("duration"), "scope": "仅第一分P的字幕；未读取画面或音轨",
                  "subtitle": "", "subtitle_status": "未提供可读取字幕",
                  "metadata_loaded": metadata_error is None, "metadata_error": metadata_error,
                  "aid": item.get_aid(), "transcript_loaded": False, "frames_loaded": False,
                  "frames": [], "frame_status": "未抽帧", "need_login_subtitle": False}
        pages = info.get("pages", [])
        cid = pages[0]["cid"] if pages else await item.get_cid(page_index=0)
        result["cid"] = cid
        try:
            if self.account.cookies().get("SESSDATA"):
                player = await item.get_player_info(cid=cid)
            else:
                player, _ = await self.account.request("api.bilibili.com", "/x/player/v2",
                                                      params={"bvid": bvid, "cid": cid})
            result["need_login_subtitle"] = bool(player.get("need_login_subtitle"))
            subs = (player.get("subtitle") or {}).get("subtitles", [])
            if not subs and result["need_login_subtitle"]:
                result["subtitle_status"] = "接口明确要求登录后读取字幕；尚未读取正文"
            subs = sorted(subs, key=lambda s: {"zh-CN": 0, "zh-Hans": 0, "ai-zh": 1,
                                               "zh-TW": 2, "en": 3, "ai-en": 4}.get(s.get("lan"), 5))
            if subs:
                url = subs[0].get("subtitle_url", "")
                if url.startswith("//"):
                    url = "https:" + url
                host = urlsplit(url).hostname or ""
                if urlsplit(url).scheme != "https" or not any(
                        host == domain or host.endswith("." + domain)
                        for domain in ("hdslb.com", "bilibili.com", "bilivideo.com")):
                    raise ValueError("非受信任字幕地址")
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                                 trust_env=True) as session:
                    async with session.get(url, allow_redirects=False) as response:
                        response.raise_for_status()
                        payload = await bounded_json(response)
                text, truncated = subtitle_timeline(payload.get('body', []))
                result["subtitle"] = text
                result["transcript_loaded"] = bool(text.strip())
                result["subtitle_status"] = (f"已读取 {subs[0].get('lan', '未知语言')} 字幕"
                                             + '（含原始字幕时间定位）' + ("（12000字符上限，已截断）" if truncated else ""))
        except Exception as exc:
            result["subtitle_status"] = f"字幕读取不可用（{failure_label(exc)}），仅有标题和简介"
        try:
            from bilibili_frames import capture_frames, capture_media
            if self.audio_enabled():
                captured = await capture_media(self.account, bvid, cid,
                    pages[0].get("duration") if pages else info.get("duration"), self.cache_dir, include_audio=True)
                frames = captured.get('frames', [])
                status = '已抽取真实画面和可用音轨；尚未提交模型'
                result.update(_audio_chunks=captured.get('audio_chunks', []), audio_status=captured.get('audio_status', '未提取音轨'))
            else:
                frames, status = await capture_frames(self.account, bvid, cid,
                    pages[0].get("duration") if pages else info.get("duration"), self.cache_dir)
            result.update(frames=frames, frames_loaded=bool(frames), frame_status=status)
        except Exception as exc:
            result["frame_status"] = "画面读取失败（" + failure_label(exc) + "）"
        result["scope"] = "仅第一分P的可用字幕及最多40张实际采样帧；视觉模型分批查看，抽到不等于已看；音轨是否听取以实际听觉结果为准，未完整观看视频"
        return result


class BilibiliFeed:
    def __init__(self, path, reader=None):
        self.path = Path(path)
        from bilibili_account import BilibiliAccount
        self.account = BilibiliAccount(self.path.parent)
        self.reader = reader or BilibiliReader(self.account, self.path.parent / "resources" / "bilibili_tmp")
        self.busy = False
        if self.path.exists():
            # Broken state must fail closed rather than silently re-enable old jobs.
            self.groups = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.groups, dict):
                raise ValueError("B站状态文件格式错误")
        else:
            self.groups = {}

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.groups, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def configure(self, group, action, topics=None):
        key = str(group)
        old = self.groups.get(key, {})
        row = dict(old)
        if action in ("开启", "兴趣"):
            values = topics or old.get("topics", [])
            if not values:
                raise ValueError("请附上兴趣词，例如：刷B站 开启 猫猫，独立游戏")
            row["topics"] = values
        if action in ("开启", "关闭"):
            row["enabled"] = action == "开启"
            if action == "开启":
                row["interactions"] = True
        if action in ("互动开启", "互动关闭"):
            row["interactions"] = action == "互动开启"
        row["revision"] = int(old.get("revision", 0)) + 1
        # Keep the previous cooldown and quota when toggling to avoid reset abuse.
        row.setdefault("next_at", time.time())
        self.groups[key] = row
        try:
            self.save()
        except Exception:
            if old:
                self.groups[key] = old
            else:
                self.groups.pop(key, None)
            raise

    def status(self, group, config):
        row = self.groups.get(str(group), {})
        cfg = settings(config)
        return (f"本群主动刷B站：{'开启' if row.get('enabled') and allowed(group, config) else '关闭'}；"
                f"兴趣：{'、'.join(row.get('topics', [])) or '未设置'}；"
                f"间隔 {cfg['interval_seconds'] // 60} 分钟，每天最多 {cfg['daily_limit']} 轮（不分享也计次）。"
                f"安静时段 {cfg['quiet_start']:02d}:00—{cfg['quiet_end']:02d}:00。"
                f"最近结果：{row.get('last_result', '尚未运行')}。"
                f"自主点赞评论：{'开启' if row.get('interactions') else '关闭'}；"
                f"最近互动：{row.get('last_interaction', '尚未执行')}。"
                "管理员：刷B站 开启 猫猫，独立游戏 / 刷B站 关闭 / 刷B站 兴趣 科普。")

    async def tick(self, load_config, now_fn, connected, generate, send):
        if self.busy or not connected():
            return
        self.busy = True
        try:
            for group in list(self.groups):
                config = load_config()
                cfg = settings(config)
                now = now_fn(config)
                row = self.groups[group]
                start, end = cfg["quiet_start"], cfg["quiet_end"]
                quiet = ((start <= now.hour < end) if start < end
                         else (now.hour >= start or now.hour < end) if start > end else False)
                if (not row.get("enabled") or not allowed(group, config) or quiet
                        or not row.get("topics") or time.time() < row.get("next_at", 0)):
                    continue
                today = now.date().isoformat()
                used = row.get("used", 0) if row.get("day") == today else 0
                if used >= cfg["daily_limit"]:
                    continue
                revision = row.get("revision", 0)
                # Reserve before any I/O: failed/silent attempts also consume the cap.
                row.update(day=today, used=used + 1, next_at=time.time() + cfg["interval_seconds"],
                           last_result="正在浏览")
                self.save()
                def current():
                    fresh = self.groups.get(group, {})
                    live_config = load_config()
                    live_settings = settings(live_config)
                    hour = now_fn(live_config).hour
                    a, b = live_settings["quiet_start"], live_settings["quiet_end"]
                    is_quiet = ((a <= hour < b) if a < b else
                                (hour >= a or hour < b) if a > b else False)
                    return (fresh.get("enabled") and fresh.get("revision", 0) == revision
                            and allowed(group, live_config) and not is_quiet and connected())
                try:
                    topic = random.choice(row["topics"])
                    candidates = await self.reader.search(topic)
                    if not current():
                        continue
                    bvid = next((b for b in candidates if b not in row.get("seen", [])), None)
                    if not bvid:
                        row["last_result"] = "这轮没有新视频"
                        self.save()
                        continue
                    item = await self.reader.read(bvid)
                    if not current():
                        continue
                    if not ((item.get("transcript_loaded") and item.get("subtitle", "").strip()) or item.get("frames_loaded")):
                        row["last_result"] = "未读到视频正文，本轮不生成或分享感想"
                        self.save()
                        continue
                    generation_config = {**config, "_bilibili_interactions": bool(row.get("interactions")), "_media_group": group}
                    decision = await generate(item, topic, generation_config, True)
                    if not current():
                        continue
                    if decision is None:
                        row["last_result"] = "读取完成但模型分析未完成，本轮未互动"
                        self.save()
                        continue
                    row["seen"] = (row.get("seen", []) + [bvid])[-1000:]
                    self.save()
                    if isinstance(decision, dict):
                        reply = decision.get("share", "")
                        still_current = decision.get("_still_current", lambda: True)
                        if row.get("interactions") and (decision.get("like") is True or decision.get("comment")):
                            row["last_interaction"] = await self.account.interact(bvid,
                                like=decision.get("like") is True, comment=decision.get("comment", ""),
                                active=lambda: current() and still_current())
                            self.save()
                        if not still_current():
                            continue
                    else:
                        reply = decision
                    if not current():
                        continue
                    if reply:
                        # Canonical link comes from our adapter, not from model output.
                        ok = await send(int(group), reply + "\n" + item["url"])
                        row["last_result"] = "已分享" if ok else "消息发送未成功"
                    else:
                        row["last_result"] = "已浏览，这轮选择不分享"
                except Exception as exc:
                    row["last_result"] = f"本轮未完成（{failure_label(exc)}）"
                self.save()
        finally:
            self.busy = False
