"""Opt-in, model-chosen voice replies; existing chat/memory functions stay intact."""
import asyncio
import base64
from collections import OrderedDict
from contextvars import ContextVar
import copy
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import re
import time
import uuid

import fish_audio as audio
from whale_stickers import StickerReply, strip_markers

_turn = ContextVar('fish_tts_turn', default=None)
HELP = '''
【Fish Audio 语音】
语音模式 开启 / 关闭 / 状态（支持繁体；当前群或当前私聊独立，开关仅配置管理员）
开启后由主模型在同一轮决定是否附语音，文字始终保留；普通聊天、群主动接话和私聊自主唤醒可选择，不强制每条朗读。
选择音色 / 音色列表（只列私有配置里的音色名称；选择仅管理员，显示当前音色，可取消，2分钟有效，按聊天及发言者隔离）
语音测试；语音说 一段短句（群内需@我，私聊直接说；显式请求可在自动模式关闭时使用）
也可自然说“开启这个群的语音”“换成某个已配置音色”“用语音说一句……”；按原建议确认流程执行。
主模型、听觉模型与TTS完全独立；不自动复刻或上传参考音频。Key与reference_id仅填本机config.yaml顶层fish_audio。
默认短句最多180字符/30秒，全局滚动24小时60次、每聊天20次，自动语音冷却60秒；失败也计尝试，不自动重发。
语音临时音档最多保留1小时，清理不删文字记忆；定时公告、后台文献/B站分享保持文字。
'''
ACTIONS = {'tts_on': ((), True, False, False), 'tts_off': ((), True, False, False),
           'tts_status': ((), False, False, False), 'tts_voice_menu': ((), True, False, False),
           'tts_voice_set': (('name',), True, False, False), 'tts_say': (('text',), False, False, False)}
DESCRIPTIONS = {
    'tts_on': '在当前群或当前私聊开启模型自主语音：允许把合适的短回复提交Fish Audio合成并真实发送QQ语音，保留文字；不改变其他聊天或主/听觉模型',
    'tts_off': '只关闭当前聊天后续自动语音，保留文字回复、音色选择和已有记忆；在途请求可能已产生合成用量',
    'tts_status': '查看当前聊天的语音模式与当前音色，不显示key或代理地址',
    'tts_voice_menu': '打开当前聊天的音色选择菜单，显示当前音色、可取消，2分钟有效；不改变主模型/听觉模型',
    'tts_voice_set': '只为当前聊天选择已经配置的TTS音色，不自动复刻，不改人格或模型',
    'tts_say': '把用户明确要求的短句交Fish Audio合成并发送到当前聊天，同时保留文字；受字符、时长和额度限制',
}


class VoiceReply(StickerReply):
    pass


class RecordSegments(list):
    # JSON encoding remains an actual OneBot segment array; logs/timeline see a label.
    def __str__(self):
        return '[TTS语音；对应本轮已发送文字]'
    __repr__ = __str__


@dataclass
class Turn:
    bot: object
    key: str
    room: str
    version: int
    family: str = 'chat'
    speak: bool = False
    content: str = ''


class Manager:
    def __init__(self, root):
        self.root = root
        self.state, self.cache = audio.State(root), audio.Cache(root)
        self.versions = OrderedDict()
        self.busy = False

    def bump(self, room):
        value = self.versions.pop(room, 0) + 1
        self.versions[room] = value
        while len(self.versions) > 256:
            self.versions.popitem(last=False)
        return value


def manager(bot):
    value = getattr(bot, '_fish_tts_manager', None)
    if value is None or value.root != bot.BASE_DIR:
        value = Manager(bot.BASE_DIR)
        bot._fish_tts_manager = value
    return value


def selection(bot, key, config):
    cfg = audio.settings(config)
    room = audio.scope_key(key)
    if not room:
        raise audio.TTSError('invalid_scope')
    profile = manager(bot).state.profile(room)
    label = profile['voice'] or cfg['default_voice']
    identity = cfg['voices'].get(label, '')
    return cfg, profile, label, identity


