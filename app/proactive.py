"""Opt-in group participation; no network calls or file access at import time."""
import asyncio
import json
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


DEFAULTS = {
    "enabled": True, "groups": [], "reply_probability": 0.25,
    "cooldown_seconds": 300, "daily_limit": 24,
    "quiet_start": 23, "quiet_end": 8, "settle_seconds": 8,
    "idle_enabled": True, "idle_seconds": 1800,
    "context_messages": 12, "context_max_age_seconds": 21600,
    "min_messages": 2,
}

# Only frequency changes with a level; global enablement and quiet hours remain configured.
LEVELS = {
    "低频": {"reply_probability": 0.08, "cooldown_seconds": 600, "min_messages": 3,
           "idle_seconds": 3600, "daily_limit": 8},
    "标准": {"reply_probability": 0.25, "cooldown_seconds": 300, "min_messages": 2,
           "idle_seconds": 1800, "daily_limit": 24},
    "活跃": {"reply_probability": 0.65, "cooldown_seconds": 120, "min_messages": 1,
           "idle_seconds": 900, "daily_limit": 30},
}


# Official Q群管家 account; match an authenticated OneBot sender ID, never a nickname.
IGNORED_SENDERS = frozenset({'2854196310'})


def ignored_sender(sender_id, config):
    extra = (config.get('proactive', {}) or {}).get('ignore_user_ids', [])
    if not isinstance(extra, (list, tuple, set)):
        extra = []
    return str(sender_id) in IGNORED_SENDERS or str(sender_id) in {str(v) for v in extra}


def settings(config):
    result = dict(DEFAULTS)
    result.update(config.get("proactive", {}) or {})
    for name, low, high in (
        ("cooldown_seconds", 60, 86400), ("daily_limit", 1, 100),
        ("settle_seconds", 3, 120), ("idle_seconds", 600, 86400),
        ("context_messages", 1, 30), ("context_max_age_seconds", 600, 86400),
        ("min_messages", 1, 30), ("quiet_start", 0, 23), ("quiet_end", 0, 23),
    ):
        result[name] = max(low, min(high, int(result[name])))
    result["reply_probability"] = max(0.0, min(1.0, float(result["reply_probability"])))
    return result


@dataclass
class Group:
    messages: deque = field(default_factory=lambda: deque(maxlen=30))
    revision: int = 0
    last_human: float = 0
    last_bot: float = 0
    last_attempt: float = 0
    direct_until: float = 0
    checked_revision: int = -1
    idle_used: bool = False


