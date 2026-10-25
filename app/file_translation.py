"""Plain-text extraction and chunked document translation for private QQ files."""

import asyncio
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree


SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv",
    ".srt", ".vtt", ".xml", ".log", ".docx", ".pdf", ".epub",
}


class TranslationError(Exception):
    pass


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "h1", "h2", "h3", "h4", "li", "br", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "h1", "h2", "h3", "h4", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise TranslationError("无法识别文件编码，请转成 UTF-8 后重试。")


def _html_text(content: str) -> str:
    parser = _TextHTMLParser()
    parser.feed(content)
    return parser.text()


def extract_text(content: bytes, filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise TranslationError("不支持这种文件格式。支持 TXT、Markdown、HTML、JSON、CSV、字幕、DOCX、PDF 和 EPUB。")

    if extension == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise TranslationError("PDF 有密码保护，请先解锁后重试。")
        parts = [f"\n\n--- 第 {i} 页 ---\n\n{page.extract_text() or ''}"
                 for i, page in enumerate(reader.pages, 1)]
        text = "".join(parts).strip()
        if not re.search(r"\w", re.sub(r"--- 第 \d+ 页 ---", "", text)):
            raise TranslationError("PDF 没有可提取的文字，可能是扫描件；请先 OCR 后重试。")
        return text

    if extension == ".docx":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        parts = []
        for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            line = "".join(node.text or "" for node in paragraph.iter()
                           if node.tag in {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
                                           "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab"})
            if line.strip():
                parts.append(line)
        text = "\n".join(parts).strip()
    elif extension == ".epub":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > 1000:
                raise TranslationError("EPUB 包含过多文件，无法安全处理。")
            package = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            rootfile = package.find(".//{*}rootfile")
            if rootfile is None:
                raise TranslationError("EPUB 缺少目录信息。")
            opf_path = rootfile.attrib["full-path"]
            opf = ElementTree.fromstring(archive.read(opf_path))
            manifest = {item.attrib.get("id"): item.attrib.get("href")
                        for item in opf.findall(".//{*}manifest/{*}item")}
            opf_dir = str(Path(opf_path).parent).replace("\\", "/")
            parts = []
            for item in opf.findall(".//{*}spine/{*}itemref"):
                href = manifest.get(item.attrib.get("idref"))
                if href:
                    member = (opf_dir + "/" + href).lstrip("./")
                    raw = archive.read(member)
                    if len(raw) > 8 * 1024 * 1024:
                        raise TranslationError("EPUB 章节过大，无法安全处理。")
                    parts.append(_html_text(raw.decode("utf-8-sig", errors="replace")))
            text = "\n\n".join(parts).strip()
    else:
        text = _decode_text(content).strip()
        if extension in {".html", ".htm"}:
            text = _html_text(text)

    if not text:
        raise TranslationError("文件里没有可翻译的文字。")
    return text


def split_text(text: str, chunk_size: int = 4000) -> list[str]:
    """Keep paragraph breaks where possible; never discard characters."""
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, chunk_size + 1)
        if cut < chunk_size // 2:
            cut = remaining.rfind("\n", 0, chunk_size + 1)
        if cut < chunk_size // 2:
            cut = chunk_size
        else:
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    return chunks


async def translate_chunks(client, model: str, chunks: list[str], target_language: str,
                           progress=None) -> str:
    system = (f"你是严谨的文献翻译助手。将原文翻译为{target_language}。"
              "保留段落、标题、列表、引用与页码标记。专有名词、公式、URL、代码和文献引用务必准确。"
              "只输出译文，不要解释或添加前后引号。")
    translated = []
    for index, chunk in enumerate(chunks, 1):
        last_error = None
        for attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": chunk}],
                    max_tokens=6000,
                    temperature=0.2,
                    timeout=180,
                )
                choice = response.choices[0]
                result = (choice.message.content or "").strip()
                if not result or choice.finish_reason == "length":
                    raise TranslationError("模型返回空内容或截断了译文。")
                translated.append(result)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        if last_error is not None:
            raise TranslationError(f"第 {index}/{len(chunks)} 段翻译失败：{last_error}")
        if progress:
            await progress(index, len(chunks))
    return "\n\n".join(translated) + "\n"
