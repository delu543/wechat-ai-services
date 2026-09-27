# Video Audio Extractor

本工具用于把你本机能正常观看的视频号、小红书直播/直播回放等内容，尽可能稳定地输出为 MP3，方便后续转文字。

它的定位不是破解下载器。它只做三件事：

1. 被动观察网页播放过程里是否出现标准媒体流。
2. 审计本机缓存/临时目录里是否出现可识别媒体文件或片段。
3. 前两者不稳定时，由用户显式启动黑箱录制兜底，再恢复正常语速并转 MP3。

## 合规边界

- 只处理你能在本机正常打开和观看的链接。
- 不做 App 逆向。
- 不绕过登录、鉴权、签名、加密、DRM、设备指纹。
- 不读取、导出、上传账号密码、Cookie、Token 或隐私数据。
- 所有报告和输出都在本机。
- 网络探测只做被动观察。默认只在报告里写脱敏 URL，不保存带签名的原始 URL。
- 黑箱录制不会静默启动，必须由用户主动执行命令并选择音频输入设备。

## 安装

建议在本目录执行：

```bash
cd /path/to/weixin-replay-to-mp3/video-audio-extractor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

安装 ffmpeg 和 ffprobe：

```bash
brew install ffmpeg
```

如果 ffmpeg 不在 PATH，可以指定：

```bash
export FFMPEG=/absolute/path/to/ffmpeg
export FFPROBE=/absolute/path/to/ffprobe
```

当前工作区已有旧流程使用的 `imageio_ffmpeg`，本工具也会尝试自动寻找该 ffmpeg；但完整的 `ffprobe` 仍建议通过 Homebrew 安装。

## 模式选择

优先顺序：

1. `probe-url`：普通网页能用浏览器打开时，先看网络侧有没有标准媒体 URL。
2. `audit-cache`：播放发生在微信内置浏览器、浏览器不暴露请求时，审计本地缓存/临时目录。
3. `convert-file` / `convert-url`：对已经证明有音频流的候选文件或 URL 转 MP3。
4. `blackbox-record`：前面都不稳定时，用户显式启动录制兜底。

## 缓存审计

命令：

```bash
python -m src.main audit-cache \
  --dirs "目录1" "目录2" \
  --duration 120 \
  --out reports/audit_001
```

示例：观察微信 PC 的播放侧目录和临时目录：

```bash
python -m src.main audit-cache \
  --dirs "$HOME/Library/Containers/com.tencent.xinWeChat/Data/tmp" \
         "$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium" \
         "$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/net/cdncomm" \
  --duration 120 \
  --out reports/weixin_cache_001
```

输出：

- `reports/weixin_cache_001.json`：完整结构化报告。
- `reports/weixin_cache_001.csv`：变化文件清单。
- `reports/weixin_cache_001.md`：结论报告。

审计器启动前会建立快照，记录：

- path
- size
- mtime
- ctime
- inode/device
- sha256，默认只对 50MB 以下文件计算
- 前 64 到 256 bytes 文件头
- 文件头分类
- ffprobe/ffmpeg 探测结果

运行期间每 1 秒扫描一次，记录新增、删除、大小变化、mtime/ctime 变化。

## 如何判断第一次抓到的文件是否有价值

不要只看“文件变大”。

以 Markdown 报告里的 `First Changed File` 为准，逐项看：

1. 路径是不是播放侧缓存，而不是数据库、日志、LocalStorage、图片缓存。
2. 文件头是否是 `mp4`、`m4a`、`fmp4`、`aac`、`mp3`、`mpeg-ts`、`webm`。
3. ffprobe 是否能识别容器。
4. 是否包含 audio stream。
5. duration 是否接近视频/直播回放长度，还是只有几秒片段。

只有 ffprobe 识别且包含音频流的文件，才可以直接作为 `convert-file` 的输入。

如果文件被识别为：

- `sqlite`
- `leveldb/ldb/log`
- `jpg/png/webp`
- `gzip/brotli`
- `unknown`

它更可能是缓存索引、播放器状态、日志、缩略图或压缩 Web payload，不能直接说明抓到了音频。

## 排查第二次为什么抓不到

建议按 A/B/C/D 做实验。

### 实验 A：全新环境

1. 使用新的浏览器 profile，或清理测试缓存目录。
2. 打开链接。
3. 播放 60 到 120 秒。
4. 运行 `audit-cache`。
5. 判断是否出现 ffprobe 可识别的音频/视频文件。

### 实验 B：重复播放

1. 不清缓存。
2. 关闭页面后重新打开同一链接。
3. 再播放 60 到 120 秒。
4. 对比 A/B 两次报告。

如果 A 有变化、B 没有变化，常见原因是已有缓存命中或播放链路转入内存缓存。

### 实验 C：改变播放行为

在审计器运行期间依次执行：

1. 拖动进度条到未播放位置。
2. 切换清晰度，如果页面支持。
3. 暂停后继续播放。
4. 调整播放倍速，如果页面支持。

看报告里这些动作是否触发新文件、匿名临时文件或可识别媒体片段。

### 实验 D：网络请求侧观察

普通网页播放可以跑：

```bash
python -m src.main probe-url \
  --url "视频号或小红书链接" \
  --duration 120 \
  --out reports/probe_001
```

如果需要保留登录状态，可以指定独立 profile：

```bash
python -m src.main probe-url \
  --url "链接" \
  --profile-dir profiles/test_profile \
  --duration 120 \
  --out reports/probe_001