class ProactiveMode:
    def __init__(self, state_path, log, clock=time.time, roll=random.random):
        self.path = Path(state_path)
        self.log = log
        self.clock = clock
        self.roll = roll
        self.groups = {}
        self.background_busy = set()
        self.overrides = {}
        self.levels = {}
        self.daily = {}
        self.image_daily = {}
        self.lock = asyncio.Lock()
        self.storage_ok = True
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.overrides = {str(k): v for k, v in saved.get("groups", {}).items()
                                  if isinstance(v, bool)}
                self.levels = {str(k): v for k, v in saved.get("levels", {}).items()
                               if isinstance(v, str) and v in LEVELS}
                self.daily = {str(k): v for k, v in saved.get("daily", {}).items()
                              if isinstance(v, dict)}
                self.image_daily = {str(k): v for k, v in saved.get('image_daily', {}).items()
                                    if isinstance(v, dict)}
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                self.storage_ok = False
                self.log("ERROR", f"主动模式状态读取失败，已暂停主动发言：{type(exc).__name__}")

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"groups": self.overrides, "levels": self.levels, "daily": self.daily,
                                        "image_daily": self.image_daily},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def effective_settings(self, group_id, config):
        cfg = settings(config)
        cfg.update(LEVELS.get(self.levels.get(str(group_id)), {}))
        return cfg

    def set_level(self, group_id, level):
        if not self.storage_ok:
            raise OSError("状态文件读取失败，请先检查 proactive_state.json")
        if level != "配置" and level not in LEVELS:
            raise ValueError("未知主动模式档位")
        key, previous = str(group_id), dict(self.levels)
        if level == "配置":
            self.levels.pop(key, None)
        else:
            self.levels[key] = level
        try:
            self.save()
        except OSError:
            self.levels = previous
            raise
        # Invalidate in-flight results and do not reuse activity from the old level.
        self.groups.pop(key, None)

    def enabled(self, group_id, config):
        cfg = settings(config)
        key = str(group_id)
        allowed = config.get("deepseek", {}).get("trigger_groups", [])
        return (self.storage_ok and bool(cfg["enabled"])
                and bool(config.get("deepseek", {}).get("enabled", False))
                and (not allowed or key in set(map(str, allowed)))
                and self.overrides.get(key, key in set(map(str, cfg["groups"]))))

    def set_enabled(self, group_id, enabled):
        if not self.storage_ok:
            raise OSError("状态文件读取失败，请先检查 proactive_state.json")
        key = str(group_id)
        previous = dict(self.overrides)
        self.overrides[key] = enabled
        try:
            self.save()
        except OSError:
            self.overrides = previous
            raise
        # Do not act on messages received before a mode change.
        self.groups.pop(key, None)

    def reserve_image(self, group_id, config, day):
        key = str(group_id)
        if not self.enabled(key, config) or not config.get('proactive', {}).get('images_enabled', True):
            return False
        usage = self.image_daily.get(key, {})
        count = int(usage.get('count', 0)) if usage.get('date') == day else 0
        if count >= self.effective_settings(key, config)['daily_limit']:
            return False
        previous = dict(self.image_daily)
        self.image_daily[key] = {'date': day, 'count': count + 1}
        try:
            self.save()
        except OSError:
            self.image_daily = previous
            return False
        return True

    def prune_images(self, group):
        for row in group.messages:
            if self.clock() - row.get('at', 0) >= 600:
                row.pop('images', None)

    def observe(self, group_id, sender, text, config, direct=False, image_urls=None, sender_id=None):
        if ignored_sender(sender_id, config) or not self.enabled(group_id, config):
            return
        key = str(group_id)
        group = self.groups.setdefault(key, Group())
        self.prune_images(group)
        now = self.clock()
        group.revision += 1
        group.last_human = now
        group.idle_used = False
        if direct:
            # Give explicit requests priority, including slow image/AI requests.
            group.direct_until = now + 300
            group.checked_revision = group.revision
            group.last_attempt = now
        images = list(image_urls or [])[:1] if not direct and config.get('proactive', {}).get('images_enabled', True) else []
        if text.strip() or images:
            row = {"sender": str(sender)[:60], "text": text[:1000] or '[群友发送了图片，尚未识别]', "at": now}
            if images:
                row.update(images=images, image_id=uuid.uuid4().hex, image_attempted=False)
            group.messages.append(row)

    def note_bot(self, group_id, text):
        group = self.groups.get(str(group_id))
        if group:
            now = self.clock()
            group.last_bot = now
            group.direct_until = 0
            group.checked_revision = group.revision
            group.messages.append({"sender": "机器人自己", "text": text[:1000], "at": now})

    @staticmethod
    def quiet(cfg, hour):
        start, end = cfg["quiet_start"], cfg["quiet_end"]
        if start == end:
            return False
        return start <= hour < end if start < end else hour >= start or hour < end

    def reason(self, key, config, local_now):
        if key in self.background_busy or not self.enabled(key, config):
            return None
        cfg = self.effective_settings(key, config)
        group = self.groups[key]
        now = self.clock()
        elapsed = now - group.last_human
        if (self.quiet(cfg, local_now.hour) or now < group.direct_until
                or now - max(group.last_bot, group.last_attempt) < cfg["cooldown_seconds"]
                or elapsed < cfg["settle_seconds"]
                or elapsed > cfg["context_max_age_seconds"]):
            return None
        day = local_now.date().isoformat()
        usage = self.daily.get(key, {})
        if usage.get("date") == day and int(usage.get("count", 0)) >= cfg["daily_limit"]:
            return None
        if elapsed >= cfg["idle_seconds"]:
            if (cfg["idle_enabled"] and not group.idle_used
                    and group.last_human > group.last_bot):
                return "idle"
            return None
        if group.checked_revision == group.revision:
            return None
        group.checked_revision = group.revision
        recent_humans = [m for m in group.messages if m["sender"] != "机器人自己"
                         and now - m["at"] <= 300]
        if len(recent_humans) < cfg["min_messages"]:
            return None
        return "join" if self.roll() < cfg["reply_probability"] else None

    async def tick(self, get_config, get_now, connected, generate, send):
        if self.lock.locked() or not connected():
            return
        async with self.lock:
            for key, group in list(self.groups.items()):
                self.prune_images(group)
                config = get_config()
                local_now = get_now(config)
                reason = self.reason(key, config, local_now)
                if not reason:
                    continue
                cfg = self.effective_settings(key, config)
                revision = group.revision
                last_bot = group.last_bot
                group.last_attempt = self.clock()
                if reason == "idle":
                    # One opening opportunity per human activity, including a model decision to stay silent.
                    group.idle_used = True
                messages = [dict(m) for m in group.messages
                            if self.clock() - m["at"] <= cfg["context_max_age_seconds"]]
                messages = messages[-cfg["context_messages"]:]
                try:
                    reply = await generate(key, reason, messages, config)
                    fresh = get_config()
                    now = get_now(fresh)
                    if (not reply or self.groups.get(key) is not group
                            or key in self.background_busy
                            or group.revision != revision or group.last_bot != last_bot
                            or not self.enabled(key, fresh) or not connected()
                            or self.quiet(self.effective_settings(key, fresh), now.hour)
                            or self.clock() < group.direct_until):
                        continue
                    # Reserve quota before delivery: an uncertain receipt must not cause a retry flood.
                    day = now.date().isoformat()
                    usage = self.daily.get(key, {})
                    count = int(usage.get("count", 0)) if usage.get("date") == day else 0
                    if count >= self.effective_settings(key, fresh)["daily_limit"]:
                        continue
                    self.daily[key] = {"date": day, "count": count + 1}
                    self.save()
                    if reason == "idle":
                        group.idle_used = True
                    if await send(int(key), reply):
                        self.note_bot(key, reply)
                        self.log("INFO", f"主动模式已发言 群{key} 类型={reason}")
                except Exception as exc:
                    # Do not expose keys, endpoints or a service response in group chat.
                    self.log("WARN", f"主动模式本轮跳过 群{key}：{type(exc).__name__}")
