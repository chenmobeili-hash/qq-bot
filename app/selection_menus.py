"""Scoped, cancellable model/style menus; no model calls for menu navigation."""
from pathlib import Path
import re
import time

import yaml
import style_catalog
from persona import load_persona

CANCEL_WORDS = frozenset(('取消', '取消选择', '取消選擇', '不用', '不用了', '不要',
                          '算了', '不改了', '不切换了', '不切換了', '不触发'))
MODEL_COMMANDS = frozenset(('修改模型', '改模型', '切换模型', 'model'))
TTL = 120


def current(bot, config, kind):
    if kind == 'model':
        provider = config.get('current_provider') or getattr(bot, 'current_provider', '') or '未指定'
        model = config.get('current_model') or config.get('deepseek', {}).get('model') or getattr(bot, 'current_model', '') or '未指定'
        return str(provider), str(model)
    return load_persona(bot.BASE_DIR, config)[1]


def label(bot, config, kind):
    value = current(bot, config, kind)
    return '当前模型：' + ' / '.join(value) if kind == 'model' else '当前风格：' + value


def title(kind):
    return '修改模型' if kind == 'model' else '切换风格'


def invalidate(bot, key):
    transport = getattr(bot, 'decision_transport', None)
    if transport is not None:
        transport.invalidate(key)


async def publish(bot, data, config, state, lines):
    key = bot._mechanism_key(data)
    if not key:
        return False
    state.update(expires=time.monotonic() + TTL, ready=False)
    for other, old in list(bot.user_states.items()):
        if old.get('kind') in ('model', 'style') and old.get('expires', 0) <= time.monotonic():
            bot.user_states.pop(other, None)
    bot.user_states[key] = state
    while len(bot.user_states) > 128:
        bot.user_states.pop(next(iter(bot.user_states)))
    lines += ['回复数字选择，回复“取消”退出；2分钟内有效。', '选定后全局生效，影响所有群和私聊。']
    try:
        delivered = await bot.send_msg_to_user(data, config, '\n'.join(lines))
    except BaseException:
        if bot.user_states.get(key) is state:
            bot.user_states.pop(key, None)
        raise
    if bot.user_states.get(key) is state:
        if delivered:
            state['ready'] = True
        else:
            bot.user_states.pop(key, None)
    return True


async def open_model(bot, data, config):
    actor = str(data.get('sender', {}).get('user_id', ''))
    if not bot.is_admin(actor, config):
        await bot.send_msg_to_user(data, config, '❌ 你没有权限修改模型！')
        return True
    providers = [name for name, item in config.get('llm_providers', {}).items() if item.get('enabled')]
    if not providers:
        await bot.send_msg_to_user(data, config, '当前没有可用的提供方。\n' + label(bot, config, 'model'))
        return True
    state = {'kind': 'model', 'stage': 'provider', 'choices': providers,
             'baseline': current(bot, config, 'model')}
    return await publish(bot, data, config, state,
        ['正在修改模型', label(bot, config, 'model'), '请选择提供方：']
        + [f'  [{i}] {name}' for i, name in enumerate(providers, 1)])


async def open_style(bot, data, config):
    styles = list(style_catalog.available(bot.BASE_DIR))
    if not styles:
        await bot.send_msg_to_user(data, config, '当前没有可选风格。\n' + label(bot, config, 'style'))
        return True
    state = {'kind': 'style', 'stage': 'style', 'choices': styles,
             'baseline': current(bot, config, 'style')}
    return await publish(bot, data, config, state,
        ['正在切换风格', label(bot, config, 'style'), '可选风格：']
        + [f'  [{i}] {name}' for i, name in enumerate(styles, 1)])


async def set_model(bot, data, provider, model):
    """Also used after a separately confirmed natural-language operation."""
    config = bot.load_config()
    actor = str(data.get('sender', {}).get('user_id', ''))
    if not bot.is_admin(actor, config):
        await bot.send_msg_to_user(data, config, '当前账号没有修改模型的权限，未修改设置。')
        return True
    entry = bot.get_provider_config(config, provider)
    if not entry.get('enabled') or model not in bot.get_available_models(config, provider):
        await bot.send_msg_to_user(data, config, '该提供方或模型当前不可用，请重新选择。')
        return True
    config['current_provider'], config['current_model'] = provider, model
    config.setdefault('deepseek', {}).update(model=model, api_key=entry.get('api_key', ''), endpoint=entry.get('endpoint', ''))
    with (Path(bot.BASE_DIR) / 'config.yaml').open('w', encoding='utf-8') as stream:
        yaml.safe_dump(config, stream, allow_unicode=True, default_flow_style=False, sort_keys=False)
    bot.current_provider, bot.current_model = provider, model
    await bot.reload_llm_client(config)
    await bot.send_msg_to_user(data, config, '✅ 已切换模型。\n' + label(bot, config, 'model'))
    return True


