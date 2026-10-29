"""Low-frequency, opt-in-via-proactive group reading and durable reviewed-item history."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import re
import time

from proactive import ProactiveMode
import math


DEFAULTS = {"enabled": True, "interval_seconds": 7200, "idle_seconds": 1200,
            "probability": 0.35, "daily_limit": 1, "attempt_limit": 2, 'guarantee_checks': 3}


def settings(config):
    cfg = dict(DEFAULTS)
    cfg.update((config.get("paper_reader", {}) or {}).get("auto_read", {}) or {})
    for key, low, high in (("interval_seconds", 600, 86400), ("idle_seconds", 300, 86400),
                           ("daily_limit", 1, 5), ("attempt_limit", 1, 10), ('guarantee_checks', 1, 10)):
        cfg[key] = max(low, min(high, int(cfg[key])))
    cfg["probability"] = max(0, min(1, float(cfg["probability"])))
    return cfg


def source_key(info):
    file_id = str(info.get("file_id") or info.get("id") or "")
    if not file_id:
        return None
    return hashlib.sha256((file_id + ":" + str(info.get("size", info.get("file_size", 0)) or 0)).encode()).hexdigest()


def fingerprints(doc):
    text = "\n".join(unit["text"] for unit in doc["units"])
    text = re.sub(r"\s+", " ", text).strip()
    result = ["text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()]
    raw = doc.get("_content_sha256")
    if isinstance(raw, str) and re.fullmatch(r"[a-f0-9]{64}", raw):
        result.append("file:" + raw)
    return result


class PaperAutonomy:
    def __init__(self, path, log=lambda *args: None, clock=time.time, roll=random.random):
        self.path = Path(path)
        self.log, self.clock, self.roll = log, clock, roll
        self.groups = {}
        self.storage_ok = True
        self.lock = asyncio.Lock()
        if self.path.exists():
            try:
                if self.path.stat().st_size > 4_000_000:
                    raise ValueError("History too large")
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("groups"), dict):
                    raise ValueError("History schema")
                for key, row in data["groups"].items():
                    if not str(key).isdigit() or not isinstance(row, dict):
                        raise ValueError("History group")
                    if not isinstance(row.get("ids", {}), dict) or not isinstance(row.get("reviewed", {}), dict):
                        raise ValueError("History index")
                    if "auto_enabled" in row and not isinstance(row["auto_enabled"], bool):
                        raise ValueError("History switch")
                self.groups = data["groups"]
            except (OSError, ValueError, TypeError, AttributeError):
                self.storage_ok = False
                self.log("ERROR", "阅读记录不可用，已暂停自动阅读；请检查paper_reading_state.json。")

    def _edit(self, edit):
        if not self.storage_ok:
            raise OSError("阅读记录不可用")
        before = deepcopy(self.groups)
        try:
            edit()
            data = json.dumps({"version": 1, "groups": self.groups}, ensure_ascii=False, indent=2)
            if len(data.encode("utf-8")) > 4_000_000:
                raise OSError("阅读记录超出存储上限")
            temp = self.path.with_suffix(".tmp")
            temp.write_text(data, encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(self.path)
        except Exception:
            self.groups = before
            self.storage_ok = False
            raise

    def enabled(self, group_id, config):
        cfg = settings(config)
        paper = config.get("paper_reader", {}) or {}
        allowed = paper.get("groups", config.get("deepseek", {}).get("trigger_groups", [])) or []
        return (self.storage_ok and bool(cfg["enabled"]) and paper.get("enabled", True)
                and (not allowed or str(group_id) in set(map(str, allowed)))
                and self.groups.get(str(group_id), {}).get("auto_enabled", True))

    def set_enabled(self, group_id, enabled):
        self._edit(lambda: self.groups.setdefault(str(group_id), {}).update(auto_enabled=bool(enabled)))

    def seen_source(self, group_id, info):
        key = source_key(info)
        return key is not None and key in self.groups.get(str(group_id), {}).get("ids", {})

    def seen_document(self, group_id, doc):
        stored = self.groups.get(str(group_id), {}).get("reviewed", {})
        return any(fp in stored for fp in fingerprints(doc))

    def unread(self, group_id, candidates):
        return [row for row in candidates if not self.seen_source(group_id, row)]

    def mark_reviewed(self, group_id, info, doc):
        def edit():
            row = self.groups.setdefault(str(group_id), {})
            ids, reviewed = row.setdefault("ids", {}), row.setdefault("reviewed", {})
            title = str(doc.get("title", ""))[:180]
            now = self.clock()
            key = source_key(info) if info else None
            if key:
                ids.pop(key, None)
                ids[key] = {"at": now, "title": title}
            partial = bool(doc.get("truncated") or sum(len(u["text"]) for u in doc["units"]) > 20_000)
            for fp in fingerprints(doc):
                reviewed.pop(fp, None)
                reviewed[fp] = {"at": now, "title": title, "partial": partial}
            while len(ids) > 500:
                ids.pop(next(iter(ids)))
            while len(reviewed) > 1000:
                reviewed.pop(next(iter(reviewed)))
        self._edit(edit)

    @staticmethod
    def _day(row, date):
        day = row.get("daily", {})
        return dict(day) if day.get("date") == date else {"date": date, "attempts": 0, "reviewed": 0}

    def summary(self, group_id, date):
        row = self.groups.get(str(group_id), {})
        return self._day(row, date), len(row.get("ids", {}))

    def _reserve_check(self, group_id, date, attempt, interval=None):
        def edit():
            row = self.groups.setdefault(str(group_id), {})
            row["last_check"] = self.clock()
            row['checks'] = int(row.get('checks', 0)) + 1
            row['misses'] = 0 if attempt else int(row.get('misses', 0)) + 1
            row['last_outcome'] = '已预约挑书尝试' if attempt else '固定序列跳过，保底计数已增加'
            if interval is not None:
                scheduled = row.get('next_check_at', self.clock())
                next_due = scheduled + interval
                if attempt and next_due <= self.clock():
                    # Only one real catch-up attempt, then the next original grid point.
                    next_due += (math.floor((self.clock() - next_due) / interval) + 1) * interval
                row['next_check_at'] = next_due
            if attempt:
                day = self._day(row, date)
                day["attempts"] += 1
                row["daily"] = day
        self._edit(edit)

    def next_check(self, group_id, config):
        row = self.groups.get(str(group_id), {})
        return row.get('next_check_at', row.get('last_check', 0) + settings(config)['interval_seconds'] if row.get('last_check') else self.clock())

    def ensure_schedule(self, group_id, config, mode):
        key = str(group_id)
        row = self.groups.get(key, {})
        if 'next_check_at' not in row:
            cfg = settings(config)
            anchor = getattr(mode, 'activity', {}).get(key, {}).get('since', self.clock())
            due = row['last_check'] + cfg['interval_seconds'] if row.get('last_check') else anchor + cfg['idle_seconds']
            self._edit(lambda: self.groups.setdefault(key, {}).update(next_check_at=due))

    def attempt_due(self, group_id, config):
        cfg = settings(config)
        if cfg['probability'] == 0:
            return False
        row = self.groups.get(str(group_id), {})
        if int(row.get('misses', 0)) >= cfg['guarantee_checks'] - 1:
            return True
        # A stable sequence, not a fresh random roll; inspection/restarts cannot change the result.
        seed = f'{group_id}:{int(row.get("checks", 0)) + 1}'.encode()
        value = int(hashlib.sha256(seed).hexdigest()[:12], 16) / (16 ** 12)
        return value < cfg['probability']

    def outcome(self, group_id, text):
        self._edit(lambda: self.groups.setdefault(str(group_id), {}).update(last_outcome=text[:160]))

    def _completed(self, group_id, date):
        def edit():
            row = self.groups.setdefault(str(group_id), {})
            day = self._day(row, date)
            day["reviewed"] += 1
            row["daily"] = day
        self._edit(edit)

    def block_reason(self, group_id, config, now, mode, library):
        if not self.enabled(group_id, config):
            return '文献功能、白名单、主动阅读开关或状态文件未就绪'
        if not mode.enabled(group_id, config):
            return '本群主动模式未开启或未获准'
        group = mode.groups.get(str(group_id))
        if group is None:
            return '等待恢复本群后台活动状态'
        cfg, chat = settings(config), mode.effective_settings(group_id, config)
        if cfg['probability'] == 0:
            return '自动挑书权重设为0，等待修改配置'
        clock = self.clock()
        activity = getattr(mode, 'activity', {}).get(str(group_id), {})
        anchor = max(group.last_human, group.last_bot, activity.get('since', clock) if not group.last_human else 0)
        if ProactiveMode.quiet(chat, now.hour):
            return '当前处于静默时段'
        if clock < group.direct_until:
            return '等待最近点名请求结束'
        idle_left = max(cfg['idle_seconds'], chat['settle_seconds']) - (clock - anchor)
        if idle_left > 0:
            return f'等待空闲，约{int(idle_left) + 1}秒'
        if clock - max(group.last_bot, group.last_attempt) < chat['cooldown_seconds']:
            return '主动发言冷却中'
        if any(key[0] == str(group_id) for key in library.busy):
            return '本群有文献任务正在处理'
        day = self._day(self.groups.get(str(group_id), {}), now.date().isoformat())
        quota = mode.daily.get(str(group_id), {})
        used = int(quota.get("count", 0)) if quota.get("date") == now.date().isoformat() else 0
        if day['attempts'] >= cfg['attempt_limit']:
            return '今日挑书尝试额度已用完'
        if day['reviewed'] >= cfg['daily_limit']:
            return '今日主动阅读额度已用完'
        if used + 2 > chat['daily_limit']:
            return '本群主动发言剩余额度不足两条'
        return None

    def eligible(self, group_id, config, now, mode, library):
        return self.block_reason(group_id, config, now, mode, library) is None

    def diagnostic(self, group_id, config, now, mode, library):
        mode.seed(config)
        self.ensure_schedule(group_id, config, mode)
        reason = self.block_reason(group_id, config, now, mode, library)
        row = self.groups.get(str(group_id), {})
        from datetime import datetime
        cfg = settings(config)
        due = self.next_check(group_id, config)
        misses = int(row.get('misses', 0))
        guaranteed = due + max(0, cfg['guarantee_checks'] - 1 - misses) * cfg['interval_seconds']
        at = lambda stamp: datetime.fromtimestamp(stamp, now.tzinfo).strftime('%m-%d %H:%M:%S %z')
        return (f'当前等待原因：{reason or "本群触发条件已满足"}；'
                f'下次检查：{at(due)}；本周期已跳过{misses}/{cfg["guarantee_checks"] - 1}次；'
                f'条件持续允许时最迟保底：{at(guaranteed)}。'
                '到点受阻会保留保底，等待空闲/静默结束/额度恢复，不重新抽签。'
                f'最近结果：{row.get("last_outcome", "尚未检查")}。')

    async def tick(self, get_config, get_now, connected, mode, library, discover, choose,
                   download, review, send, signature, register=None):
        if self.lock.locked() or mode.lock.locked() or not connected() or not self.storage_ok:
            return
        async with self.lock:
            mode.seed(get_config())
            for key, group in list(mode.groups.items()):
                config = get_config()
                now = get_now(config)
                self.ensure_schedule(key, config, mode)
                if not self.eligible(key, config, now, mode, library):
                    continue
                cfg = settings(config)
                row = self.groups.get(key, {})
                if self.clock() < self.next_check(key, config):
                    continue
                # Replay only the bounded deterministic decisions of missed slots.
                # A guarantee cannot drift just because a VM was offline at check time.
                attempt = False
                for _ in range(cfg['guarantee_checks']):
                    if self.clock() < self.next_check(key, config):
                        break
                    attempt = self.attempt_due(key, config)
                    self._reserve_check(key, now.date().isoformat(), attempt, cfg['interval_seconds'])
                    self.log('INFO', f'[AUTO_READING] group={key} check={"attempt" if attempt else "deterministic_skip"}')
                    if attempt:
                        break
                if not attempt:
                    continue
                slot = (key, "auto")
                try:
                    library.acquire(slot)
                except Exception:
                    continue
                mode.background_busy.add(key)
                baseline = {"revision": group.revision, "last_bot": group.last_bot}
                request_signature = None

                def current():
                    fresh = get_config()
                    local_now = get_now(fresh)
                    return (self.storage_ok and connected() and mode.groups.get(key) is group
                            and group.revision == baseline["revision"] and group.last_bot == baseline["last_bot"]
                            and self.enabled(key, fresh) and mode.enabled(key, fresh)
                            and not ProactiveMode.quiet(mode.effective_settings(key, fresh), local_now.hour)
                            and self.clock() >= group.direct_until and signature(fresh) == request_signature)

                async def deliver(text):
                    if not current():
                        return False
                    fresh = get_config()
                    date = get_now(fresh).date().isoformat()
                    quota = mode.daily.get(key, {})
                    used = int(quota.get("count", 0)) if quota.get("date") == date else 0
                    if used >= mode.effective_settings(key, fresh)["daily_limit"]:
                        return False
                    mode.daily[key] = {"date": date, "count": used + 1}
                    mode.save()  # Reserve before send; unknown receipts do not trigger retries.
                    ok = await send(int(key), text)
                    if ok:
                        if group.last_bot == baseline["last_bot"]:
                            mode.note_bot(key, text)
                        baseline["last_bot"] = group.last_bot
                    return ok

                try:
                    request_signature = signature(config)
                    candidates = self.unread(key, await discover(int(key)))
                    if not candidates:
                        self.outcome(key, '尝试已触发，但没有找到可读且未读的群文档')
                        continue
                    if not current():
                        self.outcome(key, '已触发，因前台任务或设置变化取消')
                        continue
                    selected = await choose(candidates, config)
                    if selected is None:
                        self.outcome(key, '已检索文档，但模型没有选出本次想读的文档')
                        continue
                    if not current():
                        self.outcome(key, '已选书，因前台任务或设置变化取消')
                        continue
                    self.outcome(key, '正在下载并解析已选文档')
                    doc = await asyncio.wait_for(download(int(key), selected), timeout=210)
                    if not current():
                        self.outcome(key, '文档已下载，因前台任务或设置变化取消分享')
                        continue
                    if self.seen_document(key, doc):
                        self.mark_reviewed(key, selected, doc)  # Remember a renamed/reuploaded alias.
                        continue
                    announcement = selected.get("_announcement") or "我想先翻翻《" + selected["name"] + "》，看看里面写了些什么。"
                    if not await deliver(announcement):
                        self.outcome(key, '选书通知未确认送达或已被前台任务打断，本次未读模型')
                        continue
                    # The announced book is already downloaded/extracted: questions can interrupt
                    # the background review and use its real text immediately.
                    library.store_shared(key, doc)
                    self.outcome(key, '正在调用模型阅读文档')
                    result = await review(doc, config)
                    self.outcome(key, '模型阅读完成，准备分享' if result else '模型阅读完成，但本次保持沉默')
                    self.mark_reviewed(key, selected, doc)
                    self._completed(key, get_now(get_config()).date().isoformat())
                    if result and current() and await deliver(result):
                        library.store_shared(key, doc)
                        if register:
                            register(key, doc, result)
                        self.log("INFO", f"主动阅读已分享 群{key}")
                        self.outcome(key, '阅读完成并已分享')
                except asyncio.CancelledError:
                    self.outcome(key, '进程停止或任务被取消；已保存进度仍保留')
                    raise
                except Exception as exc:
                    self.outcome(key, '已触发但处理失败：' + type(exc).__name__)
                    self.log("WARN", f"主动阅读本轮跳过 群{key}：{type(exc).__name__}")
                finally:
                    mode.background_busy.discard(key)
                    library.release(slot)
