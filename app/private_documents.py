"""Recent private attachments and private reading, sharing the existing validated downloader."""
import asyncio
import base64
from collections import OrderedDict
import hashlib
from pathlib import Path
import re
import time

from group_file_reader import match_files, demonstrative, safe_name
import paper_reader
import reading_bridge

_files = OrderedDict()
_waits = {}
TTL = 600


def permitted(config):
    return (config.get('paper_reader', {}).get('enabled', True)
            and config.get('paper_reader', {}).get('private_enabled', True)
            and config.get('deepseek', {}).get('enabled') and config.get('deepseek', {}).get('trigger_private'))


def key(scope):
    if re.fullmatch(r'private_\d+', str(scope)):
        return ('private', scope.split('_')[1])
    _, group, user = scope.split('_')
    return group, user


def prune():
    now = time.time()
    for identity in list(_files):
        _files[identity] = [r for r in _files[identity] if 0 <= now - r['_received'] < TTL]
        if not _files[identity]:
            del _files[identity]
    for identity in list(_waits):
        if _waits[identity]['expires'] <= now:
            del _waits[identity]


def recent(bot, scope):
    prune()
    return _files.get((str(bot.BASE_DIR), scope), [])


def register(bot, scope, rows):
    identity = (str(bot.BASE_DIR), scope)
    existing = list(recent(bot, scope))
    added = []
    for row in rows:
        name = str(row.get('name') or row.get('file_name') or row.get('filename') or '')
        name = re.sub(r'[\x00-\x1f]', '', name)
        if Path(name).suffix.casefold() not in paper_reader.EXTENSIONS:
            continue
        fid = row.get('file_id') or row.get('id')
        if not fid and not row.get('url'):
            continue
        if not fid:
            fid = hashlib.sha256(str(row['url']).encode()).hexdigest()
        item = {k: row[k] for k in ('file_id', 'id', 'url', 'file_hash', 'fileHash', 'size', 'file_size') if k in row}
        item.update(name=name[:180], file_id=str(fid), _received=time.time(), _nonce=hashlib.sha256(
            (str(fid) + ':' + str(time.time_ns())).encode()).hexdigest())
        existing = [r for r in existing if r['file_id'] != item['file_id']]
        existing.append(item)
        added.append(item)
    if added:
        _files[identity] = existing[-8:]
        _files.move_to_end(identity)
        while len(_files) > 64:
            _files.popitem(last=False)
    return added


def wait(bot, scope, question, target=''):
    prune()
    if len(_waits) >= 64:
        _waits.pop(next(iter(_waits)))
    _waits[(str(bot.BASE_DIR), scope)] = {'expires': time.time() + TTL, 'question': question[:1500], 'target': target}


def state(bot, scope):
    doc = bot._get_paper_library().current_doc(key(scope))
    return {'最近10分钟本人私聊附件': [{'name': row['name'], 'bytes': row.get('file_size', row.get('size'))}
            for row in recent(bot, scope)],
            '已读文档': {'title': doc.get('title'), '说明': '可申请inspect_current_document取得真实摘录'} if doc else None,
            '说明': '仅本私聊附件，不是群文件目录；有多份时按名称核对，不编造未读正文'}


def resolve(bot, scope, name):
    rows = recent(bot, scope)
    if demonstrative(name) or not name or name == '待上传文件':
        return rows if len(rows) != 1 else rows[-1:]
    return match_files(name, rows)


