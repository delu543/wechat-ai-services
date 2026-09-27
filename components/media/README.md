# 直播 / 视频链接转 MP3（Codex 工具）

把本人有权访问的视频号、小红书、YouTube、X/Twitter、课程或直接媒体链接交给 Codex，
自动识别平台、下载并验证完整媒体，最后导出 MP3。其他公开网页媒体也会走通用提取器
尝试；需要登录、订阅、地区许可或 DRM 的内容不会绕过限制。

这是一个“把 Codex 对话当作产品界面”的本地工具，不生成文字稿，也不要求用户学习
命令行。项目没有共享服务器：链接处理、断点续传、转码、校验和运行状态都保存在当前
电脑、当前系统账户的隔离目录中，不会写回 GitHub，也不会与其他用户共用。

- macOS 与 Windows：小红书、YouTube、X/Twitter、直接媒体和可支持的普通网页使用同一
  套直接提取、转码和校验流程，不需要操作微信。
- 只有视频号需要微信入口：macOS 可走受保护的 `文件传输助手` 自动链路；Windows 没有
  可靠微信界面适配时，由用户完成一次明确的发送、打开和播放。

使用界面就是一个 Codex 对话窗口，不需要另外学习命令行。

## 第一次使用（macOS / Windows）

准备好以下两项即可：

