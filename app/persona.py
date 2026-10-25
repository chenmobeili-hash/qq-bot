"""Load a stable identity and the current editable TXT style on every request."""
import hashlib
import long_term_memory
from pathlib import Path


def load_persona(base_dir, config):
    root = Path(base_dir)
    import style_catalog
    import style_enhancement
    directory = root / "prompts"
    marker = directory / "_current_style.txt"
    name = marker.read_text(encoding="utf-8-sig").strip() if marker.exists() else "小鲸鱼"
    safe = bool(name) and name not in (".", "..") and not any(c in name for c in '/\\:')
    if not safe:
        name = "小鲸鱼"
    resolved = style_catalog.resolve(root, name)
    if resolved:
        name, style_file = resolved
        style = style_file.read_text(encoding="utf-8-sig").strip()
    else:
        name = style_catalog.decoded(name)
        style = config.get("deepseek", {}).get("system_prompt", "").strip()
    if not style:
        fallback = style_catalog.resolve(root, "小鲸鱼")
        style = fallback[1].read_text(encoding="utf-8-sig").strip() if fallback else "你是一个群聊助手。"
        name = "小鲸鱼"
    core_file = root / "personas" / "内在人格.txt"
    core = core_file.read_text(encoding="utf-8").strip() if core_file.exists() else ""
    prompt = (f"[持续身份与共同记忆原则]\n{core}\n\n[当前表达人格：{name}]\n{style}\n\n"
              "[人格切换规则]\n当前表达人格决定本轮语气、称呼和角色背景；"
              "持续身份维持共同记忆、事实准确与隐私边界。历史助手台词只说明当时的表达方式，"
              "不要据此切回旧人格。只使用本轮提供的历史和记录，不声称拥有未提供的记忆。")
    prompt += long_term_memory.identity_prompt(root, config)
    prompt += style_enhancement.instructions(root, name)
    signature = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return prompt, name, signature
