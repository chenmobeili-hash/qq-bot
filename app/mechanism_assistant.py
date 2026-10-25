"""Typed action suggestions, scoped delivery-gated confirmation, and a command reference."""
from collections import OrderedDict
from dataclasses import dataclass
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from whale_stickers import StickerReply


COMMAND_HELP = """群里建议先 @我；管理员指bot配置的admin_qq，不等于QQ群管理头衔。

【主动聊天与读书】
主动模式 开启 / 关闭 / 状态
主动模式 低频 / 标准 / 活跃（仅当前群；配置：恢复配置或默认频率）
主动阅读 开启 / 关闭 / 状态
开关和切档限管理员；切档不会自动开启。主动阅读需同时开启主动模式，读书频率独立。
主动模式也会留意本群图片：满足接话条件后每轮最多识别1张近10分钟的图，再决定是否回复。
看图同样遵守视觉群白名单、主动模式档位与静默时段；被@和私聊仍走原有直接识图。
主动识图有独立每日尝试额度，数值跟随当前档位（低频8/标准24/活跃30）；失败和沉默也计入。

【文档】需 @我，群内使用
挑一份你感兴趣的文档读
读文献 论文链接（也可先上传PDF/TXT/Markdown/DOCX，再发“读文献”）
读文献 文件名（可附问题；名称不唯一时会列候选）
也可自然说“提供《某书》的提要”：先查本群文件并给出建议，同一人确认后才阅读。
文献追问 你的问题
文献状态 / 忘记文献
也可自然问“这文章怎么样”“这本书有什么局限”。
PDF初读成功后，空闲30秒会自动按下一个100页范围抽样续读，受群静默时段限制。
逐页精读 / 继续阅读 / 暂停阅读 / 验收阅读 / 阅读进度（当前群、自己的当前PDF；无个人任务时使用本群共享任务）
也可说“这本逐页仔细读”“先停下”“已经够了”或“读到哪里了”，由主模型判断并按既有流程确认。
被艾特、私聊、定时/主动消息打断后，空闲自动恢复；明确暂停或验收后须恢复才继续。
精读只看PDF文字，不识别图片图表；详细文字和笔记有效5小时，长期保留累计读后感及最多3000字自选记忆。
进度持久保存；切换风格后按新人格续读。初读每100页抽样不等于逐页读完；精读会逐页调用模型。

【鲸鱼表情】群里设置限管理员，私聊设置只影响自己
鲸鱼表情 列表 / 状态 / 开启 / 关闭 / 蓝鲸 / 黑鲸
鲸鱼表情 好问题（或列表中的名称/ID）

【QQ视频、音频与WebP】
听觉同步 开启 / 关闭 / 状态（全局；开关仅管理员；支持“聽覺同步”）
私聊直接发音频文件或语音；群里 @我 听音频 / 听语音。继续听 / 补听 从本聊天本人成功进度接着听。
音频和音轨最多30分钟/64MB，每段约30秒，每轮最多5段、模型段合计300秒；采样音频缓存最多1小时。
原生音频输入无需先转文字；独立audio_input和同步主模型均需接口支持input_audio；QQ语音需接入层转码或本机可解码。
私聊可直接发视频、视频文件或WebP；群里 @我 看视频 / 看动图，再发送附件（10分钟内）。
也可先发附件，再 @我 看视频；只读取当前聊天里你自己近期发送的附件。
普通WebP按静态图识别；动态WebP最多6帧，视频随时长抽取最多40张真实画面；声音需启用听觉。
每批4张，初批覆盖前中后；模型判断需要时继续补看，每轮最多3批、模型段合计300秒。
继续看视频 / 补看视频：接着查看当前聊天中自己上次视频的未看采样（群里需@，私聊直接说）。
视频支持MP4/MOV、WebM/MKV、AVI，最大64MB/30分钟；WebP最大20MB/400帧。
原视频临时副本用完即删；采样图在内存最多保留1小时，容量不足可能提前淘汰；重启失效，文字记忆保留。

【B站】群里看视频需 @我；管理和互动限管理员
发B站链接或完整BV号：读取可用字幕/采样帧
字幕保留时间点；画面同样分批查看，手动读后可“继续看视频”，只补看本人本聊天缓存。
刷B站 猫猫：一次找视频
刷B站 开启 猫猫，独立游戏：本群自动浏览，并启用自主点赞评论
刷B站 关闭 / 状态 / 兴趣 科普，音乐
刷B站 互动开启 / 互动关闭
B站点赞 BV完整号
B站评论 BV完整号 评论文字（会真实公开发布）
B站诊断 BV完整号
管理员私聊：B站登录 → 扫码 → B站登录确认；B站登录状态

【其他】
搜索 关键词（别名：搜索一下 / 查一下 / 查查）
@我 识图：同条带图，或随后在有效期内发图
翻译：私聊发指令再发文件；群里 @我 翻译 后私聊发文件，译文回原群
取消翻译：私聊取消等待文件
切换风格：群里 @我 或私聊，显示当前风格，在当前聊天按菜单回复数字或“取消”
风格增强 开启 / 关闭 / 状态：管理员开启后，姓名、自称和称呼按当前角色；记忆保留，全局生效
修改模型：管理员直接发（别名：改模型 / 切换模型 / model），显示当前模型，在当前聊天回复数字或“取消”
视觉同步 开启 / 关闭 / 状态：开启跟随主模型，关闭恢复独立视觉配置；开关限管理员

【作用范围】
当前群：主动模式、主动阅读、刷B站及互动开关、群表情设置。
当前用户私聊：私聊表情设置。待确认操作绑定当前聊天和发言者。
文档：手动缓存按群和提问者隔离；主动宣布选中的书可在本群共同追问；忘记文献只清自己的缓存。
全局：切换风格、风格增强、修改主模型、视觉同步、听觉同步，影响所有群和私聊。
B站账号：全bot共用登录账号，但各群浏览/互动开关独立。
翻译：从哪个群预约，译文回哪个群；私聊直接预约则回私聊。

【自然表达与确认】
可以直接描述想做什么，不需要背指令。主模型每轮会判断是聊天、澄清还是提出操作建议。
例如让它多参与群聊、自己找书、换成某个人格或模型、调整看图方式、找视频，都可自然表达。
目标不明确时先问你；需要核对群文件时先查目录，读取新正文、修改设置或公开互动前列出建议。
可直接说“换成某某人格/模型”或“这个群活跃点”；程序会核验实际选项及权限。
收到程序列出的操作建议后，同一个人在同一个聊天中回复“可以”或“确认”才执行。
也可用“确认 确认码”；“取消/不用”放弃。2分钟过期，换话题或重启后失效。
帮助 / 指令列表 / 功能列表：查看本清单。
"""

