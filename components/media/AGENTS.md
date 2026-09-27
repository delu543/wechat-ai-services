# Codex project instructions

This repository is the independent macOS and Windows link-to-MP3 project whose
historical repository name is `weixin-replay-to-mp3`. Keep link classification,
download, resume, conversion, verification, and storage isolation platform-neutral.
Do not duplicate or rewrite the media pipeline for one operating system.

## First relevant request

When the user asks to install/use this project or supplies an authorized replay,
video, live, course, or direct-media link for MP3 extraction:

1. Detect the native operating system without assuming Python, FFmpeg, Git, winget, or
   an online package source is already usable.
2. On 64-bit Windows, do **not** stop at a missing prerequisite. The primary first-use
   artifact is the fixed `weixin-replay-to-mp3-windows-portable-v0.5.0.zip` Release
   Asset. It contains Python 3.13.15, the Windows FFmpeg wheel, yt-dlp, EJS, Deno, the
   product runtime, the Codex Skill, and `install-offline.ps1`. Verify the asset's fixed
   byte count and SHA-256 from `docs/WINDOWS_OFFLINE_RELEASE.json`, expand it, and run
   `install-offline.ps1`. The install phase must make no network request and must not
   call Git, winget, or pip. `bootstrap-windows.ps1` and `install-windows.ps1` remain
   supported online/checkout fallbacks, not the primary new-user route.
3. On macOS, run `python3 scripts/bootstrap.py doctor`, read `state` and `preflight`,
   and run `python3 scripts/bootstrap.py install` when the explicit request asks to
   install/use the product and the state is `needs_install`.
4. Do not execute or install anything merely because the repository was opened. An
   install/use request is the authorization for the bounded user-local installation
   above; no separate “please install Python” round trip is needed.
5. After readiness passes for an installation/check request, tell the user exactly
   `已经可以发送链接`.
6. For a link-extraction request, follow the repository's `weixin-replay-to-mp3`
   Skill after readiness passes.

Python 3.10 or newer is required for current cross-platform webpage extraction. The
Windows portable package supplies its own fixed compatible interpreter and must use it
for subsequent commands; do not depend on a system `python` command.

### Windows first-install command

Use the literal bootstrap capsule in `docs/WINDOWS_FIRST_PROMPT.md`. The capsule is
part of the user's first message, so it does not require a checkout or a prior script
download. It fetches one fixed portable ZIP through bounded Python, PowerShell, curl,
or BITS clients, verifies its byte count and SHA-256, expands it, and invokes:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-offline.ps1
```

If the capsule's terminal clients cannot retrieve the asset but the in-app browser can
open GitHub, use that browser to download the **same fixed Release Asset** to the
current user's Downloads directory. Do not transcribe a 1.5 MB installer or ask the
user to copy repository files. Verify the downloaded ZIP against the fixed release
metadata, then expand and run its offline installer. The checkout fallback remains:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\bootstrap-windows.ps1
```

For compatibility only, that older checkout bootstrap may query `api.github.com` and
`codeload.github.com`; the portable first-use route above does not depend on either API.

Do not repeat failed URLs indefinitely. The only unavoidable network boundary is
transferring the single fixed portable ZIP. Once its bytes are local, installation and
preflight are fully offline. If both terminal clients and the signed-in browser cannot
transfer any file bytes, report that acquisition boundary precisely; do not mislabel
it as missing dependencies or try to weaken TLS verification.

`unsupported_platform` is valid for Linux/remote local-WeChat operation, not for a
native Windows agent. If a Windows result says the README is macOS-only, it is stale or
from the wrong source. Refresh `main` rather than repeating that conclusion. If the
Codex Windows app is configured to run the agent in WSL, switch the agent environment
to Windows native for this product before installation.

## Platform routing

The same `run "<USER_SUPPLIED_LINK>"` command accepts and classifies:

- Weixin Channels `weixin.qq.com/sph/...`;
- Xiaohongshu live replay and share links;
- YouTube/youtu.be;
- X/Twitter;
- direct media URLs, Songy links, and other webpages supported by the pinned generic
  extractor.

For every non-Weixin link, use the shared direct/provider route on macOS and Windows.
Do not open, inspect, send to, or click WeChat for those links. Never import browser
cookies or account tokens automatically; a restricted link must fail clearly or use a
user-authorized local file/artifact.

### macOS

Run the shared command:

```bash
python3 "$HOME/Library/Application Support/WeixinReplayToMP3/runtime/weixin_replay_cli.py" \
  run "<USER_SUPPLIED_LINK>"
```