async def download(bot, row, user):
    limit = paper_reader.MAX_BYTES
    size = row.get('file_size', row.get('size'))
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        raise paper_reader.PaperError('文件大小无效，请重新上传。') from None
    if size is not None and (size < 0 or size > limit):
        raise paper_reader.PaperError('文档超过64 MB，请拆分后上传。')
    info = {'url': row.get('url')}
    if not info['url']:
        params = {'user_id': int(user), 'file_id': row['file_id']}
        file_hash = row.get('file_hash') or row.get('fileHash')
        if file_hash:
            params['file_hash'] = file_hash
        resolved = await bot.call_api('get_private_file_url', params, timeout=30)
        if isinstance(resolved, str):
            info = {'url': resolved}
        elif isinstance(resolved, dict):
            info = resolved
        if not info.get('url'):
            fallback = await bot.call_api('get_file', {'file_id': row['file_id']}, timeout=30)
            if isinstance(fallback, dict):
                info = fallback
    location = info.get('url') or info.get('file')
    encoded = info.get('base64') or (str(location)[9:] if str(location).startswith('base64://') else None)
    if encoded:
        if not isinstance(encoded, str) or len(encoded) > (limit + 2) // 3 * 4 + 9:
            raise paper_reader.PaperError('接入层返回的文档超过大小上限。')
        if encoded.startswith('base64://'):
            encoded = encoded[9:]
        try:
            body = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise paper_reader.PaperError('接入层返回了无效文件数据。') from None
    elif isinstance(location, str) and location.startswith(('https://', 'http://')):
        # Crucially reuse the VM's PublicResolver/DoH and transport patches.
        body, _, _ = await bot.paper_reader.fetch_bytes(location)
    else:
        raise paper_reader.PaperError('接入层没有提供可读取的私聊文件URL或数据；不读取任意本地路径。')
    if len(body) > limit or size is not None and size > 0 and len(body) != size:
        raise paper_reader.PaperError('文档超过上限或下载不完整，请重新上传。')
    return await bot.paper_reader.extract_in_worker(body, row['name'], '私聊文件：' + row['name'])


async def read(bot, data, question, config, *, row=None, url=None, followup=False, expected=None):
    scope = bot._mechanism_key(data)
    if not scope or not scope.startswith('private_') or not permitted(config):
        await bot.send_msg_to_user(data, config, '当前私聊文献功能未开启。')
        return
    library = bot._get_paper_library()
    identity = key(scope)
    acquired = False
    route = bot._reading_signature(config)
    def current():
        fresh = bot.load_config()
        return permitted(fresh) and bot._reading_signature(fresh) == route and library.versions.get(identity, 0) == version
    try:
        version = library.acquire(identity)
        acquired = True
        with reading_bridge.gate.foreground():
            if row:
                if expected and row['_nonce'] != expected:
                    raise paper_reader.PaperError('附件已变化，请重新指定，未读取其他文件。')
                await bot.send_msg_to_user(data, config, '正在读取私聊文件：' + safe_name(row['name']))
                doc = await bot._paper_stage('下载并解析私聊文档', download(bot, row, scope.split('_')[1]), timeout=300)
            elif url:
                await bot.send_msg_to_user(data, config, '正在读取文献链接。')
                doc = await bot._paper_stage('读取私聊文献链接', bot.paper_reader.read_link(url), timeout=300)
            else:
                doc = library.current_doc(identity)
                if not doc:
                    raise paper_reader.PaperError('当前私聊尚无已读正文，请先发送文件或链接。')
            if not current():
                return
            result = await bot._paper_stage('私聊模型阅读', bot._generate_paper(doc, question or '请概括这份文档',
                config, speaker_key=scope), timeout=reading_bridge.READING_STAGE_TIMEOUT)
            if not current():
                return
            library.store_doc(identity, doc)
            if not followup and result and doc.get('_reading_sha'):
                job = reading_bridge.sessions(bot).start(scope, doc, result)
                if job:
                    result += '\n\n' + reading_bridge.sessions(bot).status(job)
            await bot.send_msg_to_user(data, config, result)
    except asyncio.CancelledError:
        raise
    except paper_reader.PaperError as exc:
        await bot.send_msg_to_user(data, config, '私聊文献阅读：' + str(exc))
    except Exception as exc:
        bot.log('WARN', '[PRIVATE_PAPER] failed type=' + type(exc).__name__)
        await bot.send_msg_to_user(data, config, '私聊文献下载、解析或模型阅读未完成，请重试；尚不能算作已经读过。')
    finally:
        if acquired:
            library.release(identity)