# action -> (argument names, admin requirement, group-only, private-only)
ACTIONS = {
    "proactive_on": ((), True, True, False), "proactive_off": ((), True, True, False),
    "proactive_level": (("level",), True, True, False),
    "reading_on": ((), True, True, False), "reading_off": ((), True, True, False),
    "pick_document": ((), False, True, False), "read_document": (("url",), False, True, False),
    "read_group_file": (("name",), False, True, False),
    "document_deep": ((), False, True, False), "document_resume": ((), False, True, False),
    "document_pause": ((), False, True, False), "document_accept": ((), False, True, False),
    "document_progress": ((), False, True, False),
    "stickers_on": ((), False, False, False), "stickers_off": ((), False, False, False),
    "stickers_blue": ((), False, False, False), "stickers_black": ((), False, False, False),
    "send_sticker": (("name",), False, False, False),
    "bili_read": (("target",), False, False, False),
    "qq_media_read": ((), False, False, False),
    "video_more": ((), False, False, False),
    "bili_search": (("query",), False, False, False),
    "bili_on": (("topics",), True, True, False), "bili_off": ((), True, True, False),
    "bili_interactions_on": ((), True, True, False), "bili_interactions_off": ((), True, True, False),
    "bili_login": ((), True, False, True),
    "bili_login_confirm": ((), True, False, True),
    "bili_topics": (("topics",), True, True, False),
    "bili_like": (("target",), True, False, False),
    "bili_comment": (("target", "text"), True, False, False),
    "search": (("query",), False, False, False), "vision": ((), False, True, False),
    "translate": ((), False, False, False), "style_menu": ((), False, False, False),
    "model_menu": ((), True, False, False),
    "style_enhance_on": ((), True, False, False), "style_enhance_off": ((), True, False, False),
    "style_enhance_status": ((), False, False, False),
    "style_set": (("name",), False, False, False),
    "model_set": (("provider", "model"), True, False, False),
    "document_forget": ((), False, True, False),
    "translation_cancel": ((), False, False, True),
    "vision_sync_on": ((), True, False, False), "vision_sync_off": ((), True, False, False),
    "hearing_sync_on": ((), True, False, False), "hearing_sync_off": ((), True, False, False),
    "audio_read": ((), False, False, False), "audio_more": ((), False, False, False),
}


