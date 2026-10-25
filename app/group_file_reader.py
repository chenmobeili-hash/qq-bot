"""Bounded, group-scoped file metadata and deterministic reading intent matching."""
from collections import OrderedDict
import re
import time
import unicodedata

_COMMON = str.maketrans('書檔獻讀論體資時間範疇爾冊馬義學國終譯這個份簡綱總結請給',
                        '书档献读论体资时间范畴尔册马义学国终译这个份简纲总结请给')
_READ = re.compile(r'提要|摘要|概括|总结|梗概|简明介绍|读一(?:下|读)|看一(?:下|看)|阅读|解读|读懂|读完')


def normalized(value):
    value = unicodedata.normalize('NFKC', str(value)).translate(_COMMON).casefold()
    value = re.sub(r'\.(?:pdf|txt|md|markdown|docx|doc)\b', '', value)
    return ''.join(c for c in value if c.isalnum())


def plain(value):
    return re.sub(r'\[CQ:[^\]]*\]', '', str(value)).translate(_COMMON).strip()


def reading_request(text):
    text = plain(text)
    if re.search(r'不要|别(?:再|去)?(?:读|看)|不用|不必|如何.*(?:功能|指令)|怎么.*(?:功能|指令)', text):
        return False
    if text.startswith(('搜索', '查查', '查一下', '帮助', '主动模式', '主动阅读', '切换风格')) or re.search(r'总结.*(?:群聊|聊天|今天的事)|摘要(?:是什么|怎么写)|怎么(?:读|阅读)', text):
        return False
    return bool(_READ.search(text))


def reference(text):
    """Get a title from the actual request; never use it as a command or URL."""
    text = plain(text)
    bracket = re.search(r'《([^》]{1,500})》|[“「]([^”」]{1,500})[”」]', text)
    if bracket:
        return (bracket[1] or bracket[2]).strip()
    text = re.sub(r'^(?:请|麻烦|能否|能不能|可以|帮我|给我|给一下|提供|读文献|读论文|阅读|解读|读一下|看一下|一下|我|你|把|对|为|一下子|简要|再|重新|一下|一下这个|这个|的|\s)+', '', text)
    text = re.split(r'(?:的)?(?:简明)?(?:提要|摘要|概括|总结|梗概)|[，,。！？?]|\s+(?:重点|请|帮|介绍|分析|解释)', text, maxsplit=1)[0]
    return text.strip(' 的：:、!！?？。')


def demonstrative(text):
    return bool(re.search(r'这(?:一)?(?:份|篇|本|个)(?:文件|文档|文章|书|论文)?|此(?:文件|文档|书|论文)', plain(text)))


def match_files(query, files):
    """Exact filename first, normalized equality next, then conservative containment."""
    rows, seen = [], set()
    for row in files:
        identity = str(row.get('file_id') or row.get('id') or '')
        if identity and identity not in seen:
            rows.append(row)
            seen.add(identity)
    query = str(query).strip()
    exact = [r for r in rows if r['name'] == query]
    if exact:
        return exact
    title = reference(query)
    wanted = normalized(title)
    if not wanted or demonstrative(title) or len(wanted) < 2:
        return []
    equal = [r for r in rows if normalized(r['name']) == wanted]
    if equal:
        return equal
    return [r for r in rows if wanted in normalized(r['name']) or normalized(r['name']) in wanted]


def safe_name(name):
    name = re.sub(r'\[CQ:[^\]]*\]|\[\[mechanism:[^\r\n]*', '', str(name))
    return re.sub(r'[\x00-\x1f@]', '', name)[:180]


def candidate_message(rows):
    names = '\n'.join('• ' + safe_name(r['name']) for r in rows[:8])
    suffix = '\n（还有更多候选，请补充书名。）' if len(rows) > 8 else ''
    return '找到多份可能的群文档，请指定完整文件名：\n' + names + suffix + '\n可发“读文献 完整文件名”，或重新点名其中一份要提要。'


class GroupFileState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.catalogues = OrderedDict()
        self.requests = OrderedDict()
        self.lookups = OrderedDict()

    def _prune(self, store):
        for key in list(store):
            if store[key][0] <= self.clock():
                store.pop(key, None)

    def _put(self, store, key, value, ttl, capacity):
        self._prune(store)
        store.pop(key, None)
        store[key] = (self.clock() + ttl, value)
        while len(store) > capacity:
            store.popitem(last=False)

    def catalogue(self, group_id):
        self._prune(self.catalogues)
        return self.catalogues.get(str(group_id), (0, None))[1]

    def remember_catalogue(self, group_id, rows, status):
        value = {'rows': rows[:80], 'status': status}
        self._put(self.catalogues, str(group_id), value, 45, 32)
        return value

    def invalidate(self, group_id):
        self.catalogues.pop(str(group_id), None)

    def wait_for_upload(self, group_id, sender, question):
        self._put(self.requests, (str(group_id), str(sender)), str(question)[:1500], 600, 64)

    def wait_for_document(self, group_id, sender, question, target=''):
        self._put(self.requests, (str(group_id), str(sender)),
                  {'question': str(question)[:1500], 'target': str(target)[:500]}, 600, 64)

    def take_request(self, group_id, sender):
        self._prune(self.requests)
        item = self.requests.pop((str(group_id), str(sender)), None)
        return item[1] if item else None

    def cancel(self, group_id, sender):
        self.requests.pop((str(group_id), str(sender)), None)
        self.lookups.pop((str(group_id), str(sender)), None)

    def begin_lookup(self, group_id, sender):
        token = object()
        self._put(self.lookups, (str(group_id), str(sender)), token, 600, 128)
        return token

    def lookup_current(self, group_id, sender, token):
        self._prune(self.lookups)
        return self.lookups.get((str(group_id), str(sender)), (0, None))[1] is token
