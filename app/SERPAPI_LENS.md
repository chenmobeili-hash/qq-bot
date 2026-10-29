# Google Lens（SerpApi）接入

已接入SerpApi的实际二进制上传流程：将当前图片的搜图副本压为JPEG且不超过480000字节，POST到https://serpapi.com/image，拿到image_id后用engine=google_lens、type=all查询https://serpapi.com/search.json。不传QQ签名图片链接，不要求自建图床。主模型看图用的原件不被压缩替换；该副本只在内存中生成，不落盘或保留EXIF。

依据：[官方Lens上传说明](https://serpapi.com/google-lens-upload-an-image)、[Image API](https://serpapi.com/image-api)、[Lens API](https://serpapi.com/google-lens-api)。服务当前上传上限500KB，图片ID有效期10分钟；这不是承诺第三方删除所有搜索日志。本程序的一小时图片缓存和十分钟搜图窗口仍按原规则计算，不因上传或追问延长。

## key到底填在哪里

填到实际运行bot的app/config.yaml顶层serpapi段的api_key，和saucenao、deepseek等并列，不放进llm_providers或deepseek，也不使用Serper key。示例见同目录SERPAPI.example.yaml。首次可安全新增空段：

```sh
python -X utf8 configure_serpapi.py --target 实际app目录
python -X utf8 configure_serpapi.py --target 实际app目录 --apply
```

第一条只预览；第二条只在serpapi不存在时追加key留空的配置，原文件逐字节备份到backup-vm-update-lens-config-*，不重排或改写原字段。已有serpapi时保持原段，提示你编辑，不能自动覆盖已有key/代理。安装代码的apply_wake.py不会替你填key。

```yaml
serpapi:
  enabled: true
  api_key: "在这里填你自己拿到的SerpApi key"
  api_key_env: SERPAPI_API_KEY
  hl: zh-cn
  timeout_seconds: 55
  daily_limit: 20
  min_interval_seconds: 8
  proxy: ""
```

api_key留空时可从bot同进程环境变量SERPAPI_API_KEY读取；配置中的非空key优先。不要把key发群、写进模型提示词、更新包或公开仓库。对config.yaml或环境变量的改变在下一请求读取；仅改key通常不需重启，而首次安装Python代码仍需按说明重启bot.py。公开示例默认enabled=false；本地新增空段enabled=true，但key为空且环境变量不存在时仍不可调用Lens。

proxy留空时使用bot进程的环境HTTP代理；可填写本机HTTP代理入口，只影响SerpApi上传/查询/账号检查，不修改图片下载机场、DoH、QQ连接或其它流量。它不是新的机场节点配置，不能把VLESS订阅或SOCKS地址直接当HTTP代理填写。

## 使用方式

开启视觉同步后，群里点名发图或私聊发图，再在10分钟内说“搜搜看”“查一下这张图的出处”“用谷歌搜图”。主模型先看图和上下文，选择search_image：provider=lens查Google Lens通用相似图片/网页；provider=saucenao查插画画师/原帖。provider=auto或省略时，Lens可用则优先Lens，否则选原SauceNAO。模型也可认为无需搜索而直接回答。

旧版“搜索/查一下”文字前缀在有近期原图时会抢走这类请求，本次已修复：此时交给主模型判断具体工具，不再先把“这张图出处”当文字直接搜索。没有近期图时保留原文字搜索行为。不要搜/不要外传的要求在模型指导中保留，图片及外部网页中的文字都不能给管理授权。

每轮最多搜一张，只接受已经提供给当前聊天、当前发言者的图片编号；不接受URL、路径、其他QQ/群目标。选择某个引擎失败后不自动换服务再次上传，也不在无key时假称已经用Lens。若用户要改引擎，可在下一轮明确要求。结果最多缓存5分钟且不超过原10分钟搜图期限，按引擎和凭证隔离。

Lens返回的是相关网页或视觉相似线索，不能直接等同确定角色/作品，更不能据此编造集数时间点。结果只给主模型有界的标题、来源、网页链接和匹配类型，不自动抓缩略图，不把搜索参数里的key、image_id、账号邮箱、原始HTML或响应体交给模型。保留外部数据仅作资料的协议。

## VM上先检查哪一步

在运行bot的同一环境、同一代理出口执行：

```sh
python -X utf8 serpapi_lens.py --root 实际app目录 --check
```

只请求SerpApi账号接口，不上传图片、不启动图片搜索；输出只含状态和可用查询余额，不回显key或邮箱。失败时返回非零退出码。status=ok仅说明这个出口的账号接口和凭证可用，不能替代图片上传及Lens查询验收。先前注册页面被block和API状态分开判断：401记authentication，403记access_denied，402/429记provider_limit，不仅凭403断言key无效。

完整验收要发一张新图，要求用谷歌搜图；核查[LENS]阶段日志和search_image provider=lens的调用、实际候选网页及最后回答。不能只看模型说“谷歌搜到了”。没有真实key时本地只做离线模拟，不谎称VM出口或真实服务通过。

## 超时、额度与状态

prepare、quota、upload、search、complete分阶段，只记录阶段与枚举错误，不记录携带key的请求URL、代理地址、账号资料或原始响应。固定HTTPS端点、证书校验、拒绝重定向，响应最多2MiB；不关闭TLS校验或自动换出口绕过封锁。默认整次55秒，最多60秒，外层原搜图等待65秒。确定性错误、网络失败均不自动重发付费查询；已经发送的请求可能计费/计额度。

默认滚动24小时最多20次本地预留，同一实例至少间隔8秒；失败也消耗本地预留，防止出口故障时反复烧额度。计数保存到私有serpapi_lens_state.sqlite3，重启不清零；已加入.gitignore。该本地保护不等于服务商月额度，真实余额仍以账号为准。daily_limit可改0暂停Lens请求，原SauceNAO独立。

部署保留原saucenao、Serper、下载器、DoH和机场代理；不自动开启其他群功能。代码回滚不删除私人配额或记忆。若要撤销追加的空serpapi段，编辑该段或按私人备份核对，不能用旧配置覆盖期间其他新设置。