def scope_info(key):
    group = re.fullmatch(r"group_(\d+)_(\d+)", str(key))
    private = re.fullmatch(r"private_(\d+)", str(key))
    return ("group", group[2]) if group else ("private", private[1]) if private else None


def allowed(action, key, admin):
    scope = scope_info(key)
    if not scope or action not in ACTIONS:
        return False
    _, privileged, group_only, private_only = ACTIONS[action]
    privileged = privileged or (action.startswith("stickers_") and scope[0] == "group")
    return (not privileged or admin) and (not group_only or scope[0] == "group") and (not private_only or scope[0] == "private")


def validate(action, args, key, admin):
    if not allowed(action, key, admin) or not isinstance(args, dict):
        raise ValueError("Unsupported or unauthorized action")
    if set(args) != set(ACTIONS[action][0]):
        raise ValueError("Unexpected action parameters")
    for value in args.values():
        if (not isinstance(value, str) or not value.strip() or len(value) > 500
                or re.search(r"[\x00-\x1f]|\[CQ:|\[\[mechanism:|@", value, re.I)):
            raise ValueError("Invalid action parameter")
    args = {k: value.strip() for k, value in args.items()}
    if action == "read_document":
        parts = urlsplit(args["url"])
        if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password
                or any(c.isspace() for c in args["url"])):
            raise ValueError("Invalid document link")
    if action == "bili_read" and not (re.fullmatch(r"BV[0-9A-Za-z]{10}", args["target"]) or
            (urlsplit(args["target"]).scheme in ("https", "http") and
             urlsplit(args["target"]).hostname in ("www.bilibili.com", "bilibili.com", "b23.tv"))):
        raise ValueError("Invalid Bilibili target")
    if action in ('bili_like', 'bili_comment') and not re.fullmatch(r'BV[0-9A-Za-z]{10}', args['target']):
        raise ValueError('Bilibili interactions require a complete BV id')
    if action == 'proactive_level' and args['level'] not in ('低频', '标准', '活跃', '配置'):
        raise ValueError('Invalid proactive level')
    return {"action": action, "args": args}