```

这个模式会被动观察：

- `.m3u8`
- `.mpd`
- `.m4s`
- `.ts`
- `.aac`
- `.m4a`
- `.mp4`
- `.flv`
- `.webm`

并对候选 URL 运行 ffprobe/ffmpeg fallback。默认报告里只写脱敏 URL。

如果要把探测到的第一个带音频流 URL 直接转 MP3：

```bash
python -m src.main probe-url \
  --url "链接" \
  --duration 120 \
  --out reports/probe_001 \
  --convert-out outputs/from_network.mp3
```

## 候选文件转 MP3

```bash
python -m src.main convert-file \
  --input "candidate_file" \
  --out outputs/output.mp3
```

如果输入是 3 倍速录制音频，需要恢复正常语速：

```bash
python -m src.main convert-file \
  --input "fast_recording.wav" \
  --recorded-speed 3 \
  --out outputs/output_normal_speed.mp3
```

内部等价于：

```bash
ffmpeg -i fast_recording.wav \
  -filter:a "atempo=0.5,atempo=0.6666667" \
  -c:a libmp3lame -b:a 128k output_normal_speed.mp3
```

## 候选 URL 转 MP3

```bash
python -m src.main convert-url \
  --url "candidate_media_url" \
  --out outputs/output.mp3
```

注意：URL 如果包含签名或短期授权，只在本机使用，不要贴到公开文档或聊天里。

## 黑箱录制兜底

适用场景：

- 网络侧看不到标准媒体 URL。
- 缓存侧只有索引、日志、状态文件、匿名临时 FD。
- 播放发生在微信客户端或 WebView 层，浏览器脚本看不到真实媒体流。

黑箱方案需要本机有合法音频捕获方式，例如 BlackHole、Loopback，或你手动把系统输出路由到可被 ffmpeg 录到的输入设备。
本项目当前也支持 `system` 设备，使用 macOS ScreenCaptureKit 录制系统音频；上层 Studio 会在设备列表里显示它是否可用。

先列出音频设备：

```bash
python -m src.main audio-devices
```

或者在本地网站里选择 `黑箱录制`，展开 `诊断/黑箱参数`，点击 `刷新音频设备`。
如果没有传 `--audio-device`，工具会停止，不会静默录制。

### 尝试超过播放器 UI 的倍速

如果页面本身只是把倍速菜单限制在 3x，但底层仍是标准 HTML `video/audio` 元素，可以在黑箱录制前尝试页面加速片段：

1. 打开本地 Studio。
2. 进入 `黑箱录制`，把 `黑箱倍速` 填成目标值，例如 `6`、`8` 或 `12`。
3. 展开 `诊断/黑箱参数`，点击 `生成页面加速片段`。
4. 在授权播放页上下文执行生成的书签脚本或页面脚本。
5. 观察进度条和声音，确认播放器确实以目标倍速运行。
6. 再用同一个倍速启动黑箱录制。

限制：

- 该片段只会设置当前页面里标准 `HTMLMediaElement.playbackRate`。
- 如果微信内置浏览器禁止 `javascript:`、播放器把速度锁死，或播放发生在原生/私有播放器层，这个方法不会生效。
- 不能只在录制命令里填 `8x` 就认为已经 8x 播放；必须先确认页面实际加速，否则输出恢复速度会不正确。

选择设备后运行：

```bash
python -m src.main blackbox-record \
  --url "链接" \
  --speed 3 \
  --audio-device "system" \
  --duration 1200 \
  --out outputs/output_normal_speed.mp3
```

`system` 会使用 macOS ScreenCaptureKit 尝试录制系统音频；首次使用可能需要系统权限。
`:0`、`:1` 这类值会使用 avfoundation 输入设备，例如麦克风或外接/虚拟音频设备。

可选 smoke test：

```bash
python3 ../scripts/blackbox_system_audio_smoke.py
```

这个 smoke 只播放本地生成的 2 秒正弦波，并验证 `system` 录音链路能否产出可被 ffprobe/ffmpeg 识别的 MP3。它不是默认 health check，因为它会访问系统音频录制权限。

流程：

1. 打开链接。
2. 你正常登录并确认可以播放。
3. 页面支持倍速时，你手动切到 3 倍速。
4. 工具录制所选音频输入。
5. 录完后用 `atempo=0.5,atempo=0.6666667` 拉回正常语速。
6. 输出 MP3 和 `.blackbox.json` 日志。

## 结论表模板

最终报告会用这个表格表达结论：

| 方法 | 是否成功 | 证据 | 输出文件 | 风险 | 建议 |
| --- | --- | --- | --- | --- | --- |
| 网络媒体流 | 成功/失败 | ffprobe 结果 | path | 低/中/高 | 优先/备用/放弃 |
| 缓存文件 | 成功/失败 | 文件头和 ffprobe | path | 低/中/高 | 优先/备用/放弃 |
| 黑箱录制 | 成功/失败 | 录制日志和输出 MP3 | path | 中 | 兜底 |

## 已知限制

- 微信内置浏览器里的播放可能由 WebView/Renderer 持有匿名临时 FD，路径已经删除，普通目录扫描无法直接复制。
- 有些平台使用私有分片、加密分片或内存缓存，ffprobe 无法识别。
- `probe-url` 只能观察它自己打开的浏览器，不会观察微信客户端内置浏览器。
- 黑箱录制的音质和稳定性取决于本机音频路由、播放稳定性和倍速支持。
