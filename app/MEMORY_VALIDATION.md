# 可选记忆校验与正常回答分离

2026-10-29：依据 VM 调查报告在本地实现相同处理契约；不是复制实际 VM 源码，也不声明源码哈希与 VM 一致。

先校验 JSON 信封、kind/reply/action、操作参数和权限。核心非法照旧拒绝整轮；核心通过后，单独校验可选 memory。memory 任一项非法就整批丢弃，不保留其中的合法项，不写盘；合法回复和原确认流程继续，不追加模型重试。

Decision 新增可选 memory_error=(code,index)。直接调用 validate_ops/commit 仍抛 ValueError 子类 MemoryValidationError，不允许其他调用方绕过校验。诊断只含固定原因码和0起操作位置，批量问题-1，超过3项为3；不记录正文、原始JSON、密钥或其他异常消息。

原因码：memory_not_list、memory_too_many、memory_channel_unavailable、memory_op_invalid、memory_fields_invalid、memory_scope_invalid、memory_identity_forbidden、memory_key_invalid、memory_key_duplicate、memory_retention_invalid、memory_text_invalid、memory_text_empty、memory_text_too_long、memory_text_forbidden。意外校验错误统一为memory_validation_failed/-1。

合法的非空 memory 仍不能放在 inspect 轮次；非法 memory 会被丢弃，但不会阻止原本合法的只读查询。非管理员写身份记忆仍被拒绝保存，后台操作仍需原有的参数核对和用户确认。精读临时资料过滤、5小时期限、记忆存储格式和软遗忘机制保持原样。

最终回复附注“本轮记忆操作未通过校验，未保存；不能算作已经记住或已经保存”。附注在操作确认文字整理后加上，避免长确认问句截断它；这不代表设置操作已执行。正常记忆仍走原原子提交，存储错误仍走既有写入失败提示。

提示词补齐字段白名单、scope与权限、key字符/长度、同scope同key唯一、text限制、recall只能标记本轮确实提供且使用过的笔记；不确定时可省略memory。

相关回归：python -m unittest test_memory_validation test_long_term_memory test_memory_decay test_model_decision test_decision_json_compat test_model_stream -q

请只运行明确选择的离线测试；不要直接unittest discover，test_send.py有真实QQ连接/发消息副作用。本地离线测试不等于实际VM或真实模型验收。已有VM修复请继续保留，不要为了本地新哈希覆盖VM自定义；流式r1增量包仍不改ask_deepseek、parse、Decision资料类或long_term_memory.py。
