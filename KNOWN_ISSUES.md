# 已知问题

本地离线回归共 606 项，602 通过，以下 4 项回复长度控制测试失败，0 项测试错误。这些失败在图片专用代理及本次公开消息更新前已经存在，本次未修改对应控制逻辑。

- `test_reply_policy.ChatFlowTests.test_empty_reasoning_output_retries_without_saving_empty_history`
- `test_reply_policy.ChatFlowTests.test_ignoring_rewrite_still_cannot_flood`
- `test_reply_policy.ChatFlowTests.test_overlong_output_rewrites_once_and_saves_sent_answer`
- `test_reply_policy.ReplyPolicyTests.test_hard_limits_for_pathological_output`

因此不能依赖当前测试结果保证异常模型输出始终符合回复长度限制。该问题尚未在本副本中修复。

PDF 精读仅处理文字层；视频和 B站只查看有限画面及可用字幕，不进行音轨转写。远程网络和模型服务需另行验收。
