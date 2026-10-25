# -*- coding: utf-8 -*-
"""图片身份识别（搜图）——SauceNAO 为主，Serper 文字搜索兜底

为什么单独一个模块，而不是塞进 bot.py：
  1. bot.py 已经 1900+ 行，再混进搜图逻辑会更难维护；
  2. 搜图有自己的限速约束（SauceNAO 免费额度是「30 秒内 N 次 + 每天 M 次」双限制），
     必须自己做节流，这部分逻辑值得独立且有注释；
  3. 独立后能脱离 bot 单跑，方便验证：python image_search.py 图片路径

API 事实（来自 saucenaopie 客户端源码实测确认，勿凭记忆改）：
  · POST https://saucenao.com/search.php
  · api_key 走【表单参数】，不是 HTTP header
  · output_type=2 才是 JSON；numres 控制返回条数（不是 maxresults）
  · 图片用 multipart/form-data，字段名就是 file
  · 响应是 {"header": {...}, "results": [{"header": {...}, "data": {...}}, ...]}
    其中 similarity / index_id 在【每个 result 自己的 header】里，
    且数值是字符串（"93.28"），必须转 float 才能比较
  · 403=api_key 无效、413=图片太大、429=限速（限额数字在 header 里）
"""
from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:  # 仅用于"图片过大时压缩"，缺了也能跑
    from PIL import Image
    import io as _io
    _HAS_PIL = True
except Exception:  # pragma: no cover
    _HAS_PIL = False

SAUCENAO_ENDPOINT = "https://saucenao.com/search.php"

# SauceNAO 对上传体积有限制，超过就返回 413。留出余量。
MAX_UPLOAD_BYTES = 1_900_000

# 免费 key 的限速（两个包文档分别写 4次/30秒 与 8次/30秒，取保守值）
DEFAULT_MIN_INTERVAL = 8.0     # 两次搜图之间至少间隔这么多秒
DEFAULT_DAILY_LIMIT = 100      # 每天最多搜这么多次（内存计数，重启归零）
RETRIES = 3                    # 网络类故障（SSL 断连/连接重置）重试次数

_state = {
    "last_call": 0.0,          # 上次实际发起搜图的时刻
    "day": "",                 # 计数所属日期
    "count": 0,                # 当天已用次数
}


# ============================================================
# 配置
# ============================================================

def load_config(config_path: str | Path | None = None) -> dict:
    """读取 bot 的 config.yaml（和 bot.py 共用同一份配置）"""
    import yaml
    path = Path(config_path) if config_path else Path(__file__).resolve().parent / "config.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def get_saucenao_config(config: dict | None = None) -> dict:
    """取出 saucenao 段并补默认值"""
    cfg = (config or load_config()).get("saucenao", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "api_key": str(cfg.get("api_key", "") or "").strip(),
        "db": int(cfg.get("db", 999)),
        "numres": int(cfg.get("numres", 5)),
        "min_similarity": float(cfg.get("min_similarity", 50.0)),
        "timeout": int(cfg.get("timeout", 30)),
        "min_interval_seconds": float(cfg.get("min_interval_seconds", DEFAULT_MIN_INTERVAL)),
        "daily_limit": int(cfg.get("daily_limit", DEFAULT_DAILY_LIMIT)),
        "fallback_search": bool(cfg.get("fallback_search", True)),
        "fallback_suffix": str(cfg.get("fallback_suffix", " 角色 出处")),
        # 第 2 跳的后缀：用「作品 + 角色特征」去搜角色名
        "twohop_suffix": str(cfg.get("twohop_suffix", " 角色")),
        # 代理：留空=跟随系统设置；"direct"/"none"=强制直连；也可填 http://127.0.0.1:2080
        "proxy": str(cfg.get("proxy", "") or "").strip(),
        # 第 3 跳：搜图能认出作品但认不出人时，去抓该作品的百科页正文找角色名
        "wiki_lookup": bool(cfg.get("wiki_lookup", True)),
        # 排障：把搜过的图存一份到 sauce_debug/，便于事后用
        # `python image_search.py <图> --debug` 复现当时到底返回了什么字段
        "debug_save_image": bool(cfg.get("debug_save_image", False)),
    }


