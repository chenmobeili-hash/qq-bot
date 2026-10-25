"""Explicit, bounded group paper reading; no account cookies or conversation history."""
import asyncio
from collections import OrderedDict
from html.parser import HTMLParser
import io
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

MAX_BYTES = 64 * 1024 * 1024
MAX_CHARS = 240_000
MAX_PAGES = 100
CONTEXT_CHARS = 24_000
EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".docx", ".doc"}
HELP = ("群里读文献：先上传 PDF/TXT/Markdown/DOCX（旧DOC需antiword），再 @我 读文献；也可以先 @我 读文献，再上传。\n"
        "自主挑选：@我 挑一份你感兴趣的文档读，从本群文件挑一篇后阅读。\n"
        "链接：@我 读文献 https://arxiv.org/abs/1706.03762 解释方法与局限\n"
        "追问：@我 文献追问 实验如何设计？\n状态：@我 文献状态；清除：@我 忘记文献。\n"
        "每人每群分别保留最近一篇，1小时后过期，重启不保留。正文会交给当前配置的聊天模型分析。")


class PaperError(Exception):
    pass


def clean_label(value, limit=180):
    return re.sub(r"[\r\n\x00-\x1f]|\[CQ:[^\]]*\]", " ", str(value)).strip()[:limit]


def is_document_followup(text):
    if not text or len(text) > 1500:
        return False
    return bool(re.search(
        r"(?:这|那|刚才那|刚刚那|你刚读的|刚读的)(?:一)?(?:篇|份|本)?(?:文章|论文|文献|文档|研究|书籍|书)"
        r"|(?:这|那)(?:一)?(?:篇|份|本)(?:怎么样|如何|呢|有什么|有啥|好看|值得)"
        r"|第\s*\d+\s*页.*(?:什么|怎么|解释|问题|假设|结论|意思)"
        r"|(?:文中|作者).*(?:方法|实验|结论|假设|证据|局限)", text))


