"""Durable scoped reading cursors, expiring precise notes, and foreground priority."""
import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import paper_reader

PRECISE_SECONDS = 5 * 3600
background_reading = ContextVar('background_reading', default=False)


class PriorityGate:
    def __init__(self, clock=time.monotonic):
        self.clock, self.last, self.busy, self.revision, self.task = clock, clock(), 0, 0, None

    def interrupt(self):
        self.revision += 1
        self.last = self.clock()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if self.task and not self.task.done() and self.task is not current:
            self.task.cancel()

    @contextmanager
    def foreground(self):
        self.interrupt()
        self.busy += 1
        try:
            yield
        finally:
            self.busy -= 1
            self.last = self.clock()

    def idle(self):
        return not self.busy and self.clock() - self.last >= 30


class ReadingSessions:
    def __init__(self, root, log=lambda *args: None, clock=time.time):
        self.root, self.log, self.clock = Path(root), log, clock
        self.path = self.root / 'reading_sessions.json'
        self.cache = self.root / 'reading_sources'
        self.jobs, self.ignored, self.storage_ok = {}, {}, True
        self.lock = asyncio.Lock()
        try:
            if self.path.exists():
                if self.path.stat().st_size > 32000000:
                    raise ValueError('State too large')
                state = json.loads(self.path.read_text(encoding='utf-8'))
                if state.get('version') != 1 or not isinstance(state.get('jobs'), dict) or len(state['jobs']) > 64:
                    raise ValueError('Invalid state')
                self.jobs = state['jobs']
                self.ignored = state.get('ignored', {})
                if not isinstance(self.ignored, dict) or len(self.ignored) > 256:
                    raise ValueError('Invalid personal visibility state')
                for key, job in self.jobs.items():
                    if (not re.fullmatch(r'group_\d+_\d+:[a-f0-9]{64}', key)
                            or not isinstance(job, dict) or job.get('id') != key
                            or job.get('scope') != key.rsplit(':', 1)[0] or job.get('sha') != key.rsplit(':', 1)[1]
                            or job.get('mode') not in ('sample', 'deep')
                            or any(type(job.get(n)) is not int or job[n] < 0 for n in ('next_sample', 'next_deep', 'part', 'total'))
                            or not isinstance(job.get('precise'), list)):
                        raise ValueError('Invalid job')
                self.prune()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.jobs, self.storage_ok = {}, False
            log('WARN', '[READING] 状态文件异常，续读已停用；保留原文件供检查')

    def save(self):
        if not self.storage_ok:
            raise OSError('Reading state unavailable')
        data = json.dumps({'version': 1, 'jobs': self.jobs, 'ignored': self.ignored}, ensure_ascii=False)
        if len(data.encode('utf-8')) > 32000000:
            raise OSError('Reading state full')
        temp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as stream:
                os.chmod(temp, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
        except OSError:
            self.storage_ok = False
            raise
        finally:
            temp.unlink(missing_ok=True)

    def prune(self):
        changed = False
        for job in self.jobs.values():
            notes = [n for n in job['precise'] if n['expires'] > self.clock()]
            if notes != job['precise']:
                job['precise'] = notes
                changed = True
        if changed:
            self.save()

    def cache_pdf(self, body):
        if not self.storage_ok or not body[:1024].lstrip().startswith(b'%PDF-') or len(body) > paper_reader.MAX_BYTES:
            return None
        digest = hashlib.sha256(body).hexdigest()
        if self.cache.is_symlink():
            raise OSError('Reading source directory cannot be a symlink')
        self.cache.mkdir(exist_ok=True)
        os.chmod(self.cache, 0o700)
        path = self.cache / (digest + '.pdf')
        if not path.exists():
            # A bounded cache: never evict a source which still has a saved cursor.
            for old in self.cache.glob('*.pdf'):
                if self.clock() - old.stat().st_mtime > 86400 and not any(j['sha'] == old.stem for j in self.jobs.values()):
                    old.unlink()
            if sum(p.stat().st_size for p in self.cache.glob('*.pdf')) + len(body) > 256 * 1024 * 1024:
                raise OSError('Reading source cache full')
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(body)
        return digest

    def source(self, job):
        if not re.fullmatch('[a-f0-9]{64}', job['sha']):
            raise ValueError('Invalid source')
        path = self.cache / (job['sha'] + '.pdf')
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != job['sha']:
            raise ValueError('Reading source changed')
        return path

    def current(self, scope):
        self.prune()
        rows = [j for j in self.jobs.values() if j['scope'] == scope and j.get('current', True)]
        return max(rows, key=lambda j: j['updated'], default=None)

    def start(self, scope, doc, review):
        sha = doc.get('_reading_sha')
        if not sha or not re.fullmatch(r'group_\d+_\d+', scope):
            return None
        key = scope + ':' + sha
        for job in self.jobs.values():
            if job['scope'] == scope and job['id'] != key:
                job['active'], job['current'] = False, False
        if key not in self.jobs:
            if len(self.jobs) >= 64:
                raise OSError('Reading session limit reached')
            # Continue after the last actually extracted page, not blindly after page 100.
            pages = [int(m[1]) for u in doc['units'] if (m := re.fullmatch(r'第(\d+)页', u['label']))]
            end = max(pages, default=0)
            self.jobs[key] = {'id': key, 'scope': scope, 'sha': sha, 'title': str(doc['title'])[:200],
                'total': doc['total'], 'mode': 'sample', 'next_sample': end + 1, 'next_deep': 1,
                'part': 0, 'active': end < doc['total'], 'current': True, 'accepted': False,
                'reflection': review, 'memory': '', 'precise': [], 'windows': [], 'gaps': [],
                'errors': 0, 'retry_at': 0, 'updated': self.clock(), 'deep_started': False}
        job = self.jobs[key]
        job['current'], job['updated'] = True, self.clock()
        self.save()
        return job

    def control(self, scope, action):
        if action == 'forget':
            for key in [k for k, j in self.jobs.items() if j['scope'] == scope]:
                del self.jobs[key]
            group = re.fullmatch(r'group_(\d+)_\d+', scope)
            shared = self.current(f'group_{group[1]}_0') if group else None
            if shared:
                self.ignored[scope] = shared['sha']
                while len(self.ignored) > 256:
                    del self.ignored[next(iter(self.ignored))]
            self.save()
            return '已清除你在本群的续读进度和阅读记忆，并隐藏当前共享阅读记忆。'
        job = self.current(scope)
        if not job:
            return '你在本群还没有可续读的PDF，请先让我成功读一次群文件或PDF链接。'
        if action == 'deep':
            job.update(mode='deep', active=True, accepted=False, errors=0, retry_at=0, deep_started=True)
        elif action == 'resume':
            job.update(active=True, accepted=False, errors=0, retry_at=0)
        elif action in ('pause', 'accept'):
            job['active'] = False
            job['accepted'] = action == 'accept'
        self.save()
        return self.status(job)

    def status(self, job):
        return (f"《{job['title']}》：{'逐页精读' if job['mode'] == 'deep' else '每100页抽样续读'}；"
                f"{'等待空闲自动续读' if job['active'] else '已停止'}。"
                f"下次抽样从第{job['next_sample']}页；下次精读第{job['next_deep']}页"
                f"（页内偏移{job['part']}）；全书{job['total']}页。"
                f"未识别文字页{len(job['gaps'])}页；连续失败{job['errors']}次。抽样不等于逐页读完；精读不识别图片图表。")

    def memory_for(self, scope, question=''):
        job = self.current(scope)
        if not job:
            return None
        match = re.search(r'第?\s*(\d+)\s*页', question)
        candidates = list(reversed(job['precise']))
        if match:
            candidates.sort(key=lambda n: n['page'] != int(match[1]))
        notes, budget = [], 24000
        for note in candidates:
            length = len(note.get('text', '')) + len(note['note'])
            if length <= budget:
                notes.append(copy.deepcopy(note))
                budget -= length
        return {'title': job['title'], 'status': self.status(job), 'reflection': job['reflection'],
                'selected_memory': job['memory'], 'precise_notes': notes,
                'precision_expires': min((n['expires'] for n in notes), default=None),
                'precise_coverage': '仅本次选入的有效笔记与文字片段，未选入或未提供的页不可凭空回答',
                '说明': '精确笔记仅5小时有效。过期后只有读后感和不超过3000字的自选记忆；无法核对细节时明确说不记得，需重读。'}

    async def tick(self, gate, eligible, extract, context, generate, publish, job_allowed=lambda job: True):
        if self.lock.locked() or not self.storage_ok:
            return
        self.prune()
        if not gate.idle() or not eligible():
            return
        rows = [j for j in self.jobs.values() if j['active'] and j['retry_at'] <= self.clock() and job_allowed(j)]
        if not rows:
            return
        job = min(rows, key=lambda j: j['updated'])
        # Run one cancellable step. The outer runner survives foreground interruption.
        async with self.lock:
            revision = gate.revision
            async def step():
                token = background_reading.set(True)
                try:
                    await self._step(job, lambda: gate.idle() and gate.revision == revision and eligible() and job_allowed(job),
                                     extract, context, generate, publish)
                finally:
                    background_reading.reset(token)
            gate.task = asyncio.create_task(step())
            async def watch():
                while not gate.task.done():
                    await asyncio.sleep(1)
                    if not gate.idle() or gate.revision != revision or not eligible() or not job_allowed(job):
                        gate.task.cancel()
                        return
            watcher = asyncio.create_task(watch())
            try:
                await gate.task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                self.log('INFO', '[READING] 前台活动打断续读，空闲后从已保存页码恢复')
            except Exception as exc:
                job['errors'] += 1
                job['retry_at'] = self.clock() + min(1800, 60 * 2 ** job['errors'])
                if job['errors'] >= 3:
                    job['active'] = False
                self.save()
                self.log('WARN', '[READING] 本步未完成，页码未推进 类型=' + type(exc).__name__)
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
                gate.task = None

    async def _step(self, job, valid, extract, context, generate, publish):
        deep = job['mode'] == 'deep'
        page = job['next_deep' if deep else 'next_sample']
        if page > job['total']:
            job['active'] = False
            self.save()
            return
        data = await extract(self.source(job), page, page if deep else min(page + 99, job['total']), deep)
        if not valid():
            return
        finished = True
        if deep:
            full = data['units'][0]['text'] if data['units'] else ''
            offset = job['part']
            if offset > len(full):
                raise ValueError('Page offset invalid')
            chunk = full[offset:offset + 12000]
            finished = offset + len(chunk) >= len(full)
            payload = {'mode': 'deep', 'page': page, 'offset': offset, 'page_complete': finished,
                       'text': chunk, 'no_text': not full}
        else:
            if data['end'] < page:
                raise ValueError('Extraction did not advance')
            payload = {'mode': 'sample', 'range': [page, data['end']], 'document': context({
                **data, 'title': job['title']})}
        # Empty/scanned pages are recorded as gaps, never presented as understood.
        result = await generate(copy.deepcopy(job), payload, self.memory_for(job['scope']))
        if not valid() or not job['active']:
            return
        if not isinstance(result, dict) or set(result) != {'note', 'reflection', 'memory'}:
            raise ValueError('Invalid reading result')
        limits = {'note': 4000, 'reflection': 16000, 'memory': 3000}
        if any(not isinstance(result[k], str) or len(result[k]) > limits[k] or not result[k].strip() for k in limits):
            raise ValueError('Invalid reading memory size')
        previous = copy.deepcopy(job)
        job['reflection'], job['memory'] = result['reflection'], result['memory']
        if deep:
            job['precise'].append({'page': page, 'offset': job['part'], 'note': result['note'], 'text': chunk,
                                   'expires': self.clock() + PRECISE_SECONDS})
            if finished:
                job['next_deep'], job['part'] = page + 1, 0
            else:
                job['part'] += len(chunk)
        else:
            job['next_sample'] = data['end'] + 1
            job['windows'].append({'range': [page, data['end']],
                'locators': payload['document']['coverage']['locators']})
        job['gaps'] = sorted(set(job['gaps']) | set(data['empty']))
        job.update(errors=0, retry_at=0, updated=self.clock())
        if job['next_deep' if deep else 'next_sample'] > job['total']:
            job['active'] = False
        try:
            self.save()
        except OSError:
            job.clear()
            job.update(previous)
            raise
        self.log('INFO', f"[READING] {'精读' if deep else '抽样'} 已保存页码={page} next={job['next_deep' if deep else 'next_sample']}")
        # Deep reading stays quiet between pages; only the final reflection is shared.
        if valid() and (not deep or not job['active']):
            prefix = (f"《{job['title']}》精读进度已走到末页；没有文字的页未理解。\n" if deep else
                      f"《{job['title']}》续读第{page}—{data['end']}页范围的抽样，未逐页读完。\n")
            try:
                await publish(job, prefix + (result['reflection'] if deep else result['note'])[:2500])
            except Exception as exc:
                self.log('WARN', '[READING] 阅读已保存但分享未送达，不重读或反复补发 类型=' + type(exc).__name__)