Only a Weixin link may enter the guarded automatic File Transfer Assistant workflow.

### Windows

Use the same provider, download, resume, conversion, and verification pipeline. The
installed PowerShell command is:

```powershell
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" `
  run "<USER_SUPPLIED_LINK>"
```

For Xiaohongshu, YouTube, X/Twitter, direct-media, Songy, and generic webpage routes,
no WeChat step is allowed. For a Weixin link, the command first tries target-bound
local/provider routes. The public release does not claim a verified Windows WeChat UI
adapter and must never send or click blindly. If and only if a Weixin run reports
`Manual playback is required`, tell the user exactly:

1. Open official desktop WeChat.
2. Open the conversation whose title is exactly `文件传输助手`.
3. Send the exact supplied link there, open the newest matching message, and start the
   video. Stop unrelated WeChat video playback.
4. Reply to Codex with `已在文件传输助手打开这个链接，并开始播放` only after that exact
   video is visibly playing.

Wait for that explicit confirmation. Then, and only then, run:

```powershell
& "$env:LOCALAPPDATA\WeixinReplayToMP3\runtime\weixin-replay-to-mp3.cmd" `
  run "<USER_SUPPLIED_WEIXIN_LINK>" --manual-playback
```

Do not reinterpret `--manual-playback` as permission to operate another chat. It only
authorizes the bounded recent-runtime scan after the user has confirmed the exact
playback. If the Windows WeChat runtime is stored outside the known safe locations,
inspect only playback/runtime directories and set `WEIXIN_REPLAY_RUNTIME_ROOTS` to a
semicolon-separated list. Never point it at chat/contact databases.

If runtime capture is unavailable, offer conversion of a user-authorized local media
file with `convert-file`. Audio-device recording is an explicit last resort; list
devices first, require the user to choose one and confirm playback, and never install
or enable a driver automatically.

Linux and remote/cloud environments are not supported for local WeChat operation.

## Storage

Default data and output roots are outside the repository and bound to an opaque
namespace for the current operating-system account. Provider outputs use stable names
such as `weixin_<id>.mp3`, `xiaohongshu_<id>.mp3`, `youtube_<id>.mp3`, and
`x_<id>.mp3`. If the user explicitly selects a local `--profile`, keep using that same
profile for every resume/verify step.

- macOS runtime/data: `~/Library/Application Support/WeixinReplayToMP3/`
- Windows runtime/data: `%LOCALAPPDATA%\WeixinReplayToMP3\`
- MP3 output on both systems: the current account's
  `Downloads/WeixinReplayMP3/<opaque-namespace>/`

## Non-negotiable safety gates

- On macOS, send only the exact supplied link and only after the conversation is
  proven to be exactly `文件传输助手` by the implemented name, header, icon, and
  latest-message gates. Any ambiguity stops before input or Return.
- On Windows, this release does not send messages or click WeChat automatically.
  Require the explicit manual confirmation above before any recent-runtime scan.
- Non-Weixin routes must not touch WeChat on either operating system.
- Webpage extraction must not automatically read browser Cookie stores, tokens,
  passwords, or account databases. Public extraction failure is not permission to
  weaken this boundary.
- Never read chat/contact databases, cookies, tokens, unrelated history, or account
  secrets. Never install a certificate, change the proxy, hook/patch WeChat, or disable
  window protection.
- A protected/white screenshot is not proof that WeChat exited. macOS uses process,
  `WeChatAppEx`, AX, WindowServer, exact-message, and playback assertion evidence.
- Do not run broad stale-cache scans when the exact target was not opened and playback
  was not independently proven.
- Keep `work/`, `.codex/`, outputs, media, reports, screenshots, signed URLs, keys, and
  account data local and untracked.
- Never read or reuse another storage namespace. A shared OS login is not a
  separate-user security boundary; recommend separate OS accounts when different
  humans share a computer.
- Preserve an existing MP3 and target-bound run state. Never overwrite invalid evidence
  or delete a prior result during a retry.

## Completion

Completion requires the final command result, an existing MP3, and a full ffmpeg
decode. Report the exact path, byte count, duration, selected source byte count when
available, provider, and whether the run reused prior verified state. Do not call a
player window, candidate, download start, or partial file successful output.

Before any public push, run `python scripts/release_check.py` on the available local
platform and keep the original development workspaces untouched. Windows CI must build
the fixed portable asset, remove Git/system Python/winget from `PATH`, block online pip,
install from only that ZIP, prove preflight, and convert a local audio fixture to a
fully decoded MP3. Real Windows WeChat playback/runtime behavior remains a separate
machine-evidence gate.