def public_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme not in ("https", "http") or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, 80, 443)
                or any(ord(c) < 33 for c in url) or "\\" in url):
            raise ValueError()
        if host.lower() == "localhost" or host.lower().endswith((".local", ".localhost")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    except (ValueError, TypeError):
        raise PaperError("只支持不含账号密码的公网 HTTP/HTTPS 链接（80/443端口）。") from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the actual addresses used by the connector, including after redirects."""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            rows = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM, family=family)
        except OSError:
            raise PaperError("无法解析文献网址，请检查网络。") from None
        if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
            raise PaperError("文献地址解析到了非公网地址，已停止读取。")
        return [{"hostname": host, "host": row[4][0], "port": port,
                 "family": row[0], "proto": row[2], "flags": socket.AI_NUMERICHOST}
                for row in rows]

    async def close(self):
        pass


async def fetch_bytes(url):
    from paper_download import fetch_document
    return await fetch_document(url, resolver_factory=PublicResolver,
        validate_url=public_url, error=PaperError, max_bytes=MAX_BYTES)


class PaperHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.in_title = False
        self.title = []
        self.parts = []
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = attrs.get("name", attrs.get("property", "")).lower()
            self.meta[key] = attrs.get("content", "")
        if tag in ("p", "div", "h1", "h2", "h3", "br", "li", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.hidden:
            self.hidden -= 1
        if tag == "title":
            self.in_title = False
        if tag in ("p", "div", "section"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.hidden:
            self.parts.append(data)


def decode_text(body):
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise PaperError("无法识别文字编码，请转为 UTF-8 后重试。")


def extract_document(body, filename, source="群文件"):
    """Runs in an isolated short-lived worker for PDF parsing."""
    if len(body) > MAX_BYTES:
        raise PaperError(f"文献超过{MAX_BYTES // (1024 * 1024)} MB。")
    suffix = Path(filename).suffix.lower()
    title = clean_label(filename)
    units, used, total, truncated = [], 0, 0, False
    if suffix == ".pdf" or body[:1024].lstrip().startswith(b"%PDF-"):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(body))
            if reader.is_encrypted:
                raise PaperError("PDF 有密码保护，请先解锁后上传。")
            total = len(reader.pages)
            for index, page in enumerate(reader.pages):
                if index >= MAX_PAGES or used >= MAX_CHARS:
                    truncated = True
                    break
                text = (page.extract_text() or "").strip()
                room = MAX_CHARS - used
                if len(text) > room:
                    truncated = True
                text = text[:room]
                used += len(text)
                if text:
                    units.append({"label": f"第{index + 1}页", "text": text})
        except PaperError:
            raise
        except Exception:
            raise PaperError("PDF 损坏或无法解析，请换一个文件。") from None
        kind = "PDF文字；未识别图表画面"
    else:
        if suffix not in EXTENSIONS and suffix not in (".html", ".htm"):
            raise PaperError("支持 PDF、TXT、Markdown、Word DOCX（旧DOC需antiword），或公开论文网页。")
        word_truncated = False
        if suffix in ('.docx', '.doc'):
            import word_reader
            try:
                text = word_reader.extract(body, suffix)
                word_truncated = len(text) >= word_reader.TEXT_LIMIT
            except word_reader.WordError as exc:
                raise PaperError(str(exc)) from None
            kind = 'Word正文及表格文字；按段定位，未识别图片图表；未还原Word分页'
        else:
            text = decode_text(body)
            kind = "文字文件"
        if suffix in (".html", ".htm"):
            parser = PaperHTML()
            parser.feed(text)
            title = clean_label(parser.meta.get("citation_title") or "".join(parser.title) or filename)
            text = "".join(parser.parts)
            kind = "网页可见文字，不保证论文全文"
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        truncated = word_truncated or len(text) > MAX_CHARS
        text = text[:MAX_CHARS]
        units = [{"label": f"段{i // 1800 + 1}", "text": text[i:i + 1800]}
                 for i in range(0, len(text), 1800)]
        total = len(units)
    if not units or sum(len(re.sub(r"\s", "", u["text"])) for u in units) < 40:
        raise PaperError("没有提取到足够正文；可能是扫描件，请先 OCR，或提供带文字层的 PDF。")
    return {"title": title, "source": source, "kind": kind, "units": units,
            "total": total, "truncated": truncated}


async def extract_in_worker(body, filename, source):
    with tempfile.TemporaryDirectory(prefix="qq-paper-") as temp:
        path = Path(temp) / ("paper" + Path(filename).suffix.lower())
        await asyncio.to_thread(path.write_bytes, body)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), "--extract", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise PaperError("文献解析超时，请拆分文件后再试。") from None
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise PaperError("文献解析失败，请检查文件或运行环境。") from None
        if "error" in result:
            raise PaperError(result["error"])
        result["title"], result["source"] = clean_label(filename), source
        return result


def arxiv_pdf(url):
    parts = urlsplit(url)
    if parts.hostname not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        return None
    match = re.fullmatch(r"/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)(?:\.pdf)?/?", parts.path)
    return "https://arxiv.org/pdf/" + match[1] if match else None


async def read_link(url):
    url = public_url(url)
    preferred = arxiv_pdf(url)
    try:
        body, content_type, final_url = await fetch_bytes(preferred or url)
    except (PaperError, aiohttp.ClientError, asyncio.TimeoutError):
        if not preferred or preferred == url:
            raise
        body, content_type, final_url = await fetch_bytes(url)
    filename = Path(urlsplit(final_url).path).name or "论文"
    if body[:1024].lstrip().startswith(b"%PDF-"):
        doc = await extract_in_worker(body, filename + ".pdf", url)
    elif "html" in content_type.lower() or body.lstrip().startswith((b"<!DOCTYPE", b"<html", b"<!doctype")):
        parser = PaperHTML()
        parser.feed(decode_text(body))
        pdf_url = parser.meta.get("citation_pdf_url", "")
        if pdf_url:
            try:
                pdf_body, _, _ = await fetch_bytes(urljoin(final_url, pdf_url))
                if pdf_body[:1024].lstrip().startswith(b"%PDF-"):
                    return await extract_in_worker(pdf_body, clean_label(parser.meta.get("citation_title") or filename) + ".pdf", url)
            except (PaperError, aiohttp.ClientError, asyncio.TimeoutError):
                pass
        if preferred:
            abstract = parser.meta.get("citation_abstract") or parser.meta.get("description") or parser.meta.get("og:description")
            if not abstract:
                raise PaperError("只拿到论文页面，未读到正文或摘要；请直接上传PDF。")
            doc = extract_document(abstract.encode(), "摘要.txt", url)
            doc["kind"] = "仅网页摘要，未读到论文全文"
            doc["title"] = clean_label(parser.meta.get("citation_title") or "".join(parser.title) or filename)
        else:
            doc = extract_document(body, "论文网页.html", url)
    else:
        if Path(filename).suffix.lower() not in EXTENSIONS:
            raise PaperError("链接没有返回 PDF 或可读论文网页，请上传论文文件。")
        doc = await extract_in_worker(body, filename, url)
    return doc


def build_context(doc, question=""):
    units = doc["units"]
    # Split large pages as well, keeping the same printed PDF page locator.
    blocks = [{"label": u["label"], "text": u["text"][i:i + 1800]}
              for u in units for i in range(0, len(u["text"]), 1800)]
    capacity = max(1, CONTEXT_CHARS // 1900)
    if len(blocks) <= capacity:
        selected = list(range(len(blocks)))
    elif question.strip():
        terms = set(re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2}", question.lower()))
        def score(index):
            block = blocks[index]
            text = block["text"].lower()
            page_match = re.search(r"第?\s*(\d+)\s*页", question)
            return sum(min(text.count(term), 8) for term in terms) + (100 if page_match and block["label"] == f"第{page_match[1]}页" else 0)
        selected = sorted(sorted(range(len(blocks)), key=lambda i: (-score(i), i))[:capacity])
    else:
        selected = sorted({round(i * (len(blocks) - 1) / (capacity - 1)) for i in range(capacity)})
    excerpts = [blocks[i] for i in selected]
    coverage = {"kind": doc["kind"], "total_pages_or_paragraphs": doc["total"],
                "extraction_truncated": doc["truncated"], "selected_blocks": len(selected),
                "all_blocks": len(blocks), "sampled": len(selected) < len(blocks),
                "locators": list(dict.fromkeys(b["label"] for b in excerpts))}
    return {"title": doc["title"], "coverage": coverage, "excerpts": excerpts}


class PaperLibrary:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.docs = OrderedDict()
        self.shared_docs = OrderedDict()
        self.ignored_shared = OrderedDict()
        self.files = OrderedDict()
        self.waits = OrderedDict()
        self.busy = set()
        self.last = OrderedDict()
        self.versions = {}

    def _prune(self):
        now = self.clock()
        for store in (self.docs, self.shared_docs, self.ignored_shared, self.files, self.waits, self.last):
            for key in list(store):
                if store[key][0] <= now:
                    store.pop(key, None)

    def _put(self, store, key, value, ttl, capacity):
        self._prune()
        store.pop(key, None)
        store[key] = (self.clock() + ttl, value)
        while len(store) > capacity:
            store.popitem(last=False)

    def get(self, store, key):
        self._prune()
        return store.get(key, (0, None))[1]

    def remember_file(self, key, info):
        name = clean_label(info.get("name") or info.get("file_name") or info.get("filename") or "")
        if Path(name).suffix.lower() not in EXTENSIONS:
            return False
        data = {k: info[k] for k in ("id", "file_id", "busid", "bus_id", "size", "file_size", "url") if k in info}
        data["name"] = name
        self._put(self.files, key, data, 600, 64)
        return True

    def group_files(self, group_id):
        self._prune()
        return [value[1] for key, value in self.files.items() if key[0] == str(group_id)]

    def store_doc(self, key, doc):
        self._put(self.docs, key, doc, 3600, 16)

    def store_shared(self, group_id, doc):
        self._put(self.shared_docs, str(group_id), doc, 3600, 8)

    def current_doc(self, key):
        self._prune()
        own = self.docs.get(key)
        shared = self.shared_docs.get(str(key[0]))
        ignored = self.ignored_shared.get(key)
        if shared and (not ignored or ignored[1] != shared[0]) and (not own or shared[0] > own[0]):
            return shared[1]
        return own[1] if own else None

    def forget(self, key):
        shared = self.shared_docs.get(str(key[0]))
        if shared:
            self._put(self.ignored_shared, key, shared[0], max(1, shared[0] - self.clock()), 128)
        for store in (self.docs, self.files, self.waits):
            store.pop(key, None)
        if key in self.busy:
            self.versions[key] = self.versions.get(key, 0) + 1

    def acquire(self, key):
        self._prune()
        if key in self.busy or len(self.busy) >= 2:
            raise PaperError("文献阅读正在处理中，请稍后再试。")
        if self.get(self.last, key):
            raise PaperError("请间隔15秒再发起下一次文献阅读。")
        self.busy.add(key)
        self._put(self.last, key, True, 15, 128)
        return self.versions.get(key, 0)

    def release(self, key):
        self.busy.discard(key)
        self.versions.pop(key, None)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--extract":
    # Parser limits are additional to the parent process's wall-clock deadline.
    try:
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
        path = Path(sys.argv[2])
        result = extract_document(path.read_bytes(), path.name)
    except Exception as exc:
        result = {"error": str(exc) if isinstance(exc, PaperError) else "文献解析失败，请检查文件或解析依赖。"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
