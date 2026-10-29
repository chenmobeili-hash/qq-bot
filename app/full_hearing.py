"""Global full-audio policy; scoped cancellation for inline Bilibili audio jobs."""
import asyncio
from collections import OrderedDict
import re
import time

import hearing
from media_reader import MediaError
from vision_sync import VisionSync


class Mode(VisionSync):
    def __init__(self, path):
        super().__init__(path)
        self.active = {}
        self.history = OrderedDict()

    def status(self, key):
        row = self.active.get(key) or self.history.get(key)
        if not row:
            return None
        count = len(row['state'].get('seen', [])) if 'state' in row else row['count']
        return f'完整听力：{row["phase"]}；实际听取{count}/{row["total"]}段。'

    def stop(self, key):
        row = self.active.get(key)
        if row:
            row['stopped'] = True
            row['phase'] = '正在停止，等待当前请求返回'
        return bool(row)


def settings(bot):
    value = getattr(bot, 'full_hearing_mode', None)
    path = bot.BASE_DIR / 'full_hearing_mode.json'
    if value is None or value.path != path:
        value = Mode(path)
        bot.full_hearing_mode = value
    return value


def enabled(bot):
    switch = settings(bot)
    if not switch.storage_ok:
        raise MediaError('完整听力状态文件异常，请检查full_hearing_mode.json；未静默改为部分收听')
    return switch.enabled


async def command(bot, data, text, config):
    normalized = text.translate(str.maketrans('聽開啟關閉狀態', '听开启关闭状态'))
    match = re.fullmatch(r'(?:完整听力|全面听力)(?:\s*(开启|关闭|状态))?[。！!\s]*', normalized)
    if not match:
        return False
    operation = match[1] or '状态'
    switch = settings(bot)
    actor = str(data.get('sender', {}).get('user_id') or data.get('user_id', ''))
    if operation in ('开启', '关闭'):
        if not bot.is_admin(actor, config):
            await bot.send_msg_to_user(data, config, '只有管理员可以更改全局完整听力模式。')
            return True
        try:
            switch.set_enabled(operation == '开启')
        except OSError:
            await bot.send_msg_to_user(data, config, '完整听力模式未保存，请检查full_hearing_mode.json及目录权限。')
            return True
        if operation == '关闭':
            # Cancel only our own per-request QQ jobs, never the long-lived Bilibili runner.
            import qq_media
            listener = qq_media.controller(bot).full_listener
            for job in list(listener.jobs.values()):
                if job.get('automatic') and not job['task'].done():
                    job['task'].cancel()
            for row in switch.active.values():
                row['stopped'] = True
    if not switch.storage_ok:
        await bot.send_msg_to_user(data, config, '完整听力状态文件异常，自动完整收听已暂停。')
        return True
    state = '开启' if switch.enabled else '关闭'
    await bot.send_msg_to_user(data, config, '完整听力：' + state + '（全局，所有群和私聊）。'
        '开启后，已触发的音频读取、看视频及可提取的B站音轨会自动分轮听完，并保留原有画面采样。'
        '仍需可用听觉模型；不自动读取未点名的群附件，不改变主模型、视觉或B站互动权限。'
        '每份最多30分钟/64MB，可能多次调用模型；失败暂停，成功片段保留。'
        '关闭停止自动续听，已发出的B站片段请求可能需等返回；明确下达的“听完”仍可使用。')
    return True


async def review(bot, result, config, question='', state=None, group=None):
    """Shared inline path for Bilibili; QQ uses its cancellable FullListening jobs."""
    state = state if state is not None else {}
    if not enabled(bot):
        return await hearing.review(bot, result, config, question, state, group)
    chunks = result.get('audio_chunks', [])
    if not chunks:
        raise MediaError(result.get('audio_status') or '没有可读取的音轨')
    switch = settings(bot)
    key = config.get('_media_key') or f'bili_auto_{group}_{id(state)}'
    if key not in switch.active and len(switch.active) >= 4:
        raise MediaError('完整听力任务较多，请稍后再试')
    cache, cached = None, None
    if config.get('_media_key'):
        import qq_media
        cache = qq_media.controller(bot).reviews
        candidate = cache.get(key)
        if candidate and candidate['result'] is result:
            cached = candidate
    deadline = time.monotonic() + 3600
    row = {'state': state, 'total': len(chunks), 'phase': '收听中', 'stopped': False}
    switch.active[key] = row

    def check():
        if switch.active.get(key) is not row or row['stopped'] or not enabled(bot):
            raise MediaError('完整听力已停止，成功片段保留；缓存有效期内可用“听完”继续')
        if not hearing.allowed(bot, bot.load_config(), group):
            raise MediaError('本聊天听觉配置已不可用，完整听力已暂停')
        if time.monotonic() >= deadline or (cached is not None and cache.get(key) is not cached):
            raise MediaError('音轨缓存已更新或过期，完整听力已暂停')

    try:
        while True:
            check()
            fresh = bot.load_config()
            before = (state.get('route'), len(state.get('seen', [])))
            output = await hearing.review(bot, result, fresh, question, state, group,
                                          exhaustive=True, checkpoint=check)
            check()
            if len(state.get('seen', [])) >= len(chunks):
                row['phase'] = '完成'
                return output
            after = (state.get('route'), len(state.get('seen', [])))
            if after == before:
                raise MediaError('本轮没有增加成功片段，完整听力已暂停')
            await asyncio.sleep(0)
    except BaseException:
        row['phase'] = '已暂停，成功片段保留'
        raise
    finally:
        if switch.active.get(key) is row:
            switch.active.pop(key, None)
            switch.history[key] = {'phase': row['phase'], 'count': len(state.get('seen', [])), 'total': len(chunks)}
            while len(switch.history) > 128:
                switch.history.popitem(last=False)
