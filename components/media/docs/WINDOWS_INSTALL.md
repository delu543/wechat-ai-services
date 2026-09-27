# Windows 安装与救援（v0.5.0）

## 结论

Windows x64 新用户的主入口不再是 Git clone、`bootstrap-windows.ps1`、winget 或在线
pip，而是一个固定的便携 Release Asset：

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `weixin-replay-to-mp3-windows-portable-v0.5.0.zip` | 87,413,851 | `95d8cace6bc257a4556bcb1aabe5632a5c71739397d19fbf72f20c4e109c638f` |

下载地址：

<https://github.com/delu543/weixin-replay-to-mp3/releases/download/v0.5.0/weixin-replay-to-mp3-windows-portable-v0.5.0.zip>

机器可读清单见 [WINDOWS_OFFLINE_RELEASE.json](WINDOWS_OFFLINE_RELEASE.json)。ZIP 已包含：

- 官方 CPython 3.13.15 Windows x64 embeddable runtime；
- `imageio-ffmpeg==0.6.0` Windows x64 wheel 和其中的 FFmpeg；
- `yt-dlp==2026.8.19`；
- `yt-dlp-ejs==0.8.0`；
- `deno==2.9.5` Windows x64 wheel；
- 项目运行源码、Codex Skill、逐文件清单和 `install-offline.ps1`。

所以新电脑不需要预先安装 Python、FFmpeg、Git、winget 或 pip。只要这个 ZIP 的字节已经
到达本机，后续安装不会再发出网络请求。

## 新 Codex 窗口

把 [WINDOWS_FIRST_PROMPT.md](WINDOWS_FIRST_PROMPT.md) 中的完整内容作为第一条消息。它
内置了 6,916 字节的 ASCII 启动胶囊，SHA-256 为：

```text
9fc42b7ff46644e2f0ab10e375336042c37342b5d9614afe28fef00591d4e723
```

这个胶囊来自消息本身，所以不存在“为了下载安装器，必须先下载安装器”的循环。它会：

1. 优先复用 Codex 提供的 bundled Python 下载固定 ZIP；
2. 再尝试 `curl.exe`、PowerShell 和 BITS；
3. 每个候选都核对固定字节数和 SHA-256；
4. 展开 ZIP 并运行里面的离线安装器；
5. 安装后用便携 Python做 preflight。

只有返回 `READY: you can send a link now` 且 preflight 的 `ready`、
`web_link_ready`、`ffmpeg_ready`、`yt_dlp_ready` 和 `javascript_runtime_ready` 都为真，
才能告诉用户“已经可以发送链接”。

## 终端 TLS 失败时

PowerShell、Git 或 raw GitHub 失败，不代表产品依赖缺失。启动胶囊会记录每种客户端的实际
错误并停止无上限重试。

如果 Codex 内置浏览器能打开 GitHub，则使用浏览器打开：

<https://github.com/delu543/weixin-replay-to-mp3/releases/tag/v0.5.0>

只下载文件名恰好为 `weixin-replay-to-mp3-windows-portable-v0.5.0.zip` 的资产。下载完成后：

1. 在当前用户 Downloads 中找到精确文件；
2. 核对上表的字节数和 SHA-256；
3. 重新运行首条消息已经写入的 `bootstrap-windows-portable.ps1`，通过
   `-LocalArtifact "<精确 ZIP 路径>"` 交给它；
4. 不让用户手工复制源码或逐个安装依赖。

如果所有终端客户端和内置浏览器都不能传输任何文件字节，那么阻塞点是该电脑的网络/下载
策略，而不是 Python、FFmpeg 或产品脚本。不得关闭证书校验、安装 MITM 证书或修改系统
代理。换网络或由已授权渠道传递这一个固定 ZIP 后，可以完全离线继续。

## 已经取得 ZIP 时的直接命令

Codex 应自动执行，用户不需要自己操作：

```powershell
$asset = "C:\Users\<user>\Downloads\weixin-replay-to-mp3-windows-portable-v0.5.0.zip"
$expected = "95d8cace6bc257a4556bcb1aabe5632a5c71739397d19fbf72f20c4e109c638f"
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $asset).Hash.ToLowerInvariant()
if ((Get-Item -LiteralPath $asset).Length -ne 87413851 -or $actual -ne $expected) {
    throw "Portable asset verification failed"
}
$expanded = Join-Path $env:TEMP ("weixin-portable-" + [Guid]::NewGuid().ToString("N"))
Expand-Archive -LiteralPath $asset -DestinationPath $expanded
$installer = Get-ChildItem -LiteralPath $expanded -Filter install-offline.ps1 -Recurse |
    Select-Object -ExpandProperty FullName -First 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
```

安装器只写当前账户：

```text
%LOCALAPPDATA%\WeixinReplayToMP3\runtime\
%USERPROFILE%\.codex\skills\weixin-replay-to-mp3\
%USERPROFILE%\Downloads\WeixinReplayMP3\<隔离命名空间>\
```

升级旧的受管运行时时，旧目录会先移动到可恢复备份目录；同名但没有本工具管理标记的目录会
被拒绝覆盖。安装器不读取微信聊天/联系人数据库、浏览器 Cookie 或账号 Token。

## 安装后的命令

不依赖系统 `python`，始终使用便携启动器：

```powershell
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" preflight
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" `
  run "<本人有权处理的链接>"
```

视频号只有在命令明确返回 `Manual playback is required` 时，才进入 README 中的手动微信
播放门禁；非视频号链接不得操作微信。

## 兼容入口

已经有完整 checkout 的开发者仍可运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\bootstrap-windows.ps1
```

该旧入口会取得内嵌源码安装器，并可能使用在线 pip/winget；它用于兼容现有环境，不是新
用户的一步安装主路径。对“第一条消息安装、第二条消息发链接”的产品验收必须使用上面的
固定便携 ZIP。

## 真正的完成标准

- `install-offline.ps1` 退出码为 0；
- JSON 中 `status` 为 `ready`，`install_mode` 为 `offline_portable`；
- 安装路径中的便携 Python 可运行；
- FFmpeg、yt-dlp、EJS 和 Deno 都由 preflight 证明可用；
- 最终出现 `READY: you can send a link now`；
- Windows CI 在隐藏 Git、系统 Python、winget 并禁用在线 pip 的条件下完成同一安装，并
  把一个本地音频样本转换成完整可解码 MP3。
