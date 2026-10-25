"""Readable role labels and lossless indexing of legacy #U filenames."""
import hashlib
from pathlib import Path
import re


def decoded(stem):
    text = re.sub(r'#[Uu]([0-9a-fA-F]{4})', lambda match: chr(int(match[1], 16)), stem)
    try:
        text = text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le')
    except UnicodeError:
        return stem
    return text if text and not any(c in text for c in '/\\:\x00\n\r') and text not in ('.', '..') else stem


def character_name(stem, text):
    """Read explicit role declarations; never invent a name from a clipped label."""
    text = text.replace('\\n', '\n').replace('\\t', '\t')
    patterns = (
        r'(?:扮演|模仿)(?:一[名个位])?(?:名?叫(?:做)?)[“「『"]?([\u4e00-\u9fff·]{1,24}?)(?=[”」』"的，,。])',
        r'角色[“「『"]([\u4e00-\u9fff·]{1,24})[”」』"]',
        r'角色[—\-：:\s]+([\u4e00-\u9fff·]{1,24})(?=[（(，,。\n])',
        r'(?:人物名称|角色名称|你的名字叫做|你的名字叫|姓名)\s*[：:]?\s*[“「『"]?([\u4e00-\u9fff·]{1,24}?)(?=[”」』"\s，,。]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, text[:12000])
        if match:
            return match[1]
    return decoded(stem)


def available(root):
    directory = Path(root) / 'prompts'
    paths = sorted((p for p in directory.glob('*.txt') if not p.stem.startswith('_') and not p.is_symlink()),
                   key=lambda p: (bool(re.search(r'#[Uu][0-9a-fA-F]{4}', p.stem)), p.name))
    result, seen = {}, set()
    for path in paths:
        body = path.read_bytes()
        name = character_name(path.stem, body.decode('utf-8-sig'))
        signature = (name, hashlib.sha256(body).hexdigest())
        if signature in seen:
            continue
        seen.add(signature)
        label, number = name, 2
        while label in result:
            label = f'{name}（同名副本{number}）'
            number += 1
        result[label] = path
    return result


def resolve(root, marker):
    styles = available(root)
    if marker in styles:
        return marker, styles[marker]
    if not marker or marker in ('.', '..') or any(c in marker for c in '/\\:\x00\n\r'):
        return None
    # Match old markers by actual filename, including encoded and clipped ones.
    path = Path(root) / 'prompts' / (marker + '.txt')
    if path.is_file() and not path.is_symlink():
        body = path.read_bytes()
        for name, candidate in styles.items():
            if candidate == path or candidate.read_bytes() == body:
                return name, candidate
    alias = decoded(marker)
    for name, candidate in styles.items():
        if decoded(candidate.stem) == alias:
            return name, candidate
    return None