def describe(proposal):
    action, args = proposal["action"], proposal["args"]
    fixed = {
        "qq_media_read": "读取当前聊天中你最近发送的QQ视频、音频或WebP；有可用听觉配置时读取实际音轨；已读取时直接讨论已有描述，不重复建议读取",
        "video_more": "接着补看本聊天本人上次QQ视频、WebP或手动B站视频中尚未查看的真实采样；已有描述足够回答时直接聊天，不重复读取；缓存最多1小时，重启失效",
        "proactive_on": "在本群开启主动模式：允许偶尔主动接话，并按主动阅读设置偶尔找群文档读",
        "proactive_off": "在本群关闭主动模式，同时暂停主动阅读",
        "reading_on": "在本群开启主动阅读：主动模式开启时，空闲时偶尔挑群文档阅读和分享",
        "reading_off": "在本群关闭主动阅读，保留主动接话和手动读文献",
        "pick_document": "从本群文件自主挑一份感兴趣的文档，下载实际正文后阅读并分享",
        "document_deep": "对当前可续读PDF开启逐页精读，从已保存的精读页码开始（首次第1页）；空闲自动读，被打断后自动恢复；精确笔记保留5小时，长期保留读后感和最多3000字自选记忆",
        "document_resume": "恢复当前PDF的空闲续读，从已保存的抽样或精读进度继续",
        "document_pause": "暂停当前PDF后续阅读，保留进度和阅读记忆",
        "document_accept": "验收当前阅读结果并停止后续自动续读，保留已保存的进度和记忆",
        "document_progress": "查看当前PDF的抽样/精读进度、未识别页与续读状态",
        "stickers_on": "开启当前聊天的自动鲸鱼表情", "stickers_off": "关闭当前聊天的自动鲸鱼表情",
        "stickers_blue": "当前聊天使用蓝鲸娘表情", "stickers_black": "当前聊天使用黑鲸娘表情",
        "bili_off": "停止本群自动刷B站和后续自主互动",
        "bili_interactions_on": "为本群的自动刷B站开启自主点赞和公开评论，使用已登录的B站账号",
        "bili_interactions_off": "停止本群后续自主点赞和评论，保留已有浏览分享设置",
        "bili_login": "在管理员私聊中生成B站登录二维码，扫码后该账号将用于已开启的B站功能",
        "bili_login_confirm": "检查管理员刚完成的B站扫码并保存成功取得的登录状态，供全bot共用",
        "vision": "开始识图：请在提示的有效期内发图片",
        "translate": "开始文件翻译：私聊发送文件；从群发起时译文作为文件回到当前群",
        "style_menu": "打开切换风格菜单；选定后全局生效，影响所有群和私聊",
        "style_enhance_on": "全局开启风格增强：按当前角色的真实姓名、自称和称谓扮演；保留共同记忆、真实权限和隐私规则",
        "style_enhance_off": "全局关闭风格增强：恢复原有持续身份与风格组合规则，不删除记忆",
        "style_enhance_status": "查看全局风格增强状态",
        "model_menu": "打开修改模型菜单；选定后全局生效，影响所有群和私聊；视觉同步开启时看图也跟随",
        "audio_read": "读取本聊天本人最近发送的音频或视频的真实音轨，需已配置听觉模型或听觉同步；不跨聊天读取",
        "audio_more": "从本聊天本人上一份媒体成功保存的听觉进度继续听，缓存最多1小时；不会把失败片段记为已听",
        "hearing_sync_on": "全局开启听觉同步，音频与视频音轨跟随主模型及接口；主模型和接口必须支持input_audio，实际调用后才能确认支持",
        "hearing_sync_off": "全局关闭听觉同步，恢复原独立audio_input配置，不修改主模型及视觉设置",
        "vision_sync_on": "全局开启视觉同步：所有群、私聊识图及B站采样帧使用当前主模型；以后跟随主模型切换",
        "vision_sync_off": "全局关闭视觉同步：所有群、私聊识图及B站采样帧恢复原独立视觉配置，不改变主模型",
        "document_forget": "清除你在本群的文献缓存、上传预约及当前共享文献的个人可见状态，不删除全群已读记录",
        "translation_cancel": "取消你当前私聊的等待翻译文件预约",
    }
    return fixed.get(action) or {
        "read_document": "读取指定文献：" + args.get("url", ""),
        "read_group_file": "读取本群文件：" + args.get("name", ""),
        "proactive_level": "将本群主动模式调为“" + args.get('level', '') + "”档；只改频率，不自动开启，保留当日用量",
        "bili_topics": "修改本群自动刷B站的兴趣：" + args.get('topics', ''),
        "bili_like": "用已登录B站账号为该视频真实点赞：" + args.get('target', ''),
        "bili_comment": "用已登录B站账号向该视频公开发表评论：" + args.get('target', '') + "；评论原文：" + args.get('text', ''),
        "style_set": "全局切换表达人格为“" + args.get('name', '') + "”；影响所有群和私聊，保留记忆",
        "model_set": "全局切换主模型为“" + args.get('provider', '') + ' / ' + args.get('model', '') + "”；视觉同步开启时视觉也跟随",
        "send_sticker": "发送鲸鱼表情：" + args.get("name", ""),
        "bili_read": "读取B站视频可用字幕和采样帧：" + args.get("target", ""),
        "bili_search": "按这个主题找一条B站视频并读取可用字幕或采样帧：" + args.get('query', '') + "；本次不会自动点赞或评论",
        "bili_on": "在本群开启自动刷B站，兴趣：" + args.get("topics", "") + "；同时启用自主点赞和公开评论",
        "search": "联网搜索：" + args.get("query", ""),
    }[action]


class SuggestedReply(StickerReply):
    def __new__(cls, text, key, ticket, sticker=None, persona_signature=None):
        value = super().__new__(cls, text, sticker, persona_signature)
        value.mechanism_key, value.mechanism_ticket = key, ticket
        return value


@dataclass
class Pending:
    ticket: str
    proposal: dict
    signature: str
    expires: float
    active: bool = False
    context: dict | None = None


MARKER = re.compile(r"\[\[mechanism:(\{[^\r\n]{1,3000}\})\]\]")


