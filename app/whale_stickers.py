"""Local whale-girl catalog, contextual selection and QQ-safe image payloads."""
import base64
import json
import math
from pathlib import Path
import re
import time
import uuid

MARKER = re.compile(r"\[\[whale:([^\]\r\n]{1,160})\]\]")


class StickerReply(str):
    def __new__(cls, text, sticker=None, persona_signature=None):
        obj = super().__new__(cls, text)
        obj.whale_sticker = sticker
        obj.persona_signature = persona_signature
        return obj

    def __bool__(self):
        return len(self) > 0 or self.whale_sticker is not None


def scope_from_user(user):
    group = re.fullmatch(r"group_(\d+)_\d+", str(user))
    private = re.fullmatch(r"private_(\d+)", str(user))
    return "g:" + group.group(1) if group else "p:" + private.group(1) if private else None


def strip_markers(text):
    pieces = re.split(r"(```[\s\S]*?```|`[^`\r\n]*`)", str(text))
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(r"\[\[whale:[^\]\r\n]*$", "", MARKER.sub("", pieces[index]))
    return "".join(pieces).strip()


class WhaleStickers:
    def __init__(self, root, state_path, clock=time.time):
        self.root = Path(root).resolve()
        self.state_path = Path(state_path)
        self.clock = clock
        catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
        self.catalog = {}
        for row in catalog:
            if not re.fullmatch(r"[a-z0-9-]+", row["id"]) or not re.fullmatch(r"[a-z0-9-]+\.png", row["file"]):
                raise ValueError("Invalid sticker catalog")
            self.catalog[row["id"]] = row
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        if not isinstance(self.state, dict):
            raise ValueError("Invalid sticker state")
        for key, row in self.state.items():
            if not re.fullmatch(r"[gp]:\d+", key) or not isinstance(row, dict):
                raise ValueError("Invalid sticker target state")
            if "last_at" in row and (not isinstance(row["last_at"], (int, float)) or not math.isfinite(row["last_at"])):
                raise ValueError("Invalid sticker cooldown")
            if "recent" in row and (not isinstance(row["recent"], list) or not all(isinstance(x, str) for x in row["recent"])):
                raise ValueError("Invalid recent stickers")

    def save(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def settings(self, scope, config):
        raw = config.get("whale_stickers", {}) or {}
        row = self.state.get(scope, {})
        try:
            cooldown = max(10, min(3600, float(raw.get("cooldown_seconds", 90))))
        except (TypeError, ValueError):
            cooldown = 90
        return {"enabled": bool(raw.get("enabled", True)), "auto": bool(row.get("auto", True)),
                "variant": row.get("variant", raw.get("variant", "blue")) if row.get("variant", raw.get("variant", "blue")) in ("blue", "black") else "blue",
                "cooldown": cooldown}

    def set_option(self, scope, **values):
        if not re.fullmatch(r"[gp]:\d+", scope):
            raise ValueError("Invalid target")
        old = dict(self.state.get(scope, {}))
        self.state[scope] = {**old, **values}
        try:
            self.save()
        except Exception:
            self.state[scope] = old
            raise

    def offer(self, scope, config):
        if not scope:
            return False
        cfg = self.settings(scope, config)
        row = self.state.get(scope, {})
        return (cfg["enabled"] and cfg["auto"] and
                (not row.get("last_at") or self.clock() - row["last_at"] >= cfg["cooldown"]))

    def guidance(self, scope, config):
        if not self.offer(scope, config):
            return ""
        recent = self.state.get(scope, {}).get("recent", [])
        entries = [f"{key}={row['text']}" for key, row in self.catalog.items() if key not in recent]
        variant = self.settings(scope, config)["variant"]
        return ("\n你可以按语境附上一张鲸鱼娘表情。不要每次都发，不要代替应有的解答；"
                "严肃、悲伤或需要认真安慰的场合不要用嘲讽贴纸。贴纸上的服务器/测试等文案不代表真实状态，不能拿它编造状态。"
                "合适时仅在本轮回答末尾添加一个内部标记 [[whale:ID:" + variant + "]]，不合适就不添加。"
                "这个标记会转换成真实图片，勿解释标记或凭空创造ID。可用ID及图中文字：\n" + "；".join(entries))

    def extract(self, text, scope, config):
        selection = None
        if self.offer(scope, config):
            recent = self.state.get(scope, {}).get("recent", [])
            matches = list(MARKER.finditer(str(text)))
            for match in matches[-1:]:
                if str(text)[match.end():].strip() or str(text)[:match.start()].count("```") % 2:
                    continue
                parts = match.group(1).split(":")
                id_ = parts[0]
                variant = parts[1] if len(parts) == 2 else self.settings(scope, config)["variant"]
                if len(parts) <= 2 and id_ in self.catalog and variant in ("blue", "black") and id_ not in recent:
                    selection = (id_, self.settings(scope, config)["variant"])
                    break
        return strip_markers(text), selection

    def resolve(self, query):
        if query in self.catalog:
            return query
        for key, row in self.catalog.items():
            if query == row["text"] or query in row.get("triggers", []):
                return key
        return None

    def image_segment(self, id_, variant):
        if id_ not in self.catalog or variant not in ("blue", "black"):
            raise ValueError("Unknown sticker")
        file = self.catalog[id_]["file"]
        path = (self.root / file if variant == "blue" else self.root / "black" / file).resolve()
        if not path.is_relative_to(self.root) or path.stat().st_size > 3_500_000:
            raise ValueError("Invalid sticker file")
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid sticker image")
        return {"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode("ascii"),
                                           "summary": "[鲸鱼娘：" + self.catalog[id_]["text"] + "]"}}

    def reserve(self, scope, id_, variant, config, manual=False):
        cfg = self.settings(scope, config)
        row = self.state.get(scope, {})
        if not cfg["enabled"] or (not manual and not self.offer(scope, config)):
            return None
        if manual and row.get("last_at") and self.clock() - row["last_at"] < 3:
            return None
        if not manual and id_ in row.get("recent", []):
            return None
        segment = self.image_segment(id_, variant)
        ticket = uuid.uuid4().hex
        self.state[scope] = {**row, "last_at": self.clock(), "ticket": ticket,
                             "recent": (row.get("recent", []) + [id_])[-2:]}
        try:
            self.save()
        except Exception:
            self.state[scope] = row
            raise
        return ticket, segment, row

    def rejected(self, scope, reservation):
        ticket, _, previous = reservation
        if self.state.get(scope, {}).get("ticket") == ticket:
            # Preserve a concurrently changed switch/variant while undoing a definite failure.
            changed = {key: self.state[scope][key] for key in ("auto", "variant") if key in self.state[scope]}
            self.state[scope] = {**previous, **changed}
            self.save()
