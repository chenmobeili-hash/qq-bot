# 完整收齐后返回的流式接收

2026-10-29：底层默认使用 Chat Completions 的 `stream=True`，把 `delta.content` 拼成完整回复后，恢复成原来的 `choices[].message.content` 和 `finish_reason`。群里仍然按原来分段发送，不会随 token 刷屏。流式不是新的模型，也不改变主模型、人格或模型池。

已覆盖主聊天（包含自然语言操作、长期笔记、上下文压缩、好友判断和主动发言共用的 DecisionTransport）、文献首读/续读/挑选、翻译、独立识图、视频补看、听觉。图片及音频输入参数原样传递，不把它们改成文字。

## 完成和失败

- 只拼可见文本；服务商的 reasoning_content 等推理字段不进回复、记忆或日志。
- 收到结束原因并正常结束迭代后才返回。连接断开、缺少结束原因、超时、非法结构、取消均不返回半截内容，不执行半截 JSON。每个选项分开拼接。
- `finish_reason=length` 保留，让原有调用者继续判断截断。结构化聊天仍然严格校验 JSON，听觉/续读仍按原规则确认完整结果后才提交进度。
- 不增加流式网络重试。关闭 SDK 的隐式重试，由原操作逻辑负责：阅读仍是最多两次、每次约145秒、间隔5秒，整轮最多300秒；断流属于连接失败，可在这层预算内重试。
- 只有请求尚未返回响应流、400/422 明确拒绝 stream/streaming 时，允许同一总预算内回退一次普通请求；按接口与模型记住1小时，最多64条。401/403/429、连接错误、半路断流不触发回退。原有 response_format 兼容仍独立处理。
- 有些网关忽略 stream=True，直接返回 application/json：读取完整 JSON 并校验，不再发第二个请求。
- 这个接收器面向文本结果；refusal、tool_calls、function_call、输出音频不会被当作普通聊天文本执行。多模态输入不受这条限制。

## 时间及资源

每次调用的绝对预算取原来的请求 timeout（包括 with_options 设置的 timeout）；未给定时按90秒处理，外层更短的截止时间仍优先。首段等待和段间空闲也受该次预算约束。流式小块不会重置总预算，兼容回退也不重新计时。取消/超时会关闭响应，清理最多额外等待1秒。缓冲上限4MiB、10万个块、16个选项，超出直接失败。

`[MODEL_STREAM]` 日志有 source、阶段、耗时、块数及字节数，不记录正文、推理、密钥或接口地址。可看到 connected、first_chunk、first_text、complete、unsupported_fallback、json_complete、nonstream_complete、failed、cancelled 或超时阶段。仅 connected 代表响应头已到，不代表收到正文；complete、json_complete 或 nonstream_complete 才是成功收齐。

默认启用，不必编辑 config.yaml。要临时恢复非流式，在启动 bot 的同一进程环境设置 `QQ_BOT_STREAMING=0` 并仅重启 Python bot；删掉该变量或设为1后重启恢复。该开关不改模型、配置文件或网络出口。

## 实际能力和验收

流式可能减少代理长时间没有正文数据导致的断连；如果服务商本来就不支持流式、代理缓存整个响应，或模型迟迟不出首段，它不能保证提速，更不能修复模型服务本身的故障。流式不会保证更省 token；失败后的操作级重试仍可能计费。

更新包只合并模型请求相关函数与新增 import，不覆盖 VM 的 paper_reader.py、图片机场代理、DoH 配置、私人配置、人格及记忆。qq_media.py 只合并 describe_frames，其他下载函数保留。未知函数差异会停下，禁止为了安装而整文件覆盖网络补丁。

离线测试：`python -m unittest test_model_stream test_model_decision test_reading_retry -q`。测试使用假密钥及 httpx.MockTransport，涵盖真实 SDK 的 SSE 字节拆分、中文、网关直接回 JSON、断流、取消、原图/音频参数和阅读页码不前移；不是 VM/真实模型验收。

VM 安装后先只发一句普通聊天，查看 first_chunk/first_text/complete 和完整回复；再验自然语言操作的原确认流程、图片及一次续读。对照同一模型、相近输入的首字/总耗时和失败次数，才能判断该上游是否受益。部署者检查在途任务、空闲时仅重启 bot.py，不必重启 QQ/SnowLuma。

协议参考：[OpenAI Streaming API responses](https://developers.openai.com/api/docs/guides/streaming-responses)。