- Codex 桌面版；Windows 用户可参考 [OpenAI 官方 Windows 说明](https://learn.chatgpt.com/docs/windows/windows-app)；
- 如果要处理视频号，再准备已登录的官方桌面微信。

Windows x64 新电脑不需要预先安装 Python、FFmpeg、Git、winget 或 pip。`v0.5.0` 的
固定便携包一次性带齐 Python 3.13.15、Windows FFmpeg、yt-dlp、EJS、Deno、项目运行时
和 Codex Skill。Codex 只需取得并核对这一个 Release Asset，后续展开、安装与 preflight
都不再联网，也不需要从系统或 PyPI 临时找依赖。
换句话说，不需要预先安装 Python、FFmpeg 或 Git，也不需要用户手工补环境。

第一次使用时，把 [Windows 第一次对话](docs/WINDOWS_FIRST_PROMPT.md) 中的完整消息原样
发给新的 Windows Codex 窗口。消息本身带有极小启动胶囊，因此没有本地 checkout 也能
开始；它会优先自动下载固定便携包，终端 TLS 受限而内置浏览器可用时则通过浏览器下载
同一个文件。固定文件名、大小和 SHA-256 见
[Windows 离线发布清单](docs/WINDOWS_OFFLINE_RELEASE.json)。

Codex 只有在安装器返回 `READY: you can send a link now`，且便携 Python 的 preflight
证明 FFmpeg、yt-dlp 和 Deno 全部可用后，才会回复“已经可以发送链接”。下一条消息即可
直接发送本人有权处理的视频链接。当前版本明确支持原生 Windows；如果检查结果说 README
只支持 macOS，说明读取了旧版本或错误来源，应刷新 GitHub `main` 后重试。

## 日常使用：只发链接

日常只需要发送任意一个目标链接，例如：

```text
https://weixin.qq.com/sph/<视频短链接 ID>
https://www.xiaohongshu.com/.../live_replay/...
https://www.youtube.com/watch?v=...
https://x.com/<用户>/status/<ID>
```

非视频号链接在 macOS 和 Windows 上都直接进入同一套平台/网页提取器，不打开微信，也
不会读取浏览器 Cookie 或账号 token。只有 Windows 上的视频号命令明确提示需要手动
播放时，用户才需要：

1. 打开官方 Windows 微信中标题恰好为 `文件传输助手` 的聊天；
2. 发送本次链接，打开最新的同一链接并开始播放；
3. 回到 Codex 回复：`已在文件传输助手打开这个链接，并开始播放`。

Codex 收到这句确认后会继续抓取、下载、转码和完整校验。Windows 当前不会自动点击或
发送微信消息，因此不会盲目操作其他群聊；如果当前微信版本的安全运行目录不兼容，
Codex 会说明边界，并改用用户有权保存的本地媒体文件转换，不会伪报成功。

Codex 会返回最终文件路径、大小和时长。默认文件位于当前系统账户的 Downloads 目录：

```text
Downloads/WeixinReplayMP3/<本地隔离命名空间>/weixin_<短链接 ID>.mp3
Downloads/WeixinReplayMP3/<本地隔离命名空间>/youtube_<视频 ID>.mp3
Downloads/WeixinReplayMP3/<本地隔离命名空间>/x_<状态 ID>.mp3
Downloads/WeixinReplayMP3/<本地隔离命名空间>/xiaohongshu_<回放 ID>.mp3
```

同一平台目标再次发送时会先完整解码验证现有 MP3；通过就直接复用，不会重复下载或
二次转码。中途断开时，也只会继续当前用户命名空间内、与目标链接绑定的状态。

## macOS 与 Windows 的差异

| 系统 | 非视频号链接 | 视频号入口 |
| --- | --- | --- |
| macOS | 小红书、YouTube、X/Twitter、直接媒体、Songy 和通用网页走共享提取器 | 自动验证 `文件传输助手`、发送/复用链接、打开并证明播放 |
| Windows | 与 macOS 使用同一套平台识别、网页提取、转码和完整解码 | 先试目标绑定来源；需要微信时由用户手动发送、打开并确认播放 |
| Linux/云端 | 本产品暂不作为支持平台 | 不支持本机微信入口 |

两端运行时都需要：

- Python 3.10 或更高版本；
- 足够空间保存原视频工作文件和最终 MP3。

Windows 便携包已经包含所需 Python 和媒体依赖；用户不必先手工安装。Codex 应在
Windows 原生 PowerShell 环境执行本项目，而不是把 WSL/Linux 的
`unsupported_platform` 结果当成 Windows 结果。

只有视频号需要已登录的官方桌面微信；只有 macOS 自动微信界面链路需要 Swift Command
Line Tools 和辅助功能权限。

Windows 当前不会盲点微信界面，也不会自动向任何聊天发送消息。如果命令返回
`Manual playback is required`，Codex 必须告诉用户并等待完成以下步骤：

1. 打开官方 Windows 微信。
2. 打开标题恰好为 `文件传输助手` 的聊天。
3. 把本次链接发进去，打开最新的同一链接并开始播放；不要同时播放其他微信视频。
4. 回到 Codex 明确回复“已在文件传输助手打开这个链接，并开始播放”。

确认前不会扫描近期运行文件。确认后，工具只检查已知的安全播放/运行目录，再继续同一
下载和转码核心。如果 Windows 微信版本把播放运行文件放在其他位置，可以显式设置
`WEIXIN_REPLAY_RUNTIME_ROOTS`，多个目录用分号分隔；不得把它指向聊天或联系人数据库。

## 用户数据隔离

项目没有共享服务器，也不会把运行数据写回 GitHub。默认隔离键由当前操作系统账户、
本机 Home 目录和可选本地 profile 共同生成；目录名只包含短哈希，不直接包含用户名或
profile 名。

macOS：

```text
~/Library/Application Support/WeixinReplayToMP3/data/profiles/<隔离命名空间>/
~/Downloads/WeixinReplayMP3/<隔离命名空间>/
```

Windows：

```text
%LOCALAPPDATA%\WeixinReplayToMP3\data\profiles\<隔离命名空间>\
%USERPROFILE%\Downloads\WeixinReplayMP3\<隔离命名空间>\
```

- 不同电脑、不同系统账户默认不会共享状态、分片、解密材料或 MP3；
- 直接从 GitHub 检出的源码运行，也不会把私有数据写进仓库的 `work/`；
- macOS 私有目录使用 `0700` 和 `077` umask；Windows 默认写入当前账户的
  LocalAppData 并继承该账户的 NTFS 权限；
- profile 名会先验证再哈希，不能用 `../` 或绝对路径逃逸；
- 旧输出和旧 `runtime/work/` 会原样保留，新版不会自动跨命名空间读取。

同一个登录账户需要分开本地工作区时，可以增加 `--profile <本地英文代号>`。这只隔离
工具文件；同一个系统账户仍共享微信登录和桌面会话。不同人需要真实安全隔离时，应使用
不同的 macOS/Windows 账户。

## 第一次安装会做什么

Windows x64 的主安装物是一个固定 Release Asset：

```text
weixin-replay-to-mp3-windows-portable-v0.5.0.zip
```

首窗消息内的启动胶囊会用有界超时依次尝试 Codex bundled Python、PowerShell、
`curl.exe` 和 BITS 下载这个文件；每种客户端都必须得到发布清单中的固定字节数和
SHA-256。终端 TLS 失败但内置浏览器可以下载时，Codex 改为用浏览器取得同一资产，再做
同一校验。它不再尝试从网页抄写 1.5MB 脚本，也不会让用户手动寻找依赖。

macOS 仍使用：

```bash
python3 scripts/bootstrap.py doctor
python3 scripts/bootstrap.py install
```

只有用户明确要求安装或使用后才运行安装。便携包内的 `install-offline.ps1` 会：

1. 校验包清单和 Python、源码、四个固定 wheel 的逐文件哈希；
2. 在当前账户的 LocalAppData 中展开自带 Python 和运行源码；
3. 离线展开 `imageio-ffmpeg`、`yt-dlp`、`yt-dlp-ejs` 和 Deno；
4. 安装 `weixin-replay-to-mp3` Codex Skill；
5. 用刚安装的便携 Python 运行 preflight，并做最终依赖可用性校验。

安装器不调用 Git、winget 或在线 pip，不安装证书、不修改代理、不退出微信，也不读取聊天
数据。升级既有受管运行时时会先把旧运行时移动到当前账户的可恢复备份目录；非本工具管理
的同名目录会被拒绝覆盖。旧的 `bootstrap-windows.ps1`/`install-windows.ps1` 继续作为
已有 checkout 的兼容路径；它可能使用 GitHub API/codeload，但不再是新用户的主路径。详细说明见
[Windows 安装与救援](docs/WINDOWS_INSTALL.md)。

## 为什么这通常不是录一个小时

正常路径不是实时录音。小红书、YouTube、X/Twitter 和普通网页先直接获取可用媒体；
视频号在 macOS 自动入口或 Windows 手动播放接力之后进入自己的目标绑定流程。两类路线
最终都直接下载/转换源媒体并完整解码校验。

视频号路线会：

1. 把本次播放与精确短链接绑定；
2. 只冻结播放前后变化的安全运行文件；
3. 从同一上下文配对媒体地址和解密参数；
4. 先解密一小段并要求得到标准 MP4 头；
5. 按服务器声明的完整字节数并发、断点续传下载；
6. 转成 MP3，再用 ffmpeg 从头到尾完整解码。

因此主要耗时通常是网页解析、源文件下载和转码，不受视频时长一比一限制。网络/CDN
限速仍会影响速度。系统音频录制只是明确的最后备选，不是正常 Windows 流程。

## 为什么不会发错群

非视频号链接完全不操作微信，因此没有误发群聊的路径。

macOS 自动发送前必须同时证明：

- 左侧名称是 `文件传输助手`；
- 右侧聊天标题是 `文件传输助手`；
- 左侧是文件传输助手的绿色图标；
- 最新消息状态能够验证；
- 点击后复制回来的完整链接与用户提供的链接一致。

任何一项不确定，都会在输入或发送前停止。白色/受保护截图不会被当成“微信退出”；
流程会改用外层微信进程、`WeChatAppEx`、辅助功能和 WindowServer 元数据，但不会解除
或破坏微信的窗口保护。

Windows 版本目前不自动发送，所以脚本不会误发到群聊。聊天选择和发送由用户完成；
Codex 必须说清楚“文件传输助手、最新同一链接、已经播放”三项并等待确认，不能把
“微信已打开”当成“目标已播放”。

## 手动命令（开发和排障）

macOS 安装目录：

```bash
python3 "$HOME/Library/Application Support/WeixinReplayToMP3/runtime/weixin_replay_cli.py" preflight
python3 "$HOME/Library/Application Support/WeixinReplayToMP3/runtime/weixin_replay_cli.py" \
  run "<本人有权处理的链接>"
```

Windows PowerShell 安装目录：

```powershell
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" preflight
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" `
  run "<本人有权处理的链接>"
```

Windows 命令明确要求手动播放时，先完成上文步骤并回复确认，再运行：

```powershell
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" `
  run "https://weixin.qq.com/sph/<id>" --manual-playback
```

如果用户已经有权保存并拿到本地 MP4/M4A 等文件，两端都可以跳过网页或微信入口直接转换：

```text
python <运行目录>/weixin_replay_cli.py convert-file "<本地媒体文件>"
```

完整解码验证：

```text
python <运行目录>/weixin_replay_cli.py verify "<MP3 文件>"
```

如果用户明确知道最低时长，可增加 `--min-duration <秒>`。默认不假设视频一定是 60
分钟；视频号完整性主要由精确链接、新增量、同上下文参数、MP4 头、完整字节数、候选
大小和最终完整解码共同证明；其他平台也必须得到完整的下载/转换结果并通过最终完整解码。

## 隐私和安全

- 只处理用户本次提供的链接；
- 不读取微信聊天/联系人数据库；
- 不自动读取浏览器 Cookie、Keychain/Credential Manager 或账号 token；
- 不安装抓包证书，不修改系统代理，不运行第三方不透明下载器；
- 原始签名地址和解密参数只保存在当前用户/profile 的本机私有工作目录，不进入 Git
  或普通报告，也不会被另一个隔离命名空间自动读取；
- `work/`、输出媒体、截图、缓存和任务状态全部被发布检查排除。

详细边界见 [PRIVACY.md](PRIVACY.md)、[SECURITY.md](SECURITY.md) 和
[能力矩阵](docs/CAPABILITY_MAP.md)。

## 回归和发布检查

```text
python scripts/release_check.py
```

检查包括：代码/私密数据允许清单、绝对开发路径、凭据模式、大文件/媒体、Codex Skill、
自包含安装器可复现性和不触碰真实微信的离线回归。Windows CI 会构建固定便携包，把
Git、系统 Python 和 winget 从 `PATH` 移除并阻断在线 pip，然后只靠该 ZIP 安装、执行
preflight，并把本地音频完整转成可解码 MP3；macOS 继续跑同一核心测试。这会验证分发、
跨平台路由与依赖，但不等同于每个外部网站或 Windows 微信真机都已验证，相关边界会
继续明确标注。

## 许可证与声明

本仓库代码采用 MIT License。外部组件和算法说明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。项目与腾讯、微信、OpenAI 均无官方
隶属关系；用户必须只处理自己有权访问和转换的内容，并遵守当地法律及平台条款。
