"""Read bounded Word text without opening Office, extracting ZIPs, or fetching links."""
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

XML_LIMIT = 16 * 1024 * 1024
EXPANDED_LIMIT = 128 * 1024 * 1024
TEXT_LIMIT = 240_001
WORD_NS = {'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
           'http://purl.oclc.org/ooxml/wordprocessingml/main'}


class WordError(ValueError):
    pass


def tag(node):
    return node.tag.rsplit('}', 1)[-1]


def text_nodes(node):
    name = tag(node)
    if name in ('del', 'moveFrom', 'instrText', 'delText', 'drawing', 'pict', 'object', 'altChunk'):
        return
    if name == 'r' and any(tag(n) == 'vanish' for n in node.iter()):
        return
    if name == 't' and node.text:
        yield node.text
    elif name == 'tab':
        yield '\t'
    elif name in ('br', 'cr'):
        yield '\n'
    else:
        for child in node:
            yield from text_nodes(child)
    if name in ('p', 'tr'):
        yield '\n'
    elif name == 'tc':
        yield '\t'


def read_docx(body):
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            names = [info.filename for info in entries]
            if len(entries) > 4096 or len(set(names)) != len(names):
                raise WordError('Word压缩包条目过多或重名')
            if sum(info.file_size for info in entries) > EXPANDED_LIMIT:
                raise WordError('Word解压后体积超过128MB，请拆分文档')
            if any(info.flag_bits & 1 or PurePosixPath(info.filename).is_absolute()
                   or '..' in PurePosixPath(info.filename).parts for info in entries):
                raise WordError('Word文件受保护或包含无效条目，请另存为普通DOCX')
            if 'word/document.xml' not in names:
                raise WordError('不是有效的Word DOCX文件')
            parts, used = [], 0
            for filename in ('word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml'):
                if filename not in names:
                    continue
                if archive.getinfo(filename).file_size > XML_LIMIT:
                    raise WordError('Word正文XML超过16MB，请拆分文档')
                raw = archive.read(filename)
                if re.search(rb'<!DOCTYPE|<!ENTITY', raw.replace(b'\x00', b''), re.I):
                    raise WordError('Word XML包含不支持的实体声明')
                root = ET.fromstring(raw)
                ns = root.tag[1:].split('}', 1)[0] if root.tag.startswith('{') else ''
                if ns not in WORD_NS:
                    raise WordError('Word正文命名空间无效')
                nodes = [root] if filename.endswith('document.xml') else [
                    n for n in root if not any(k.endswith('}type') and v in ('separator', 'continuationSeparator') for k, v in n.attrib.items())]
                if filename != 'word/document.xml':
                    parts.append('\n[脚注或尾注]\n')
                for node in nodes:
                    for piece in text_nodes(node):
                        room = TEXT_LIMIT - used
                        if room <= 0:
                            return ''.join(parts)
                        parts.append(piece[:room])
                        used += min(len(piece), room)
            return ''.join(parts)
    except WordError:
        raise
    except (zipfile.BadZipFile, KeyError, RuntimeError, ET.ParseError, ValueError, OSError, RecursionError):
        raise WordError('Word DOCX损坏、加密或无法解析，请另存为DOCX后重试') from None


def read_doc(body):
    if not body.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        raise WordError('旧版DOC格式无效；请在Word中另存为DOCX或PDF后上传')
    program = shutil.which('antiword')
    if not program:
        raise WordError('旧版DOC需要安装antiword解析器；可先另存为DOCX或PDF，DOCX无需额外组件')
    with tempfile.TemporaryDirectory(prefix='qq-word-') as temporary:
        source, output = Path(temporary) / 'input.doc', Path(temporary) / 'text.txt'
        source.write_bytes(body)
        source.chmod(0o600)
        try:
            options = {}
            if os.name == 'posix':
                def limit_output():
                    import resource
                    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
                options['preexec_fn'] = limit_output
            with output.open('wb') as stream:
                result = subprocess.run([program, '-m', 'UTF-8.txt', str(source)], stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.DEVNULL, timeout=45, check=False, **options)
            if result.returncode != 0 or output.stat().st_size > 8 * 1024 * 1024:
                raise WordError('旧版DOC无法解析或正文过大，请另存为DOCX或PDF')
            return output.read_text(encoding='utf-8', errors='strict')[:TEXT_LIMIT]
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise WordError('旧版DOC解析失败或超时，请另存为DOCX或PDF') from None


def extract(body, suffix):
    if suffix == '.docx':
        return read_docx(body)
    if suffix == '.doc':
        return read_doc(body)
    raise WordError('不支持此Word文件格式')
