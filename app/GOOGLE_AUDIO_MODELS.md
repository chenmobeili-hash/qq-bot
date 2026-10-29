# Google AI Studio 音频模型配置

本次只增加模型池选项，不自动切换主模型、视觉模型、听觉模型或听觉同步。

- a6api：追加 `gemini-3.8-flash`，沿用已有 a6api 接口和密钥；没有实测中转是否开放音频。
- google_ai_studio：直连 Google 官方 OpenAI 兼容接口，密钥初始留空。
- Google模型：`gemini-3.8-flash`、`gemini-3.5-flash-lite`、`gemini-3.6-flash`、`gemini-4-argon`。
- 四款官方规格均列出Audio输入；不添加比3.5 Flash-Lite更老的型号，也不混入需要Live或TTS专用协议的模型。

## 填写密钥和使用

在app/config.yaml的llm_providers.google_ai_studio.api_key填写你自己的Google AI Studio密钥。
本地配置中该提供方enabled=true，菜单可见；密钥未填时独立听觉选择会提示接口/密钥缺失。
公开版config.example.yaml保持enabled=false；复制示例时请填写密钥并自行启用。
官方兼容地址：https://generativelanguage.googleapis.com/v1beta/openai/

然后在群中@机器人或私聊发送“选择听觉模型”，选择google_ai_studio，再选模型。
建议先选gemini-3.8-flash，用10–20秒普通MP3验收。
选定仅影响听觉，主模型不变；开启听觉同步会改为跟随主模型。
配置文件每次请求重新加载，填Key不需要重启；如果VM尚未安装听觉功能及独立选择菜单，需先安装前两个功能包并只重启bot.py。
只加模型名不等于已验证调用；账号权限、地区、额度及中转音频格式仍以真实调用为准。

## VM仅合并配置

不要把本地config.yaml或其备份发到VM，也不要覆盖VM生产配置。
解压本配置包，使用bot自己的Python环境执行：

python add_google_audio_models.py --target /实际/app目录
python add_google_audio_models.py --target /实际/app目录 --apply

脚本只给a6api追加一个模型，并新增google_ai_studio提供方；若该提供方已经存在则保留已有密钥及启用状态，仅补模型。
不改当前提供方/模型、不改其他配置，保留注释和原换行，自动备份config.yaml。
不重启服务、不发联网请求；该包不含任何API密钥、原生产配置或聊天记录，也不包含bot.py及文献下载器。

## 官方依据（2026-10-25核对）

- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-4-argon
- https://ai.google.dev/gemini-api/docs/openai/#audio-understanding
