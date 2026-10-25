"""
本地切换角色风格（等价于群里发"切换风格"）
改 config.yaml 的 deepseek.system_prompt + prompts/_current_style.txt
bot 在下一次生成回复时重新加载，无需重启
"""
import sys
from pathlib import Path

import yaml
from persona import load_persona
from style_catalog import available

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"


def main():
    style_files = available(BASE_DIR)
    styles = list(style_files)
    if not styles:
        print("prompts/ 下没有可用风格文件")
        sys.exit(1)

    config = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
    print("当前风格：" + load_persona(BASE_DIR, config)[1])
    print("可选风格：")
    for i, s in enumerate(styles, 1):
        print(f"  [{i}] {s}")
    choice = input(f"输入序号（1-{len(styles)}），或输入取消: ").strip()
    if choice in ("取消", "算了", "不改了"):
        print("已取消切换风格，保留原设置。")
        return
    if not choice.isdigit() or not 1 <= int(choice) <= len(styles):
        print("无效选择")
        sys.exit(1)

    style_name = styles[int(choice) - 1]
    prompt_text = style_files[style_name].read_text(encoding="utf-8-sig").strip()

    cfg_path = BASE_DIR / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config.setdefault("deepseek", {})["system_prompt"] = prompt_text
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    (PROMPTS_DIR / "_current_style.txt").write_text(style_name, encoding="utf-8")
    print(f"已切换为「{style_name}」")
    print("提示: bot 在下一次生成回复时重新加载，无需重启")


if __name__ == "__main__":
    main()
