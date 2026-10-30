# Google AI Studio 音频模型配置

`app/config.example.yaml` 的 `llm_providers` 里提供了 `google_ai_studio` 示例，把几个支持音频输入的模型加入主模型池，供“选择听觉模型”独立选用。本文只对应该示例，不涉及任何私有部署的提供方或配置。

- google_ai_studio：直连 Google 官方 OpenAI 兼容接口，示例中 `enabled: false`、密钥留空。
- 示例模型：`gemini-3.8-flash`、`gemini-3.5-flash-lite`、`gemini-3.6-flash`、`gemini-4-argon`。
- 四款官方规格均列出 Audio 输入；列表不是能力认证，也没有混入需要 Live 或 TTS 专用协议的模型。

## 填写密钥和使用

1. 复制 `app/config.example.yaml` 为 `app/config.yaml`。
2. 在 `llm_providers.google_ai_studio` 下填写你自己的 Google AI Studio 密钥（`api_key`），并把 `enabled` 改为 `true`。
3. 官方兼容地址：https://generativelanguage.googleapis.com/v1beta/openai/
4. 在群中@机器人或私聊发送“选择听觉模型”，选择 `google_ai_studio`，再选模型。建议先选 `gemini-3.8-flash`，用 10–20 秒普通 MP3 验收。

选定仅影响听觉，主模型不变；开启听觉同步会改为跟随主模型。配置文件每次请求重新加载，填 Key 不需要重启。密钥未填或提供方未启用时，独立听觉选择会提示接口/密钥缺失。

只加模型名不等于已验证调用；账号权限、地区、额度及中转音频格式仍以真实调用为准。如果你用的是第三方中转而不是 Google 官方接口，请自行在 `llm_providers` 中新增提供方，并确认它支持 `input_audio`。

## 官方依据（2026-10-25核对）

- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash
- https://ai.google.dev/gemini-api/docs/models/gemini-4-argon
- https://ai.google.dev/gemini-api/docs/openai/#audio-understanding