def signature(bot, key, config):
    cfg, profile, label, identity = selection(bot, key, config)
    return hashlib.sha256(json.dumps([cfg, profile, label, identity], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def available(bot, key, config, *, automatic=False):
    if 'fish_audio' not in config:
        return False
    try:
        cfg, profile, _, identity = selection(bot, key, config)
        return bool(cfg['enabled'] and cfg['api_key'] and identity and (profile['enabled'] or not automatic))
    except (audio.TTSError, ValueError, TypeError):
        return False


def summary(bot, key, config):
    try:
        cfg, profile, label, identity = selection(bot, key, config)
        return {'自动语音': profile['enabled'], '可调用': bool(cfg['enabled'] and cfg['api_key'] and identity),
                '当前音色': label or '未配置', '可选音色': list(cfg['voices']), 'TTS模型': cfg['model'],
                '说明': '按当前聊天生效；群/私聊开关及音色选择均仅管理员；开启后由模型决定，保留文字。'}
    except (audio.TTSError, ValueError, TypeError):
        return {'自动语音': False, '可调用': False, '说明': '配置或语音状态不可用，文字正常，管理员检查私有fish_audio配置。'}


def arm_transport(bot):
    transport = bot.decision_transport
    if getattr(transport, '_fish_tts_wrapped', False):
        return
    original = transport.create
    @wraps(original)
    async def create(client, model, messages, **kwargs):
        ctx = _turn.get()
        if not ctx or ctx.bot is not bot or not kwargs.get('structured') or not available(bot, ctx.key, bot.load_config(), automatic=True):
            return await original(client, model, messages, **kwargs)
        marker = '[本轮强制决策协议' if ctx.family == 'chat' else '[本轮公开消息输出协议'
        if not any(row.get('role') == 'system' and isinstance(row.get('content'), str) and marker in row['content'] for row in messages):
            return await original(client, model, messages, **kwargs)
        ctx.speak = False
        request = [dict(row) for row in messages]
        request.append({'role': 'system', 'content':
            '[本轮可选语音扩展，由程序开启]\n当前聊天允许自主附语音。在原完整JSON中额外允许speak布尔值，省略或false表示只发文字。'
            '请由你根据语境判断，不必每条都用语音；温柔问候、简短回应或用户明确希望听你说话时可true。'
            '只有最终chat（公开协议为send）的自然短句可以true，原有kind/reply/action/memory及权限规则保持。'
            '只朗读最终reply，不另写台词，不选择收件人、key、音色ID或API。代码、链接、资料长文、敏感凭据、'
            '操作确认/澄清、沉默和只读查询都用false；用户要求纯文字或不要语音时false。'
            'reply尽量120字内，无格式或控制标记，不能假称已经发出语音；程序只在文字实际送达后尝试合成。'})
        response = await original(client, model, request, **kwargs)
        try:
            choice = response.choices[0]
            if choice.finish_reason != 'stop':
                return response
            from model_decision import _envelope_json
            value, _ = _envelope_json(choice.message.content)
            allowed = {'kind','reply','action','memory','speak'} if ctx.family == 'chat' else {'kind','reply','speak'}
            if not isinstance(value, dict) or 'speak' not in value or set(value) - allowed:
                return response
            flag = value.pop('speak')
            ctx.speak = (flag is True and value.get('kind') == ('chat' if ctx.family == 'chat' else 'send')
                         and isinstance(value.get('reply'), str) and value.get('action') is None)
            ctx.content = value.get('reply', '') if ctx.speak else ''
            # Strip only this optional field. Core parsing, memory repair and public
            # final-text validation still happen in their original callers.
            response = copy.copy(response)
            response.choices = list(response.choices)
            response.choices[0] = copy.copy(choice)
            response.choices[0].message = copy.copy(choice.message)
            response.choices[0].message.content = json.dumps(value, ensure_ascii=False)
        except (ValueError, TypeError, AttributeError, IndexError):
            ctx.speak = False
        return response
    transport.create = create
    transport._fish_tts_wrapped = True


def attach(bot, key, reply, *, automatic, version=None):
    from mechanism_assistant import SuggestedReply
    if not reply or isinstance(reply, SuggestedReply):
        return reply
    cfg, _, _, _ = selection(bot, key, bot.load_config())
    audio.clean_text(reply, cfg['max_chars'])
    if type(reply) is str:
        reply = VoiceReply(reply, None, getattr(bot, '_persona_signature', None))
    room = audio.scope_key(key)
    reply.fish_tts = dict(key=key, room=room, ticket=uuid.uuid4().hex, expires=time.monotonic() + 120,
        version=manager(bot).versions.get(room, 0) if version is None else version,
        route=signature(bot, key, bot.load_config()), automatic=automatic,
        persona=getattr(reply, 'persona_signature', None) or getattr(bot, '_persona_signature', None))
    return reply


async def generation(bot, key, function, *args, family='chat', **kwargs):
    if not available(bot, key, bot.load_config(), automatic=True):
        return await function(*args, **kwargs)
    current = manager(bot)
    room = audio.scope_key(key)
    ctx = Turn(bot, key, room, current.versions.get(room, 0), family)
    token = _turn.set(ctx)
    arm_transport(bot)
    try:
        reply = await function(*args, **kwargs)
        expected = strip_markers(ctx.content)
        visible = str(reply or '').strip()
        matches = bool(expected and (visible == expected or visible.startswith(expected + '\n（')))
        if ctx.speak and matches and current.versions.get(room, 0) == ctx.version:
            try:
                return attach(bot, key, reply, automatic=True, version=ctx.version)
            except (audio.TTSError, ValueError, TypeError):
                pass
        return reply
    finally:
        _turn.reset(token)


async def deliver(bot, destination, reply):
    metadata = getattr(reply, 'fish_tts', None)
    if not isinstance(metadata, dict) or destination != metadata['room']:
        return False
    current, path, status = manager(bot), None, 'failed'
    reserved = False
    async def notify(reason):
        if metadata['automatic'] or metadata.get('notified'):
            return
        metadata['notified'] = True
        data = ({'message_type':'group','group_id':int(destination[2:]),'sender':{'user_id':metadata['key'].split('_')[-1]}}
                if destination.startswith('g:') else {'message_type':'private','sender':{'user_id':int(destination[2:])}})
        text = ('语音发送回执尚未确认，未自动重发；文字已保留。' if reason == 'unknown' else
                '本次语音未完成，文字已保留；请查看“语音模式 状态”或稍后再试。')
        try:
            await bot.send_msg_to_user(data, bot.load_config(), text)
        except Exception:
            bot.log('WARN', '[FISH_TTS] manual_notice_not_delivered')
    def valid():
        try:
            config = bot.load_config()
            bot._persona_prompt(config)
            return (metadata['expires'] > time.monotonic()
                and current.versions.get(destination, 0) == metadata['version']
                and getattr(bot, '_persona_signature', None) == metadata['persona']
                and available(bot, metadata['key'], config, automatic=metadata['automatic'])
                and signature(bot, metadata['key'], config) == metadata['route'])
        except (ValueError, TypeError, OSError):
            return False
    if not valid():
        return False
    if current.busy:
        await notify('busy')
        return False
    current.busy = True
    try:
        cfg, _, _, identity = selection(bot, metadata['key'], bot.load_config())
        reason = current.state.reserve(destination, metadata['ticket'], cfg, manual=not metadata['automatic'])
        if reason:
            status = reason
            bot.log('INFO', '[FISH_TTS] skipped=' + reason)
            return False
        reserved = True
        path, duration = await audio.synthesize(str(reply), identity, cfg, current.cache, guard=valid, log=bot.log)
        if not valid():
            raise audio.TTSError('context_changed')
        payload = RecordSegments([{'type':'record', 'data':{'file':'base64://' + base64.b64encode(path.read_bytes()).decode('ascii')}}])
        outcome = {}
        params = {'group_id' if destination.startswith('g:') else 'user_id': int(destination[2:]), 'message': payload}
        result = await bot.call_api('send_group_msg' if destination.startswith('g:') else 'send_private_msg', params, outcome=outcome)
        status = 'sent' if result is not None else 'unknown' if outcome.get('status') == 'unknown' else 'send_failed'
        bot.log('INFO', '[FISH_TTS] status=' + status + ' duration_ms=' + str(round(duration * 1000)) + ' bytes=' + str(path.stat().st_size))
        return result is not None
    except asyncio.CancelledError:
        status = 'cancelled'
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, audio.TTSError) else 'tts_failed'
        bot.log('WARN', '[FISH_TTS] failed=' + code + ' text_kept=1')
        return False
    finally:
        current.busy = False
        if path:
            current.cache.active.discard(path)
        if reserved:
            try:
                current.state.finish(metadata['ticket'], status)
            except audio.TTSError:
                bot.log('WARN', '[FISH_TTS] finish_state_unavailable')
        if status not in ('sent', 'cancelled', 'already_attempted'):
            await notify(status)


async def say(bot, data, text, config):
    key = bot._mechanism_key(data)
    try:
        if not available(bot, key, config):
            raise audio.TTSError('not_configured')
        cfg = audio.settings(config)
        audio.clean_text(text, cfg['max_chars'])
        room = audio.scope_key(key)
        manager(bot).bump(room)
        bot._persona_prompt(config)
        reply = attach(bot, key, text, automatic=False)
        await bot.send_msg_to_user(data, config, reply)
    except (audio.TTSError, ValueError, TypeError):
        await bot.send_msg_to_user(data, config, '语音尚未配置好，或短句超过限制。请检查私有fish_audio的key、voice_id/voices；不会改动主模型。')


async def menu(bot, data, config):
    key = bot._mechanism_key(data)
    info = summary(bot, key, config)
    choices = info.get('可选音色', [])
    if not choices:
        await bot.send_msg_to_user(data, config, '尚无配置音色。请在Fish Audio选择音色，把模型ID填入私有fish_audio.voice_id或voices；Key本身不能指定角色声音。')
        return
    state = dict(kind='tts_voice', choices=choices, baseline=signature(bot, key, config), ready=False, expires=time.monotonic() + 120)
    bot.user_states[key] = state
    bot.decision_transport.invalidate(key)
    bot._get_mechanism_assistant().cancel(key)
    text = '正在选择语音音色\n当前音色：' + info['当前音色'] + '\n' + '\n'.join('  [{}] {}'.format(i, name) for i, name in enumerate(choices, 1))
    text += '\n回复数字选择，回复“取消”退出；2分钟内有效。只影响当前聊天，不改人格、主模型或听觉模型。'
    state['ready'] = bool(await bot.send_msg_to_user(data, config, text))


async def command(bot, data, text, config):
    key = bot._mechanism_key(data)
    if not key:
        return False
    room = audio.scope_key(key)
    actor = key.split('_')[-1]
    text = text.strip().rstrip('。！! ')
    row = bot.user_states.get(key, {})
    if row.get('kind') == 'tts_voice':
        if text in ('取消','不用','不用了','算了','取消选择','取消選擇'):
            bot.user_states.pop(key, None)
            await bot.send_msg_to_user(data, config, '已取消音色选择，当前音色：' + summary(bot, key, config).get('当前音色', '未配置'))
            return True
        if text.isdigit():
            bot.user_states.pop(key, None)
            if (not row.get('ready') or row['expires'] <= time.monotonic() or not bot.is_admin(actor, config)
                    or row['baseline'] != signature(bot, key, config)):
                await bot.send_msg_to_user(data, config, '音色菜单未送达、已过期、权限或设置已变化；未修改，请重新选择音色。')
            elif len(text) <= 3 and 1 <= int(text) <= len(row['choices']):
                manager(bot).state.set(room, voice=row['choices'][int(text) - 1])
                await bot.send_msg_to_user(data, config, '已为当前聊天选择音色：' + row['choices'][int(text) - 1] + '。未改变其他聊天或模型。')
            else:
                await bot.send_msg_to_user(data, config, '无效编号，未修改音色；请重新打开菜单。')
            return True
        bot.user_states.pop(key, None)
    match = re.fullmatch(r'(?:语音模式|語音模式|TTS模式)(?:\s*(开启|開啟|关闭|關閉|状态|狀態))?', text, re.I)
    choosing = text in ('选择音色','選擇音色','音色列表')
    manual = re.fullmatch(r'(?:语音说|語音說|用语音说|用語音說)\s+([\s\S]+)', text)
    testing = text in ('语音测试','語音測試')
    if not (match or choosing or manual or testing):
        return False
    if room.startswith('g:') and not bot._is_mentioned(data.get('raw_message', ''), data.get('message', [])):
        return False
    action = {'開啟':'开启','關閉':'关闭','狀態':'状态'}.get(match[1], match[1]) if match else None
    if (choosing and text != '音色列表' or action in ('开启','关闭')) and not bot.is_admin(actor, config):
        await bot.send_msg_to_user(data, config, '只有配置管理员可以修改当前聊天的语音开关或音色。')
        return True
    try:
        if choosing:
            if text == '音色列表':
                info = summary(bot, key, config)
                await bot.send_msg_to_user(data, config, '当前音色：' + info.get('当前音色','未配置') + '\n可选音色：' + '、'.join(info.get('可选音色',[])))
            else:
                await menu(bot, data, config)
        elif manual or testing:
            await say(bot, data, manual[1] if manual else '你好，这是我的语音试听。', config)
        else:
            if action == '开启' and not available(bot, key, config):
                await bot.send_msg_to_user(data, config, '尚未配置可用的Fish Audio key和音色ID，未开启自动语音；请填写私有配置。')
                return True
            if action in ('开启','关闭'):
                manager(bot).state.set(room, enabled=action == '开启')
                manager(bot).bump(room)
            await bot.send_msg_to_user(data, config, '语音状态（仅当前聊天）：' + json.dumps(summary(bot, key, config), ensure_ascii=False))
    except (audio.TTSError, OSError, ValueError, TypeError):
        await bot.send_msg_to_user(data, config, '语音配置或状态保存未完成，原设置保留；文字聊天正常。请检查私有配置和持久盘。')
    return True


async def execute(bot, data, proposal, config):
    from mechanism_assistant import validate
    key = bot._mechanism_key(data)
    validate(proposal['action'], proposal['args'], key, bot.is_admin(key.split('_')[-1], config))
    action, args = proposal['action'], proposal['args']
    if action == 'tts_say':
        return await say(bot, data, args['text'], config)
    if action == 'tts_voice_set':
        cfg = audio.settings(config)
        if args['name'] not in cfg['voices']:
            raise audio.TTSError('unknown_voice')
        manager(bot).state.set(audio.scope_key(key), voice=args['name'])
        return await bot.send_msg_to_user(data, config, '已为当前聊天选择音色：' + args['name'])
    text = {'tts_on':'语音模式 开启', 'tts_off':'语音模式 关闭', 'tts_status':'语音模式 状态', 'tts_voice_menu':'选择音色'}[action]
    # This action has already passed same-chat confirmation + permission checks.
    if audio.scope_key(key).startswith('g:'):
        data = {**data, 'raw_message':'[CQ:at,qq=' + str(bot.bot_qq) + '] ' + text}
    return await command(bot, data, text, config)


def install(bot):
    if getattr(bot, '_fish_tts_installed', False):
        return
    bot._fish_tts_installed = True
    import mechanism_assistant as mechanisms
    import model_decision
    mechanisms.ACTIONS.update(ACTIONS)
    old_describe = mechanisms.describe
    def describe(proposal):
        return DESCRIPTIONS.get(proposal['action']) or old_describe(proposal)
    mechanisms.describe = model_decision.describe = bot.mechanism_description = describe
    bot.COMMAND_HELP = mechanisms.COMMAND_HELP = mechanisms.COMMAND_HELP + HELP
    original = bot._handle_mechanism_control
    @wraps(original)
    async def control(data, text, config):
        if await command(bot, data, text, config):
            return True
        return await original(data, text, config)
    bot._handle_mechanism_control = control
    original_execute = bot._execute_mechanism
    @wraps(original_execute)
    async def dispatch(data, proposal, config):
        if proposal['action'] in ACTIONS:
            return await execute(bot, data, proposal, config)
        return await original_execute(data, proposal, config)
    bot._execute_mechanism = dispatch
    original_state = bot._semantic_state
    @wraps(original_state)
    async def semantic(key, words, config, admin, event_context=None):
        result = await original_state(key, words, config, admin, event_context=event_context)
        if 'fish_audio' in config:
            result['语音模式（当前聊天）'] = summary(bot, key, config)
        return result
    bot._semantic_state = semantic
    original_ask = bot.ask_deepseek
    @wraps(original_ask)
    async def ask(key, words, *args, **kwargs):
        return await generation(bot, key, original_ask, key, words, *args, **kwargs)
    bot.ask_deepseek = ask
    original_send = bot._send_reply_sticker
    @wraps(original_send)
    async def attachments(scope, reply):
        image_ok = await original_send(scope, reply)
        voice_ok = await deliver(bot, scope, reply)
        return image_ok or voice_ok
    bot._send_reply_sticker = attachments
    original_generic = bot.send_msg_to_user
    @wraps(original_generic)
    async def send(data, config, text):
        result = await original_generic(data, config, text)
        if result and data.get('message_type') == 'private' and getattr(text, 'fish_tts', None):
            await deliver(bot, audio.scope_key(bot._mechanism_key(data)), text)
        return result
    bot.send_msg_to_user = send
    for name in ('on_group_message', 'on_private_message'):
        function = getattr(bot, name)
        def wrap(function):
            @wraps(function)
            async def incoming(data):
                key = bot._mechanism_key(data)
                if key and str(data.get('sender',{}).get('user_id') or data.get('user_id','')) != str(bot.bot_qq):
                    if key.startswith('private_') or bot._is_mentioned(data.get('raw_message',''), data.get('message',[])):
                        manager(bot).bump(audio.scope_key(key))
                return await function(data)
            return incoming
        setattr(bot, name, wrap(function))
    original_proactive = bot._generate_proactive
    @wraps(original_proactive)
    async def proactive(group, *args, **kwargs):
        return await generation(bot, 'group_' + str(group) + '_0', original_proactive, group, *args, family='public', **kwargs)
    bot._generate_proactive = proactive
    import private_wake
    old_generate, old_send = private_wake.generate, private_wake.send
    @wraps(old_generate)
    async def wake_generate(instance, user, *args, **kwargs):
        if instance is not bot:
            return await old_generate(instance, user, *args, **kwargs)
        return await generation(bot, 'private_' + str(user), old_generate, instance, user, *args, family='public', **kwargs)
    @wraps(old_send)
    async def wake_send(instance, user, reply):
        result = await old_send(instance, user, reply)
        if instance is bot and result:
            await deliver(bot, 'p:' + str(user), reply)
        return result
    private_wake.generate, private_wake.send = wake_generate, wake_send
    old_runner = bot.vision_image_cache_runner
    @wraps(old_runner)
    async def runner():
        async def cleanup():
            while bot.running:
                try:
                    manager(bot).cache.cleanup()
                except (OSError, audio.TTSError):
                    bot.log('WARN', '[FISH_TTS] cache_cleanup_failed')
                await asyncio.sleep(60)
        task = asyncio.create_task(cleanup())
        try:
            return await old_runner()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    bot.vision_image_cache_runner = runner
