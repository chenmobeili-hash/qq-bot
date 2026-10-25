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


DEFAULTS = {"enabled": True, "interval_seconds": 7200, "idle_seconds": 1200,
            "probability": 0.35, "daily_limit": 1, "attempt_limit": 2}


def settings(config):
    cfg = dict(DEFAULTS)
    cfg.update((config.get("paper_reader", {}) or {}).get("auto_read", {}) or {})
    for key, low, high in (("interval_seconds", 600, 86400), ("idle_seconds", 300, 86400),
                           ("daily_limit", 1, 5), ("attempt_limit", 1, 10)):
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

    def _reserve_check(self, group_id, date, attempt):
        def edit():
            row = self.groups.setdefault(str(group_id), {})
            row["last_check"] = self.clock()
            if attempt:
                day = self._day(row, date)
                day["attempts"] += 1
                row["daily"] = day
        self._edit(edit)

    def _completed(self, group_id, date):
        def edit():
            row = self.groups.setdefault(str(group_id), {})
            day = self._day(row, date)
            day["reviewed"] += 1
            row["daily"] = day
        self._edit(edit)

    def eligible(self, group_id, config, now, mode, library):
        if not self.enabled(group_id, config) or not mode.enabled(group_id, config):
            return False
        group = mode.groups.get(str(group_id))
        if group is None or not group.last_human:
            return False
        cfg, chat = settings(config), mode.effective_settings(group_id, config)
        clock = self.clock()
        if (ProactiveMode.quiet(chat, now.hour) or clock < group.direct_until
                or clock - group.last_human < max(cfg["idle_seconds"], chat["settle_seconds"])
                or clock - group.last_human > chat["context_max_age_seconds"]
                or clock - max(group.last_bot, group.last_attempt) < chat["cooldown_seconds"]
                or any(key[0] == str(group_id) for key in library.busy)):
            return False
        day = self._day(self.groups.get(str(group_id), {}), now.date().isoformat())
        quota = mode.daily.get(str(group_id), {})
        used = int(quota.get("count", 0)) if quota.get("date") == now.date().isoformat() else 0
        return (day["attempts"] < cfg["attempt_limit"] and day["reviewed"] < cfg["daily_limit"]
                and used + 2 <= chat["daily_limit"])

    async def tick(self, get_config, get_now, connected, mode, library, discover, choose,
                   download, review, send, signature, register=None):
        if self.lock.locked() or mode.lock.locked() or not connected() or not self.storage_ok:
            return
        async with self.lock:
            for key, group in list(mode.groups.items()):
                config = get_config()
                now = get_now(config)
                if not self.eligible(key, config, now, mode, library):
                    continue
                cfg = settings(config)
                row = self.groups.get(key, {})
                if self.clock() - row.get("last_check", 0) < cfg["interval_seconds"]:
                    continue
                attempt = self.roll() < cfg["probability"]
                self._reserve_check(key, now.date().isoformat(), attempt)
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
                    if not candidates or not current():
                        continue
                    selected = await choose(candidates, config)
                    if selected is None or not current():
                        continue
                    doc = await asyncio.wait_for(download(int(key), selected), timeout=210)
                    if not current():
                        continue
                    if self.seen_document(key, doc):
                        self.mark_reviewed(key, selected, doc)  # Remember a renamed/reuploaded alias.
                        continue
                    announcement = selected.get("_announcement") or "我想先翻翻《" + selected["name"] + "》，看看里面写了些什么。"
                    if not await deliver(announcement):
                        continue
                    # The announced book is already downloaded/extracted: questions can interrupt
                    # the background review and use its real text immediately.
                    library.store_shared(key, doc)
                    result = await review(doc, config)
                    self.mark_reviewed(key, selected, doc)
                    self._completed(key, get_now(get_config()).date().isoformat())
                    if result and current() and await deliver(result):
                        library.store_shared(key, doc)
                        if register:
                            register(key, doc, result)
                        self.log("INFO", f"主动阅读已分享 群{key}")
                except Exception as exc:
                    self.log("WARN", f"主动阅读本轮跳过 群{key}：{type(exc).__name__}")
                finally:
                    mode.background_busy.discard(key)
                    library.release(slot)
