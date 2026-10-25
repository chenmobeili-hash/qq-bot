"""第 3 跳取数：直接抓作品的百科页正文。

为什么要做这一跳
  第 1 跳（SauceNAO）只认得出「这是哪部作品」，图库元数据里通常没有角色名。
  第 2 跳（网页搜索）拿到的摘要只覆盖页面开头，而角色信息往往在页面中后段，
  所以搜索几乎拿不到角色名。
  第 3 跳直接读那一页的正文 —— 实测这正是唯一能稳定拿到角色名的一跳。
  谷歌/百度识图看起来聪明，靠的是又大又对的索引；我们没有索引，
  但没必要爬全网，精准地只读那一页更快更准。

两道防错闸门（不做这俩就会给出自信的错误答案）
  1. 页面身份校验：抓回的正文里必须真的出现作品名。
     反例（实测踩过的坑）：Bangumi API 搜索「RE:D Cherish!」时返回的是
     RE:VICE[D] 等无关条目，直接拿 id 取角色就会把另一部作品的角色名当成答案。
  2. 导航模板过滤：百科页底部常有「本品牌其它作品的登场角色」导航块，
     形如「主要角色 甲 • 乙 • 丙」。那些是**别的作品**的角色，必须剔除，
     否则会把无关角色的名字喂给模型。
"""
from __future__ import annotations

import gzip
import io
import re
import urllib.error
import urllib.parse
import urllib.request

# 百科站点的标题路径模板；按优先级尝试
PAGE_TEMPLATES = [
    "https://zh.moegirl.org.cn/{title}",
    "https://mzh.moegirl.org.cn/{title}",
]

# 非浏览器 UA 在很多站点直接 403，实测必须用浏览器 UA
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 摘录时优先保留的行（剧情简介、角色、CAST、制作信息）
SIGNALS = ("剧情", "简介", "角色", "CAST", "CV", "主角", "女主", "登场",
           "主要", "原画", "开发", "发行", "原名", "类型", "制作", "编剧", "音乐")

# 导航模板行：形如「主要角色 甲 • 乙 • 丙」—— 是别的作品的角色，必须剔除
NAV_TPL = re.compile(r"^\s*(主要角色|登场角色|其他角色|Staff|.*角色)\s*.+•")

_TAG_DROP = re.compile(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>",
                       re.S | re.I)