def _build_opener(cfg: dict):
    """按配置构造 urllib opener（代理策略可控，便于排障）"""
    proxy = cfg.get("proxy", "")
    if proxy.lower() in ("direct", "none", "off"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()   # 跟随系统代理


# ============================================================
# 节流：保护免费额度
# ============================================================

def check_quota(cfg: dict) -> tuple[bool, str]:
    """判断现在能不能发起搜图。返回 (是否放行, 拦截原因)"""
    now = time.time()
    today = time.strftime("%Y-%m-%d")

    # 跨天重置计数
    if _state["day"] != today:
        _state["day"] = today
        _state["count"] = 0

    if _state["count"] >= cfg["daily_limit"]:
        return False, f"今日搜图次数已达上限 {cfg['daily_limit']} 次（防超额，明天自动重置）"

    wait = cfg["min_interval_seconds"] - (now - _state["last_call"])
    if wait > 0:
        return False, f"距上次搜图不足 {cfg['min_interval_seconds']:.0f} 秒，还需等 {wait:.1f} 秒（避免触发限速）"

    return True, ""


def _mark_used():
    _state["last_call"] = time.time()
    _state["count"] += 1


def quota_snapshot() -> dict:
    """给日志用：当前额度的粗略状态。真实剩余量以 SauceNAO 响应 header 为准"""
    return {"day": _state["day"], "used_today": _state["count"]}


# ============================================================
# 图片预处理
# ============================================================

def shrink_if_needed(image_bytes: bytes) -> tuple[bytes, str]:
    """图片过大时压到限值以内。返回 (字节, 说明)"""
    if len(image_bytes) <= MAX_UPLOAD_BYTES:
        return image_bytes, f"原图 {len(image_bytes)/1024:.0f} KB，无需压缩"
    if not _HAS_PIL:
        return image_bytes, f"⚠️ 图片 {len(image_bytes)/1024:.0f} KB 超过限制且缺少 Pillow，可能被拒(413)"

    try:
        im = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        w, h = im.size
        # 先按最长边缩到 1600，再按需要降 JPEG 质量
        longest = max(w, h)
        if longest > 1600:
            scale = 1600 / longest
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        for quality in (88, 78, 68, 58, 48):
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_UPLOAD_BYTES:
                return data, (f"原图 {len(image_bytes)/1024:.0f} KB 超限 → "
                              f"压到 {len(data)/1024:.0f} KB (JPEG q={quality})")
        return data, f"原图 {len(image_bytes)/1024:.0f} KB → 压到 {len(data)/1024:.0f} KB（仍偏大，可能被拒）"
    except Exception as e:
        return image_bytes, f"⚠️ 压缩失败({type(e).__name__}: {e})，按原图上传"


# ============================================================
# multipart 组装（标准库手写，避免引入依赖）
# ============================================================

def _build_multipart(fields: dict, filename: str, file_bytes: bytes,
                     mime: str) -> tuple[bytes, str]:
    boundary = "----SauceNaoBoundary" + uuid.uuid4().hex
    body = bytearray()

    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()

    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode()
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _guess_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "image/jpeg"


def _limits_from_error_body(detail: str) -> dict:
    """从 429/403 的错误 JSON 里解析剩余额度（若可解析）"""
    if not detail:
        return {}
    try:
        err = json.loads(detail)
        hdr = err.get("header", {}) or {}
        lim = {}
        for k in ("short_remaining", "long_remaining", "short_limit", "long_limit"):
            v = hdr.get(k)
            if v is not None:
                try:
                    lim[k] = int(v)
                except (TypeError, ValueError):
                    lim[k] = v
        return lim
    except Exception:
        return {}


# ============================================================
# 核心：调 SauceNAO
# ============================================================

def saucenao_search(image_bytes: bytes, cfg: dict | None = None,
                    enforce_quota: bool = True, debug: bool = False) -> dict:
    """调用 SauceNAO 搜图。

    返回 dict：
      {"ok": bool, "reason": str, "results": [...], "limits": {...}, "raw": {...}}
    每条形如：
      {"similarity": 93.2, "index_name": "pixiv", "index_id": 5,
       "character": "めぐみん", "title": "...", "author": "...",
       "urls": [...], "thumbnail": "..."}
    """
    cfg = cfg or get_saucenao_config()

    if enforce_quota:
        allowed, why = check_quota(cfg)
        if not allowed:
            return {"ok": False, "reason": why, "results": [], "limits": {}, "raw": None}

    image_bytes, shrink_note = shrink_if_needed(image_bytes)

    fields = {
        "output_type": 2,          # 2 = JSON
        "db": cfg["db"],
        "numres": cfg["numres"],
        "hide": 0,
    }
    if cfg["api_key"]:
        fields["api_key"] = cfg["api_key"]

    body, content_type = _build_multipart(fields, "image.jpg", image_bytes,
                                          _guess_mime("image.jpg"))
    req = urllib.request.Request(SAUCENAO_ENDPOINT, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("User-Agent", "qq-bot-image-search/1.0")

    opener = _build_opener(cfg)
    _mark_used()

    # 网络类故障（SSL 被掐断、连接重置）实测偶发，值得重试；
    # 而 403/413 这类确定性错误重试纯属浪费额度，直接返回。
    payload = None
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with opener.open(req, timeout=cfg["timeout"]) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass

            # 429 / 5xx 属于「等一会可能就好」
            if (e.code == 429 or 500 <= e.code < 600) and attempt < RETRIES:
                last_err = e
                time.sleep(attempt * 3)
                continue

            if e.code == 403:
                if not cfg["api_key"]:
                    reason = ("SauceNAO 需要 api_key：官方已不允许匿名调用 API"
                              "（请在 config.yaml 的 saucenao.api_key 填入密钥）")
                elif "not permit" in detail.lower() or "anonymous" in detail.lower():
                    reason = f"SauceNAO 拒绝：该 key 的账号类型不允许 API 调用（HTTP 403）{detail}"
                else:
                    reason = f"SauceNAO 拒绝：api_key 无效或未授权（HTTP 403）{detail}"
            elif e.code == 413:
                reason = f"SauceNAO 拒绝：图片太大（HTTP 413）{detail}"
            elif e.code == 429:
                reason = f"SauceNAO 限速/额度用尽（HTTP 429）{detail}"
            else:
                reason = f"SauceNAO 返回 HTTP {e.code} {detail}"

            limits = _limits_from_error_body(detail)
            return {"ok": False, "reason": reason, "results": [], "limits": limits, "raw": None}
        except Exception as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(attempt * 3)
                continue
            return {"ok": False,
                    "reason": f"SauceNAO 请求失败（已重试 {RETRIES} 次）：{type(e).__name__}: {e}",
                    "results": [], "limits": {}, "raw": None}

    if payload is None:
        return {"ok": False,
                "reason": f"SauceNAO 请求失败（已重试 {RETRIES} 次）：{last_err}",
                "results": [], "limits": {}, "raw": None}

    header = payload.get("header", {}) or {}
    limits = {
        "short_remaining": header.get("short_remaining"),
        "long_remaining": header.get("long_remaining"),
        "short_limit": header.get("short_limit"),
        "long_limit": header.get("long_limit"),
        "status": header.get("status"),
    }

    results = []
    for item in (payload.get("results") or []):
        h = item.get("header", {}) or {}
        d = item.get("data", {}) or {}
        try:
            sim = float(h.get("similarity", 0) or 0)
        except (TypeError, ValueError):
            sim = 0.0

        # 角色名在不同图库里的字段名不一样，按优先级取第一个非空的
        character = ""
        for key in ("character", "characters", "danbooru_character",
                    "gelbooru_character", "anime_name", "characters_name"):
            v = d.get(key)
            if v:
                character = ", ".join(v) if isinstance(v, list) else str(v)
                break

        # 【作品/系列】单独取：material / copyright 才是「出自哪部作品」，
        # 不能让它被 title 挡住 —— 这是踩过的坑：
        # Gelbooru 结果的 title 常是 "original" 这类无信息量标签，
        # 而真正的作品名（游戏/动画）在 material 里，旧逻辑按
        # title→eng_name→material 取值，导致 title="original" 把作品名吞掉了。
        material = ""
        for key in ("material", "copyright", "series", "anime_name"):
            v = d.get(key)
            if v:
                material = ", ".join(v) if isinstance(v, list) else str(v)
                break

        # 标题：过滤掉 "original"/"unknown" 这类无信息量的占位标签
        NOISE_TITLES = {"original", "unknown", "n/a", "none", "-", ""}
        title = ""
        for key in ("title", "eng_name"):
            v = d.get(key)
            if v and str(v).strip().lower() not in NOISE_TITLES:
                title = str(v)
                break
        # 兜底：source 若不是 URL，也可当标题（如 "RE：D Cherish！"）
        if not title:
            src0 = d.get("source")
            if isinstance(src0, str) and src0 and not src0.startswith("http"):
                title = src0

        author = ""
        for key in ("author", "author_name", "member_name", "creator",
                    "pawoo_user_display_name", "company"):
            v = d.get(key)
            if v:
                # 作者字段有时是 list，join 成逗号分隔
                author = ", ".join(v) if isinstance(v, list) else str(v)
                break

        urls = []
        for key in ("ext_urls", "url", "author_url"):
            v = d.get(key)
            if isinstance(v, list):
                urls.extend(str(u) for u in v)
            elif v:
                urls.append(str(v))
        # source 字段：只有真正的 URL 才当链接；标题文字（如 "RE：D Cherish！"）不进链接位
        src = d.get("source")
        if isinstance(src, str) and src.startswith("http"):
            urls.append(src)
        elif isinstance(src, list):
            for u in src:
                if str(u).startswith("http"):
                    urls.append(str(u))
        # 去重保序
        seen, uniq = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)

        # 图库名：SauceNAO 有时把 index_name 塞成 "Index #38: H-Misc (E-Hentai) - xxx.jpg"，
        # 取冒号后的可读部分，去掉文件名尾巴
        raw_index = h.get("index_name", "")
        index_name = raw_index
        if raw_index:
            # "Index #38: H-Misc (E-Hentai)" → "H-Misc (E-Hentai)"
            idx_match = re.match(r"^Index\s+#\d+:\s*(.*)$", raw_index)
            if idx_match:
                index_name = idx_match.group(1)
            # 去掉末尾的 " - xxx.jpg" 文件名（哈希里可能含下划线，必须一起匹配，
            # 之前只写 [A-Za-z0-9]+ 导致 "..._2.jpg" 这类尾巴没被清掉）
            index_name = re.sub(r"\s*-\s*[A-Za-z0-9_]+\.jpe?g$", "", index_name)

        results.append({
            "similarity": sim,
            "index_name": index_name,
            "index_id": h.get("index_id"),
            "character": character,
            "material": material,
            "title": title,
            "author": author,
            "urls": uniq,
            "thumbnail": h.get("thumbnail", ""),
            "data_keys": sorted(d.keys()) if debug else None,
        })

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return {
        "ok": True,
        "reason": shrink_note,
        "results": results,
        "limits": limits,
        "raw": payload if debug else None,
    }


# ============================================================
# 结果格式化：给聊天模型看的紧凑文本
# ============================================================

def format_results(res: dict, min_similarity: float = 60.0,
                   max_items: int = 3) -> str:
    """把搜图结果排版成一段紧凑中文，供聊天模型引用。没够格的返回空串"""
    if not res.get("ok"):
        return ""
    good = [r for r in res.get("results", []) if r["similarity"] >= min_similarity]
    if not good:
        return ""

    # 【排序】带「角色/作品」的结果优先于单纯高相似度的结果。
    # 踩过的坑：93.3% 命中的那条只有画师（认不出是谁），
    # 而 booru 类图库的结果即使相似度低些，却带 characters+material（能认出是谁）。
    # 只按相似度排会把真正有用的信息挤到后面。
    def _rank(r: dict):
        has_identity = 1 if (r.get("character") or r.get("material")) else 0
        return (has_identity, r["similarity"])

    good = sorted(good, key=_rank, reverse=True)

    lines = []
    for r in good[:max_items]:
        bits = [f"相似度 {r['similarity']:.1f}%"]
        if r["index_name"]:
            bits.append(f"来源库 {r['index_name']}")
        head = "· " + "，".join(bits)
        lines.append(head)
        if r["character"]:
            lines.append(f"  角色：{r['character']}")
        # 作品/系列放在标题前面：它才是「出自哪部作品」的答案
        if r.get("material"):
            lines.append(f"  作品/系列：{r['material']}")
        if r["title"]:
            lines.append(f"  标题：{r['title']}")
        if r["author"]:
            lines.append(f"  作者：{r['author']}")
        if r["urls"]:
            lines.append(f"  链接：{r['urls'][0]}")
    return "\n".join(lines)


def build_fallback_query(description: str, cfg: dict) -> str:
    """SauceNAO 没结果时，用视觉描述构造一个文字搜索 query"""
    desc = " ".join((description or "").split())
    # 去掉"这是一张/图片中"这类无信息量的开头，控制长度
    for noise in ("这是一张", "这张图片", "图片中", "图中", "这图"):
        if desc.startswith(noise):
            desc = desc[len(noise):]
    desc = desc[:60].strip("，。、 　")
    return f"{desc}{cfg.get('fallback_suffix', ' 角色 出处')}".strip()


# 这些值等于"没有信息"，不能当作品名/标题用
IDENTITY_NOISE = {"original", "unknown", "n/a", "none", "-", ""}


def has_identity(results: list[dict], min_similarity: float) -> bool:
    """搜图结果里是否含有真正的「身份信息」（角色名，或有意义的作品名）。

    为什么需要：实测 201934.png 命中 Gelbooru 93.33%，但图库源头标注是
    material=original、characters 为空 —— 相似度很高却认不出任何人。
    这种"高相似度但零身份信息"的情况必须识别出来，才能触发第 2 跳。
    """
    for r in results:
        if r["similarity"] < min_similarity:
            continue
        if (r.get("character") or "").strip():
            return True
        if (r.get("material") or "").strip().lower() not in IDENTITY_NOISE:
            return True
    return False


# 「角色特征」那一行：视觉模型按结构化格式输出后，直接抽这一行当搜索线索
FEATURE_RE = re.compile(r"角色特征\s*[:：]\s*(.+)")
# 视觉模型自己给的「关键词」行（3-4 个最能区分角色的外貌词）——首选
KEYWORD_RE = re.compile(r"关键词\s*[:：]\s*(.+)")
# 抽不到结构化输出时，从散文里剔除的场景噪声词
SCENE_NOISE = ("背景是", "背景", "场景是", "场景", "画面", "氛围", "阳光",
               "街道", "整体", "色调", "给人", "充满", "治愈")
# 区分角色最强的是发色与瞳色，启发式优先保留含这些字的片段
HAIR_EYE_KEYS = ("发", "髮", "瞳", "眼", "眸")


def extract_features(desc: str, limit: int = 24) -> str:
    """抽出「最明显的特征」，压到 limit 字以内，当第 2 跳的搜索线索。

    为什么要短：搜索词越长越糊。把「少女银白长发侧编麻花辫，蓝色双瞳，
    穿黑色夹克缀粉红饰带，头顶光环」整段丢进去，不如「银白长发 蓝瞳」——
    真正能区分角色的就是发色和瞳色这几个字。

    取法优先级：
      1. 模型自己给的「关键词：」行（模型比正则更懂哪几个词最identifying）
      2. 「角色特征：」行里含 发/瞳/眼 的片段（发色瞳色最优）
      3. 整行按 limit 截断
    """
    if not desc:
        return ""

    m = KEYWORD_RE.search(desc)
    if m:
        line = m.group(1).strip()
        return " ".join(line.replace("，", " ").replace("、", " ").split())[:limit]

    m = FEATURE_RE.search(desc)
    line = m.group(1).strip() if m else desc.split("\n")[0]

    # 按标点/空格切分成独立特征片段
    parts = [p.strip(" ，。、,　") for p in re.split(r"[，,、;；\s]+", line) if p.strip()]
    if not parts:
        return line.strip()[:limit]

    key = [p for p in parts if any(k in p for k in HAIR_EYE_KEYS)]
    rest = [p for p in parts if p not in key]

    out = ""
    for p in key + rest:
        cand = (out + " " + p).strip()
        if len(cand) > limit:
            break
        out = cand
    return out or line.strip()[:limit]


def clean_work(s: str, limit: int = 30) -> str:
    """作品名清洗：搜索引擎对括号、全角标点不友好。

    '[CRYSTALiA] RE：D Cherish！ (Event CG)' → 'CRYSTALiA RE:D Cherish Event CG'
    保留方括号里的内容（常常是厂牌名，能帮搜索定位），只把括号本身换成空格。
    """
    if not s:
        return ""
    s = re.sub(r"[\[\]（）()【】]", " ", s)
    s = s.replace("：", ":").replace("　", " ")
    s = re.sub(r"[!！?？。，、·—]+", " ", s)
    s = " ".join(s.split())
    if len(s) > limit:
        # 按词边界截断，别把 "Event CG" 切成 "Event C"
        s = s[:limit].rsplit(" ", 1)[0] or s[:limit]
    return s.strip()


def build_twohop_query(res: dict, description: str, cfg: dict) -> str:
    """第 2 跳的 query：『作品 + 角色特征』。

    为什么不是「画师」：画师只说明【谁画的】，不说明【画的是谁】。
    实测用画师名 "usume shirou" 只能查到他本人的主页（CG 原画家），
    永远查不出角色名；而「作品名 + 发色/瞳色/服装」才直接指向具体角色，
    例如「RE:D Cherish 白发 蓝眼 水手服 角色」→ 该作的女主角是谁。
    """
    suffix = cfg.get("twohop_suffix", " 角色")
    top = (res.get("results") or [None])[0] if res.get("ok") else None

    work = ""
    if top:
        mat = (top.get("material") or "").strip()
        if mat.lower() not in IDENTITY_NOISE:
            work = clean_work(mat)
        if not work:
            title = (top.get("title") or "").strip()
            if title.lower() not in IDENTITY_NOISE:
                work = clean_work(title)

    feats = extract_features(description)

    parts = [p for p in (work, feats) if p]
    if not parts and top:
        # 既没作品也没特征，只能退回画师（聊胜于无）
        parts = [clean_work(top.get("author") or "")]
    parts = [p for p in parts if p]
    if not parts:
        return build_fallback_query(description, cfg)

    return (" ".join(parts)[:70].rstrip() + suffix).strip()


# ============================================================
# 独立入口：python image_search.py 图片路径或URL [--debug]
# ============================================================

def _read_image(arg: str) -> tuple[bytes, str]:
    if arg.startswith("http://") or arg.startswith("https://"):
        req = urllib.request.Request(arg, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), "URL"
    p = Path(arg)
    return p.read_bytes(), str(p)


def main() -> int:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv

    if not args:
        print(__doc__)
        print("用法: python image_search.py <图片路径|图片URL> [--debug]")
        return 1

    cfg = get_saucenao_config()
    print("=== 配置 ===")
    print(f"  enabled        : {cfg['enabled']}")
    print(f"  api_key        : {'已配置 (' + cfg['api_key'][:4] + '…)' if cfg['api_key'] else '（未配置，走匿名低限额）'}")
    print(f"  db / numres    : {cfg['db']} / {cfg['numres']}")
    print(f"  min_similarity : {cfg['min_similarity']}")
    print(f"  节流           : 间隔≥{cfg['min_interval_seconds']:.0f}s，每日≤{cfg['daily_limit']}次")
    print()

    try:
        data, src = _read_image(args[0])
    except Exception as e:
        print(f"❌ 读取图片失败: {type(e).__name__}: {e}")
        return 1

    print(f"=== 搜索 {src} ({len(data)/1024:.0f} KB) ===")
    t0 = time.time()
    res = saucenao_search(data, cfg, debug=debug)
    dt = time.time() - t0

    print(f"耗时 {dt:.2f}s")
    print(f"ok={res['ok']}  {res['reason']}")
    if res.get("limits"):
        print(f"额度: {res['limits']}")

    if not res["ok"]:
        return 2

    print(f"\n返回 {len(res['results'])} 条结果：")
    for i, r in enumerate(res["results"], 1):
        print(f"\n[{i}] 相似度 {r['similarity']:.2f}%  图库={r['index_name']}(id={r['index_id']})")
        print(f"    角色 : {r['character'] or '(无)'}")
        print(f"    作品 : {r.get('material') or '(无)'}")
        print(f"    标题 : {r['title'] or '(无)'}")
        print(f"    作者 : {r['author'] or '(无)'}")
        print(f"    链接 : {r['urls'][:2] if r['urls'] else '(无)'}")
        if debug and r.get("data_keys"):
            print(f"    原始字段: {r['data_keys']}")

    text = format_results(res, cfg["min_similarity"])
    print("\n=== 会给聊天模型看的内容 ===")
    print(text if text else "(没有达到相似度门槛的结果)")

    if debug and res.get("raw"):
        print("\n=== 原始 JSON ===")
        print(json.dumps(res["raw"], ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