class MechanismAssistant:
    def __init__(self, clock=time.monotonic, log=None):
        self.clock = clock
        self.log = log or (lambda *args: None)
        self.pending = OrderedDict()

    def _prune(self):
        for key in list(self.pending):
            if self.pending[key].expires <= self.clock():
                self.pending.pop(key)

    def cancel(self, key):
        self.pending.pop(key, None)

    def guidance(self, key, config, admin):
        if not (config.get("mechanism_assistant", {}) or {}).get("enabled", True) or not scope_info(key):
            return ""
        from model_decision import instructions
        return instructions(key, config, admin)

    def extract(self, text, key, config, admin):
        pieces = re.split(r"(```[\s\S]*?```|`[^`\r\n]*`)", text)
        candidates = []
        marker_count = 0
        present = False
        for index in range(0, len(pieces), 2):
            present = present or '[[mechanism:' in pieces[index]
            matches = list(MARKER.finditer(pieces[index]))
            marker_count += len(matches)
            for match in matches:
                if text.rstrip().endswith(match[0]) and (match.start() == 0 or pieces[index][match.start()-1] == "\n"):
                    candidates.append(match[1])
            pieces[index] = MARKER.sub("", pieces[index])
            pieces[index] = re.sub(r"\[\[mechanism:[^\r\n]*$", "", pieces[index])
        cleaned = "".join(pieces).strip()
        if not present:
            return cleaned, None
        if marker_count != 1:
            self.log('WARN', '[MECHANISM] 建议拒绝：标记数量或格式无效')
            return cleaned, None
        if len(candidates) != 1:
            self.log('WARN', '[MECHANISM] 建议拒绝：标记不在独占末行')
            return cleaned, None
        if not self.guidance(key, config, admin):
            self.log('WARN', '[MECHANISM] 建议拒绝：功能关闭或聊天范围无效')
            return cleaned, None
        try:
            data = json.loads(candidates[0])
        except (ValueError, TypeError):
            self.log('WARN', '[MECHANISM] 建议拒绝：JSON无法解析')
            return cleaned, None
        try:
            if not isinstance(data, dict) or not isinstance(data.get('action'), str):
                raise ValueError()
            if not allowed(data['action'], key, admin):
                self.log('WARN', '[MECHANISM] 建议拒绝：动作不允许')
                return cleaned, None
            if set(data) != {"action", "args"}:
                raise ValueError()
            return cleaned, validate(data["action"], data["args"], key, admin)
        except (ValueError, TypeError, KeyError, AttributeError):
            self.log('WARN', '[MECHANISM] 建议拒绝：参数校验失败')
            return cleaned, None

    def prepare(self, key, proposal, signature, *, context=None):
        self._prune()
        self.pending.pop(key, None)
        pending = Pending(uuid.uuid4().hex[:6].upper(), proposal, signature, self.clock() + 120,
                          context=dict(context) if isinstance(context, dict) else None)
        self.pending[key] = pending
        while len(self.pending) > 128:
            self.pending.popitem(last=False)
        action = proposal.get('action')
        self.log('INFO', '[MECHANISM] 建议已准备 action=' + (action if action in ACTIONS else 'invalid'))
        return pending

    def activate(self, key, ticket):
        self._prune()
        pending = self.pending.get(key)
        if pending and pending.ticket == ticket:
            pending.active = True

    def respond(self, key, message, signature, config, admin):
        original = self.pending.get(key)
        self._prune()
        pending = self.pending.get(key)
        message = message.strip().rstrip("。！! ")
        yes = message in ("确认", "可以", "好", "好的", "行", "执行")
        coded = re.fullmatch(r"确认\s+([0-9A-Fa-f]{6})", message)
        no = message in ("取消", "取消选择", "取消選擇", "不用", "不用了", "不要", "算了", "不改了", "不切换了", "不切換了", "不触发")
        if not (yes or coded or no):
            self.cancel(key)
            return "none", None
        if not pending:
            return ("expired" if original or coded else "none"), None
        if no:
            self.cancel(key)
            return "cancelled", None
        if coded and coded[1].upper() != pending.ticket:
            return "mismatch", None
        if not pending.active:
            self.cancel(key)
            return "undelivered", None
        self.cancel(key)  # Consume before the first await in the caller; no double execution.
        if pending.signature != signature or not (config.get("mechanism_assistant", {}) or {}).get("enabled", True):
            return "changed", None
        try:
            proposal = validate(pending.proposal["action"], pending.proposal["args"], key, admin)
        except ValueError:
            return "denied", None
        if proposal['action'] == 'read_group_file' and pending.context:
            proposal['_paper_context'] = dict(pending.context)
        return "confirmed", proposal