_BLOCK = re.compile(r"</(p|div|li|tr|h[1-6]|dd|dt)>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def extract_title(html: str) -> str | None:
    """从原始 HTML 里取 <title>。比正文归一化更可靠，
    因为正文会包含各种无关的词（比如歌曲页里有"原创"这个词），
    而页面标题就是条目本身的正名。"""
    m = _TITLE.search(html)
    if not m:
        return None
    # 萌娘百科的 <title> 格式通常是 "条目名 - 萌娘百科 万物皆可萌的百科全书"
    t = m.group(1).strip()
    t = re.sub(r"\s*[-—\-]\s*.*$", "", t)  # 截断到第一个分隔符
    return t.strip()


def norm(s: str) -> str:
    """归一化用于身份比对：只留字母数字和 CJK，小写。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff]", "", (s or "").lower())


def identity_match(page_title: str | None, body: str | None, cand: str) -> bool:
    """两道闸门：**优先**用页面标题做身份比对；失败则退回全文子串。

    为什么用标题：正文出现"original"这类通用词的概率极高，
    全文子串比对假阳性太高。而页面的 `<title>` 几乎就是条目的正名。
    比对方向：页标题包含候选词，或候选词包含页标题（都算命中）。
    """
    n_page = norm(page_title) if page_title else ""
    n_cand = norm(cand)
    if n_page and n_cand:
        if n_page in n_cand or n_cand in n_page:
            return True
    if n_cand and body and n_cand in norm(body):
        return True
    return False


def _bracket_variants(s: str) -> list[str]:
    """从图库标题剥离品牌前缀/装饰后缀，生成一批候选写法。

    图库标题的典型格式是「品牌 真名 说明」，只有中间那段是百科条目名：
        [CRYSTALiA] RE：D Cherish！ (Event CG)
         ↑ 品牌     ↑ 条目名          ↑ 说明
    直接拿整串当页面标题会 404；只去掉某一段又可能还带残余，
    所以把「去首/去尾/去首尾」四种组合都试一遍。
    """
    left, right = r"[\[\(【「]", r"[\]\)】」]"
    first = re.compile(rf"^\s*{left}\s*[^\]\)】」]+?\s*{right}\s*")
    last = re.compile(rf"\s*{left}\s*[^\]\)】」]+?\s*{right}\s*$")

    a = first.sub("", s).strip()          # 去首：去品牌前缀
    b = last.sub("", s).strip()           # 去尾：去说明后缀
    c = last.sub("", a).strip() if a else ""   # 去首尾（去首再去尾）
    d = first.sub("", b).strip() if b else ""  # 去尾首（去尾再去首）
    bare = re.sub(r"[\[\]【】（）()]", "", s).strip()   # 全剥
    opened = re.sub(r"[\[\]【】（）()]", " ", s).strip()  # 括号换空格

    out: list[str] = []
    for v in (a, b, c, d, bare, opened):
        v = re.sub(r"\s+", " ", v).strip()
        if v and v not in out:
            out.append(v)
    return out


def build_candidates(material: str, title: str) -> list[str]:
    """由搜图得到的「作品/标题」生成一批候选标题。

    搜图返回的标题常带装饰性括号，例如 `[CRYSTALiA] RE：D Cherish！ (Event CG)`。
    直接拿它当页面标题会 404，所以要生成多种写法。
    全角冒号/感叹号统一成半角，因为百科条目标题用的是半角。

    先过滤无信息量的通用词（original 这类词在别的页面也会出现，
    拿去当标题会命中完全不相干的内容——实测抓到过《亚人酱有话要说》的歌曲页）。
    """
    noise = {"original", "unknown", "n/a", "none", "original work", "fanart",
             "illustration", "drawing", "artwork", "scanned", "untitled",
             "无题", "原创", "插画", "同人", "草图", "练习"}
    out: list[str] = []
    seen: set[str] = set()
    for raw in (material, title):
        raw = re.sub(r"\s+", " ", (raw or "").strip())
        if not raw:
            continue
        for cand in _bracket_variants(raw):
            cand = cand.replace("：", ":").replace("！", "!").replace("，", " ").strip()
            if not cand or cand in seen or cand.lower() in noise:
                continue
            seen.add(cand)
            out.append(cand)
    # 短标题更可能是条目的正名，优先尝试
    return sorted(out, key=len)


def fetch_html(url: str, timeout: int = 15) -> str | None:
    """抓一个网页并还原文本。返回 None 表示抓取失败。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            ct = r.headers.get("Content-Type") or ""
            m = re.search(r"charset=([\w-]+)", ct)
            enc = m.group(1) if m else "utf-8"
            return raw.decode(enc, "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def html_to_text(html: str) -> str:
    """粗提纯：去脚本/样式/导航块，块级标签换行，压缩空白。"""
    text = _TAG_DROP.sub(" ", html)
    text = _BLOCK.sub("\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())


def build_excerpt(text: str, max_chars: int = 1600) -> str:
    """从全文里摘出真正属于「本作」的正文片段，供模型判断。

    策略（针对萌娘百科页面结构的实测）：
      1. 从「剧情简介/故事简介」小节开始 —— 前面的只是条目头部的信息框；
      2. 到「相关作品/参见/注释」小节截止 —— 之后全是**别的作品**的
         音乐/角色导航块（比如 CRYSTALiA 其它游戏的 OP 列表），是噪音；
      3. 在 [起, 止) 区间里保留全部短行（剧情简介正文 + CAST 行
         「角色名：声优」+ 角色小节），剔除音乐类噪音行。

    为什么不全页保留：既控制 token，也避免把无关作品的角色名喂给模型
    导致它「自信地答错」（比如把另一部游戏的角色当成答案）。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # —— 定位正文区间 ——
    start = 0
    for i, ln in enumerate(lines):
        if len(ln) <= 16 and ("剧情简介" in ln or "故事简介" in ln or ln in ("简介", "故事")):
            start = i
            break
    end = len(lines)
    for j in range(start, len(lines)):
        ln = lines[j]
        if len(ln) <= 16 and any(k in ln for k in ("相关作品", "参见", "注释", "参考资料",
                                                   "外部链接", "导航", "模板", "分类")):
            end = j
            break

    body = lines[start:end]

    # —— 从正文里挑行 ——
    music_noise = re.compile(r"^(游戏音乐|相关音乐|动画版音乐|创作相关|音乐|OP|ED|TM|OST|插曲)")
    picked: list[str] = []
    for ln in body:
        if len(ln) > 160:                          # 超长行通常是脚本残留
            continue
        if NAV_TPL.match(ln):                      # 导航模板（其他作品的角色列表）
            continue
        if music_noise.match(ln):                  # 音乐/制作导航行
            continue
        if ln.startswith(("相关作品", "参见", "注释", "导航", "模板")):
            continue
        picked.append(ln)

    seen: set[str] = set()
    buf = ""
    for ln in picked:
        if ln in seen:
            continue
        seen.add(ln)
        if len(buf) + len(ln) > max_chars:
            break
        buf += ln + "\n"
    return buf.strip()


def find_work_page(candidates: list[str], timeout: int = 12) -> dict | None:
    """按候选标题逐个抓百科页，通过两道闸门后返回 {url, title, excerpt, tried}。"""
    tried: list[str] = []
    for cand in candidates:
        title = urllib.parse.quote(cand)
        for tpl in PAGE_TEMPLATES:
            url = tpl.format(title=title)
            html = fetch_html(url, timeout=timeout)
            if html is None:
                continue
            text = html_to_text(html)
            tried.append(f"{url} ({len(text)}字)")

            # 闸门 1：页面身份校验（优先比对 <title>，比全文子串可靠得多）
            if not identity_match(extract_title(html), text, cand):
                continue

            # 闸门 2：在 build_excerpt 里剔除品牌导航模板（别的作品的角色）
            excerpt = build_excerpt(text)
            if not excerpt:
                continue

            return {"url": url, "title": cand, "excerpt": excerpt, "tried": tried}

    return None
