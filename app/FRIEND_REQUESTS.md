# 自主选择好友申请

接收OneBot v11上报的request/friend事件，按实际事件中的self_id、user_id、flag绑定目标。请求只进入独立队列，后台空闲时用当前主模型、当前人格和申请者本人的私聊长期笔记判断accept通过、reject拒绝或defer暂不处理。未认识的人也可因友善或共同兴趣被接受，不强制拒绝陌生人；验证留言是待判断资料，不是权限证明。

主模型只输出decision与枚举reason，不可提供或改写flag、目标QQ、备注、消息或管理权限。程序只调用set_friend_add_request，approve显式传true/false；不顺带发送私信、同意加群、修改管理员名单或替对方开启私聊唤醒。加好友本身不改变现有私聊触发、模型调用及账号限制规则。没有新增头像/个人资料抓取，也不跨群合并记忆。

## 开关

- 好友申请 开启 / 关闭 / 状态（好友申請 開啟 / 關閉 / 狀態也可）。
- 仅config.admin_qq授权管理员可管理或查询；群与私聊均可，设置全局生效。
- 普通自然表达也可由主模型提出friend_requests_on/off/status建议，沿用本人、同聊天确认及权限核验；每个好友请求本身不再逐项请管理员批准。

当前已有部署在未配置时默认开启这项新能力；可加friend_requests: {enabled: false}提供初始关闭值。管理员指令会持久保存覆盖值。开关不改原config.yaml。公开发布的示例配置显式关闭，首次部署者自行开启。

## 队列与恢复

friend_requests.sqlite3是私人数据，权限600，包含待处理的请求flag、验证留言、申请者QQ及去重记录，不能随源代码上传。安装不预建或填充该库；收到有效事件或管理员修改开关才创建。状态查询不公开名单、留言、flag或模型原文。普通日志仅记枚举决定/结果/原因；对应申请者的私聊时间线记录处理结果。

请求先落盘再排队，忙碌、主模型关闭或断线时不处理。进程重启后仍能从原待办恢复，但需部署环境先恢复启动bot进程。只处理7天内收到/上报的有效请求；过期后清除flag及留言，不调用QQ接口。未完成队列最多128条，不承诺无限接收。终态元数据留30天，模型预算按滚动24小时最多20次预留、同一申请者至少间隔10分钟；这些计数也持久保存。

每次纯模型判断最多45秒，不增加内容修复调用；失败或不完整JSON保留待办，最多重试一次，受10分钟同人冷却与总预算限制。两次失败标记model_failed，留待人工检查，不擅自拒绝。模型主动defer只标记暂不处理，不反复调用模型劝自己改决定；可在QQ客户端自行处理。

只读模型生成阶段中断，120秒租约后可恢复；同一请求在调用QQ接口前先持久化executing并清除磁盘中的flag/留言，跨重启不重放。一旦进入此阶段，接口超时、异步受理或回执不明均标unknown；不会自动重复通过/拒绝，也不能保证进程被强杀的临界点恰好执行成功。需在QQ客户端核对。只有OneBot status=ok且retcode=0才记accepted/rejected，data=null是该接口正常返回，不应当作失败。

处理记录区分pending、deciding、executing、accepted、rejected、deferred、model_failed、api_failed、unknown、expired。关闭开关会保留待办，阻止尚未提交的新操作；已经交给QQ的操作不能撤回。生成期间开关、人格、模型路由或登录账号改变，将放弃旧决定并等待重新判断。

## 接入层验收

需要SnowLuma/NapCat实际实现并上报好友请求事件及set_friend_add_request接口；仅安装Python代码不证明你的接入层支持。请检查接入层是否已有自动同意、屏蔽请求等规则，避免先于bot处理；本功能不修改接入层配置。请用一份新的真实好友申请验收，核对队列、结构化决定和QQ好友列表，不能只以模型说愿意添加作为成功。

依据：[OneBot请求事件](https://github.com/botuniverse/onebot-11/blob/master/event/request.md)、[OneBot好友请求接口](https://github.com/botuniverse/onebot-11/blob/master/api/public.md#set_friend_add_request-处理加好友请求)。

