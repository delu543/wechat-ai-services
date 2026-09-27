# 运行手册

## 适用环境与首次使用

这是源码开发预览：Apple Silicon Mac、已登录官方桌面微信、标准 CPython 3.12/3.13。
新电脑不能继承旧电脑登录、目录签名、证书或模型路径。Windows/Intel 路线尚未验收；
不要运行 macOS 网络命令或把 Windows 聊天导出测试冒充直播测试。

给新电脑 Codex 的第一句话：

```text
请获取 https://github.com/delu543/wechat-data-extraction 的完整源码，
阅读 AGENTS.md 和 portable_skill/skills/wechat-replay-word/SKILL.md。
我想把这个视频号账号的可访问直播回放全部整理成完整 Word，只需最终文字稿。
先检查当前电脑与依赖；需要我登录或确认临时接入时一次说清，其余步骤自动处理。
不要覆盖已有项目，不要上传账号数据，不要逐条让我打开回放。
```

安装本 Skill 可使用仓库 Plugin，也可：

```bash
npx -y skills add delu543/wechat-data-extraction --skill wechat-replay-word
```

`<scripts>` 表示本 Skill 真实目录下 scripts；`<python>` 为直播专用运行时解释器。
不要把示例命令中的占位符直接执行。使用 argv 数组传递名称和路径，禁止拼接 shell。

```text
python3 <scripts>/bootstrap.py doctor
CPython3.12或3.13 <scripts>/bootstrap.py install
```

安装会下载固定版本 Python 包至用户自己的 `WeChatAIServicesReplayWord/runtime`，不会改微信、
网络或证书，不会初始化聊天数据库。未固定全部传递依赖哈希，不能宣传为可复现锁定发行。
发现现有他人环境或非私有目录时停止，不覆盖。模型不自动下载：先复用明确可用的本地
MLX Whisper 模型；没有时告知模型来源、许可、大小和磁盘需求，获得下载授权后从
`mlx-community/whisper-large-v3-turbo` 官方 Hugging Face 仓库取得固定 revision 的
config/tokenizer JSON 与 safetensors，记录 revision/hash。不要执行模型仓库自定义代码。

## 建立任务

1. 用户提供账号名及一条回放分享链接。通过用户可见页面核对种子回放标题。
2. 只读列出 `networksetup -listallnetworkservices` 并核对当前使用的服务名。不能假定叫 Wi-Fi，
   也不能假定代理端口是 7897。多个活动出口无法确定时只问所需服务，不关 VPN。
3. 用已验证本地模型运行 `bootstrap.py configure --name <账号名> --seed <分享链接>
   --model <本地模型> --service <当前网络服务>`。返回私有 task_root。
4. 为该任务运行 `prepare_capture.py <task_root>/capture-session --target <账号名>
   --seed-title <标题> --service <当前网络服务>`。默认回环端口18089，可显式指定空闲端口。
   它只创建私有配置/48小时任务 CA 和网络基线，不信任证书、不改网络。
   PAC/自动发现、认证代理、HTTP/HTTPS出口不一致等复杂环境停止审阅，不盲改。

## 临时接入的逐项说明与批准

用户必须知道：将临时信任**本任务独立 CA**，让当前指定网络服务的 HTTP/HTTPS 流量经过
本机回环代理，只解密 `channels.weixin.qq.com` 与 `res.wx.qq.com`，其他域名透传；
上游沿用原来的本地 VPN/代理或直连，TLS 验证不关闭。不读 Cookie 文件，不持久化完整流量。
页面只加入窄范围目录读取适配器，不发送聊天、不改微信安装包。

证书信任并非零风险；NameConstraints 是纵深保护，不是已经证明的 macOS 信任沙箱。
必须确认目标、服务、域名、证书指纹、期限和收尾办法，再运行：

```text
<python> <scripts>/capture_session.py start <task_root>/capture-session
  --minutes 15 --confirmed-capture
```

用户明确要求延长时可指定已同意的分钟数（最多240），不默认永久运行。
以独立后台进程运行并记录 PID；用户只需打开/刷新并播放种子一次。脚本绑定账号后
以原页面 API 自动分页，每页间隔800ms，不逐场点击。读取 `capture/status.json` 和
`catalog.private.json` 的计数/末页标志，不把签名 URL、账号 ID 或游标贴进对话。

接入在目录完整、期限到达、代理退出或正常中断时收尾；SIGKILL/断电不保证 finally 执行。
恢复任务先读 lifecycle，未收尾立即执行：

```text
<python> <scripts>/capture_session.py restore <task_root>/capture-session
```

独立网络 watchdog 会恢复其仍占用的代理设置，但不能替代证书撤销。
核验网络字段与基线一致、任务 CA 信任及 Keychain 条目不存在、回环代理不再监听。
cleanup 不删除证书证据文件。若用户中途改了网络，比较恢复会报告冲突，不覆盖新设置。
捕获代码结构变化、账号歧义、CAPTCHA、付费墙、登录失败或限流时停止相应请求，不能扩大域名兜底。

## 批处理与可视面板

要求目录 complete=true、末页 continueFlag=0；记录总数/日期跨度/目录摘要。
配置已在 task_root/batch_config.json，运行：

```text
<python> <scripts>/run_account_batch.py <task_root>/batch_config.json
<python> <scripts>/dashboard/server.py --task-root <task_root>
```

打开面板进程返回的 localhost 随机路径，保持用户可见。只有只读 GET 接口。
面板从真实文件和进程读取：目录、音频验证、转写分块、Word 哈希与排版检查、失败原因。
代码和任务目录可分离；进程检测必须同时匹配实际脚本与对应配置，不能仅看固定 PID。

下载在内存中8路2MiB Range获取媒体，只写音轨M4A；不落地全账号MP4。
目录 fileSize 不一定等于选定清晰度实际字节数，以验证后的 Content-Range 为准。
CDN HTTPS地址及重定向均检查域名。完整解码与时长校验通过后才发布音频检查点。
单个文件失败不删 partial；完成文件按账号/回放身份和hash复用。不宣称 partial M4A字节续传。
探测、下载共用两次预算和退避；共享网络异常或认证/限流错误停止队列，恢复需明确依据。

队列最多提前安排两个音轨，只有一个 ASR。离线本地模型每900秒分块、2秒重叠，按词中点
归属去除边界重复，保留真实重复发言。正文包含全音频处理结果、播放时间戳、原题与来源。
`complete=true` 表示全部音频块已处理，不代表机器每个字都正确。

## 完成门槛

- 每场目录身份唯一；严格无缺页，不用相同标题代替回放ID。
- 音轨完整解码，时长与来源一致；偏差需要检查源容器时长，不擅自截断或放宽。
- ASR分块覆盖完整且无缺块，Word正文逐段等于转写正文。
- 渲染 Word 并逐页检查，更新对应 `.verification.json` 的 render_review_complete，
  保持 docx_sha256 匹配。不要并发修改运行worker独占的 batch/status.json。
- 按目录对账每场结果，失败清单为空才叫全量完成。可先交付成功的Word，但标明部分。
- 最终说明机器转写未经人工逐字校听。用户不要中间文件时，列精确路径按删除规则处理，
  不自动递归删除任务、原素材或验证证据。
