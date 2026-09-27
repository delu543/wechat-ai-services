# Troubleshooting

## Windows 上出现 `unsupported_platform` 或“只支持 macOS”

Current `main` supports native macOS and Windows. `unsupported_platform` is expected
for Linux/remote local-WeChat operation, not for a native Windows agent. A Windows
report that says README is macOS-only came from stale/wrong source or from running the
agent inside WSL/Linux.

1. In the ChatGPT/Codex Windows settings, use the Windows-native agent for this tool.
2. Use the literal capsule in [Windows first prompt](WINDOWS_FIRST_PROMPT.md); it is
   already in the user's first message and does not need a checkout.
3. Acquire the single fixed `weixin-replay-to-mp3-windows-portable-v0.5.0.zip`, verify
   its published size and SHA-256, and run the included `install-offline.ps1`.

The asset already contains a bundled Python 3.13.15, FFmpeg, yt-dlp, EJS, Deno,
source, and the Codex Skill. Therefore missing Python, FFmpeg, Git, winget, or pip is not an
installation blocker. Once the ZIP is local, installation and preflight make no
network request.

The capsule bounds each terminal client attempt. If terminal TLS fails but the in-app
browser can download a GitHub Release Asset, download the same fixed ZIP through that
browser, verify the same hash, and continue. Do not transcribe a large PowerShell file
or search for individual packages. If neither terminal clients nor the browser can
transfer any file bytes, that network acquisition boundary is the only correct blocker;
it is not evidence that Windows is unsupported. Do not label it `unsupported_platform`
or weaken certificate checks. Exact checksums
are in [Windows installation and rescue](WINDOWS_INSTALL.md).

Full Xiaohongshu/YouTube/X/Twitter/generic webpage support requires Python 3.10 or
newer. An older Python runtime is not full product readiness.

## YouTube, X/Twitter, or another webpage fails

On Windows, run the installed `weixin-replay-to-mp3.cmd preflight`; on macOS, run
`python3 scripts/bootstrap.py doctor`. Check `web_link_ready`, `yt_dlp_ready`, and
`javascript_runtime_ready`. The Windows portable runtime supplies its own Python,
FFmpeg, yt-dlp, local EJS assets, and Deno.

The route does not automatically read browser cookies. A link can still fail because
it requires login, age confirmation, a subscription, region access, DRM, or because
the site changed. Use a user-authorized local media file/artifact when available; do
not weaken cookie, credential, proxy, or protection boundaries.

Non-Weixin links never require `文件传输助手`. If Codex asks for WeChat while processing
YouTube, Xiaohongshu, X/Twitter, Songy, a direct-media URL, or a generic webpage, stop:
the platform route was selected incorrectly.

## Windows reports `Manual playback is required`

This is the expected safe branch only for a Weixin Channels link when no target-bound
local/provider source is already available. The repository has no verified Windows
WeChat UI adapter and will not guess coordinates or send to an unverified chat.

1. Open official Windows WeChat.
2. Select the chat titled exactly `文件传输助手`.
3. Send the exact supplied link, open the newest matching message, and start playback.
4. Stop unrelated WeChat video playback and tell Codex the exact target is playing.
5. Codex may then rerun the same command with `--manual-playback`.

Do not add that flag before confirmation. The flag authorizes only the bounded recent
playback/runtime scan; it does not authorize blind UI operations.

## Windows manual playback finds no safe runtime increment

WeChat runtime layouts can differ by version and install location. The tool checks known
`xwechat/radium` and `RadiumWMPF` locations under AppData. If a read-only inspection
shows that this installation uses another playback/runtime directory, set PowerShell:

```powershell
$env:WEIXIN_REPLAY_RUNTIME_ROOTS = "C:\safe\playback-root;D:\another\runtime-root"
```

Point only to playback/runtime folders. Never use chat, contact, message, cookie,
history, or account database roots. If no compatible runtime source exists, use
`convert-file` with a user-authorized local MP4/M4A file.

## Windows last-resort audio recording

This is not the normal path. First list explicit inputs:

```powershell
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" audio-devices
```

Windows exposes FFmpeg DirectShow inputs. A usable loopback may be named `Stereo Mix`
or may come from software the user already installed. The project never installs or
enables an audio driver. The user must select an exact device, confirm the exact link is
playing, provide the real playback speed and bounded wall-clock duration, and accept
that this route is slower than source download.

## macOS screenshot is white or WeChat is missing from capture

Do not relaunch WeChat repeatedly and do not attempt to turn off protection. The runtime
already treats this as a branch:

1. Confirm outer `com.tencent.xinWeChat` and `WeChatAppEx`.
2. Raise the window through Accessibility.
3. Read AX window metadata.
4. Fall back to WindowServer title and geometry without protected pixels.
5. Continue only if the File Transfer Assistant target gates still pass.

If the target cannot be proven, the correct result is a safe stop before sending.

## macOS selected another chat or reports target mismatch

Do not retry by blindly clicking a row. Keep WeChat open, make `文件传输助手` visible in
the pinned list, and rerun. The workflow verifies the left name, right header, green
icon signature, and newest-message state together. A mismatch is not bypassable.

## The link opened but playback proof did not appear

On macOS, a player window is not enough. The automatic route performs bounded
metadata-guided activation and requires both `Playing audio` and `Video Wake Lock`.
Check VPN/network state and retry the same link; a verified exact message is reused.

On Windows, confirmation comes from the user because the repository does not claim
automatic UI proof. Confirm only after the exact newest link visibly starts.

## It found a small MP4 but the replay should be longer

The candidate must come from the target-bound increment and pass MP4-prefix decryption.
The converter orders verified candidates by declared source bytes and checks final
download length. If the user knows a reliable floor, add `--min-duration <seconds>`.
Do not hard-code one hour for all links.

## Download is slow or interrupted

The downloader uses bounded parallel ranges with per-span checksums. Rerun the same
short link and output path; it resumes only missing or invalid spans. Do not delete the
target-bound work state or rename a partial download during recovery.

## Existing MP3 is rejected

The file is preserved. The CLI refuses to overwrite an existing output that fails full
decode or the requested minimum duration. Inspect it, choose an explicit new `--output`,
or move it yourself after deciding it is not needed.

## `ffmpeg not found` or web tools are not ready

On Windows, rerun `install-offline.ps1` from the already verified portable ZIP; do not
ask the user to install FFmpeg manually. On macOS, run
`python3 scripts/bootstrap.py install`. The Windows route revalidates and restores its
embedded Python, FFmpeg, yt-dlp, EJS, and Deno without online pip. A system FFmpeg can
instead be supplied through the `FFMPEG` environment variable using its exact
executable path, but the pinned web tools are still required for full multi-platform
webpage support.

## Codex does not recognize the Skill immediately

The same repository task can run `weixin_replay_cli.py` directly after installation.
For new Codex tasks, restart Codex once so it reloads user-level Skills.

## One computer is used by more than one person

Use separate operating-system accounts for a real security boundary because the
WeChat login and desktop UI belong to that session. One person may use a validated
`--profile <name>` for separate local tool workspaces, but profiles do not authenticate
different people.

Do not point `WEIXIN_REPLAY_DATA_ROOT`, `WEIXIN_REPLAY_OUTPUT_ROOT`,
`REPLAY_MP3_LIBRARY`, `REPLAY_MP3_WORK_ROOT`, or an explicit `--output` at a shared
directory unless sharing is an intentional user decision.