async def handle(bot, data, command, config):
    key = bot._mechanism_key(data)
    if not key:
        return False
    raw = data.get('raw_message', '')
    other_ats = re.findall(r'\[CQ:at,qq=([^,\]]+)', raw)
    other_ats += [str(row.get('data', {}).get('qq', '')) for row in data.get('message', [])
                  if isinstance(row, dict) and row.get('type') == 'at']
    if data.get('group_id') is not None and any(qq not in (str(bot.bot_qq), 'all') for qq in other_ats):
        return False
    command = command.strip().rstrip('。！! ')
    opening = command in MODEL_COMMANDS or command == '切换风格'
    if opening:
        if command == '切换风格' and data.get('group_id') is not None and not bot._is_mentioned(raw, data.get('message', [])):
            return False
        # A denied model command must not destroy an existing style selection.
        if command in MODEL_COMMANDS and not bot.is_admin(str(data.get('sender', {}).get('user_id', '')), config):
            return await open_model(bot, data, config)
        invalidate(bot, key)
        bot._get_mechanism_assistant().cancel(key)
        group_files = getattr(bot, 'group_file_state', None)
        if data.get('group_id') is not None and group_files is not None:
            group_files.cancel(data['group_id'], str(data.get('sender', {}).get('user_id', '')))
        bot.user_states.pop(key, None)
        return await (open_style(bot, data, config) if command == '切换风格' else open_model(bot, data, config))
    state = bot.user_states.get(key)
    if not state or state.get('kind') not in ('model', 'style'):
        return False
    kind = state['kind']
    if command in CANCEL_WORDS:
        invalidate(bot, key)
        bot.user_states.pop(key, None)
        await bot.send_msg_to_user(data, config, f'已取消{title(kind)}，未修改设置。\n' + label(bot, bot.load_config(), kind))
        return True
    if not command.isascii() or not command.isdigit():
        bot.user_states.pop(key, None)
        return False  # A new topic exits the menu and is processed as normal chat.
    invalidate(bot, key)
    fresh = bot.load_config()
    reason = None
    if time.monotonic() >= state['expires']:
        reason = '选择已过期，请重新打开菜单。'
    elif not state.get('ready'):
        reason = '菜单尚未确认送达，请重新打开菜单。'
    elif kind == 'model' and not bot.is_admin(str(data.get('sender', {}).get('user_id', '')), fresh):
        reason = '当前账号没有修改模型的权限，已退出选择。'
    elif state['baseline'] != current(bot, fresh, kind):
        reason = '当前设置已变化，已退出旧菜单，请重新选择。'
    if reason:
        bot.user_states.pop(key, None)
        await bot.send_msg_to_user(data, fresh, reason + '\n' + label(bot, fresh, kind))
        return True
    if len(command) > 6 or not 1 <= int(command) <= len(state['choices']):
        await bot.send_msg_to_user(data, fresh, f'无效选择，当前正在{title(kind)}。回复有效数字，或回复“取消”。\n' + label(bot, fresh, kind))
        return True
    chosen = state['choices'][int(command) - 1]
    if state['stage'] == 'provider':
        models = bot.get_available_models(fresh, chosen)
        if not bot.get_provider_config(fresh, chosen).get('enabled') or not models:
            bot.user_states.pop(key, None)
            await bot.send_msg_to_user(data, fresh, '该提供方当前不可用，请重新选择。')
            return True
        return await publish(bot, data, fresh,
            {'kind': 'model', 'stage': 'model', 'provider': chosen, 'choices': list(models), 'baseline': state['baseline']},
            ['正在修改模型', label(bot, fresh, 'model'), f'请选择 {chosen} 的模型：']
            + [f'  [{i}] {name}' for i, name in enumerate(models, 1)])
    bot.user_states.pop(key, None)  # Consume before awaiting any operation.
    if kind == 'model':
        return await set_model(bot, data, state['provider'], chosen)
    directory = Path(bot.BASE_DIR) / 'prompts'
    styles = style_catalog.available(bot.BASE_DIR)
    if chosen not in styles:
        await bot.send_msg_to_user(data, fresh, '该风格已不在可选列表中，请重新选择。')
        return True
    (directory / '_current_style.txt').write_text(chosen, encoding='utf-8')
    bot._persona_prompt(fresh)
    save_history = getattr(bot, '_save_conversations', None)
    if callable(save_history):
        save_history()
    await bot.send_msg_to_user(data, fresh, '✅ 已全局切换风格。\n' + label(bot, fresh, 'style'))
    return True
