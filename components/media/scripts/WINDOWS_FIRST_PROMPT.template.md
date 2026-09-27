# Windows 第一次对话

把下面整段作为 Windows Codex 新窗口的第一条消息：

```text
请把这个 Windows Codex 窗口安装成“下一条消息发送本人有权处理的视频链接即可导出
MP3”的工具：

https://github.com/delu543/weixin-replay-to-mp3

请直接完成安装和验证。使用 Windows 原生 PowerShell，不要在 WSL 中运行，也不要只列出
缺少 Python、FFmpeg、Git、winget 或 pip。

本消息已经内置 v__PORTABLE_VERSION__ 的固定启动胶囊，不再依赖旧的
`GitHub API + bundled` 安装器抄写路径。请按以下步骤执行：

1. 如果工作区依赖查询工具可用，取得它返回的 Windows bundled Python 精确路径；不要猜
   路径。没有该路径也继续，胶囊还有 PowerShell、curl 和 BITS 客户端。
2. 在当前工作区创建 `bootstrap-windows-portable.ps1`，把下面 Base64 按 ASCII 解码为该
   文件；解码结果必须是 __BOOTSTRAP_BYTES__ 字节，SHA-256 必须是
   `__BOOTSTRAP_SHA256__`。这段内容来自我的消息，不需要先访问 GitHub：

__BOOTSTRAP_BASE64__

3. 运行该脚本；如果有 bundled Python，就通过 `-PythonExecutable` 传入精确路径。脚本
   只获取一个固定的 Windows x64 便携包，包含 Python、FFmpeg、yt-dlp、EJS、Deno、源码
   和 Codex Skill，并校验固定大小与 SHA-256。不要另行搜索或安装任何依赖。
4. 如果所有终端客户端都因 TLS/网络策略失败，但 Codex 内置浏览器能打开 GitHub，请用
   内置浏览器打开 v__PORTABLE_VERSION__ Release 页面，下载文件名恰好为
   `__PORTABLE_ASSET_NAME__` 的资产到当前用户 Downloads；然后把它的精确路径通过
   `-LocalArtifact` 交给同一胶囊。不要让我手工放文件，不要抄写大型安装器。
5. 我授权在当前 Windows 用户范围内安装该便携包。不得修改代理、安装证书、读取浏览器
   Cookie/Token、读取微信聊天或联系人数据库，也不得操作其他 Windows 用户的数据。
6. 安装器退出码为 0，返回 `offline_portable`，且自带 Python 的 preflight 同时证明
   FFmpeg、yt-dlp、EJS/Deno 和 `web_link_ready` 后，才算完成。

完成后只告诉我：安装版本、preflight 结果、MP3 输出目录，并说“已经可以发送链接”；
最后一行必须原样输出 `READY: you can send a link now`。
以后我发送 YouTube、X/Twitter、小红书、视频号、直接媒体或其他支持网页链接时，目标是
导出完整校验的 MP3，不生成文字稿。

Windows 视频号只有在工具明确返回 Manual playback is required 时，才让我在标题恰好为
“文件传输助手”的聊天中手动发送、打开最新同一链接并开始播放。不得盲点或向其他聊天
发送消息；收到我的精确播放确认后再继续。
```