async def receive(bot, data, rows, config):
    scope = bot._mechanism_key(data)
    if not permitted(config):
        await bot.send_msg_to_user(data, config, '当前私聊文献功能未开启，尚未登记或读取这份附件。')
        return
    added = register(bot, scope, rows)
    if not added:
        await bot.send_msg_to_user(data, config, '此附件不是支持的PDF、TXT、Markdown或Word文档；如需翻译请先发“翻译”。')
        return
    waiting = _waits.pop((str(bot.BASE_DIR), scope), None)
    question = re.sub(r'\[CQ:[^\]]*\]', '', data.get('raw_message', '')).strip()
    candidates = match_files(waiting['target'], added) if waiting and waiting['target'] else added
    if waiting and len(candidates) == 1:
        await read(bot, data, waiting['question'], config, row=candidates[0])
    elif question and re.search(r'读|讀|提要|总结|總結|概括|分析|看一下', question) and len(added) == 1:
        await read(bot, data, question, config, row=added[0])
    else:
        if waiting:
            wait(bot, scope, waiting['question'], waiting['target'])
        await bot.send_msg_to_user(data, config, '已登记私聊附件：' + '、'.join(safe_name(r['name']) for r in added)
            + '。10分钟内说“读一下这份”或指定文件名即可；多份时请说明哪一份。')


async def command(bot, data, config):
    scope = bot._mechanism_key(data)
    if not scope or not scope.startswith('private_'):
        return False
    text = data.get('raw_message', '').strip().translate(str.maketrans('讀獻論狀態忘記進頁繼續暫驗書這份', '读献论状态忘记进页继续暂验书这份'))
    control = re.fullmatch(r'(逐页精读|继续阅读|暂停阅读|验收阅读|阅读进度)', text)
    if control:
        await reading_bridge.control(bot, data, {'逐页精读': 'deep', '继续阅读': 'resume', '暂停阅读': 'pause',
            '验收阅读': 'accept', '阅读进度': 'progress'}[control[1]], config)
        return True
    match = re.fullmatch(r'(读文献|读论文|文献追问|文献状态|忘记文献)(?:\s+([\s\S]*))?', text)
    if not match:
        return False
    if not permitted(config):
        await bot.send_msg_to_user(data, config, '私聊文献功能未开启。')
        return True
    action, argument = match[1], (match[2] or '').strip()
    library = bot._get_paper_library()
    if action == '忘记文献':
        library.forget(key(scope))
        reading_bridge.sessions(bot).control(scope, 'forget')
        _files.pop((str(bot.BASE_DIR), scope), None)
        _waits.pop((str(bot.BASE_DIR), scope), None)
        await bot.send_msg_to_user(data, config, '已清除本私聊的文档缓存、待上传请求和阅读任务，其他群及私聊不受影响。')
    elif action == '文献状态':
        info = state(bot, scope)
        await bot.send_msg_to_user(data, config, '最近10分钟的私聊附件：' + ('、'.join(r['name'] for r in info['最近10分钟本人私聊附件']) or '暂无')
            + '；当前已读：' + (info['已读文档']['title'] if info['已读文档'] else '暂无'))
    elif action == '文献追问':
        await read(bot, data, argument, config, followup=True)
    elif re.search(r'https?://', argument):
        url = re.search(r'https?://[^\s<>]+', argument)[0]
        await read(bot, data, argument.replace(url, '').strip(), config, url=url)
    else:
        rows = resolve(bot, scope, argument)
        if len(rows) == 1:
            await read(bot, data, argument, config, row=rows[0])
        elif len(rows) > 1:
            await bot.send_msg_to_user(data, config, '最近有多份附件，请指定名称：' + '、'.join(safe_name(r['name']) for r in rows))
        else:
            wait(bot, scope, argument, argument)
            await bot.send_msg_to_user(data, config, '已登记阅读请求，请在10分钟内私聊上传对应文档。')
    return True
