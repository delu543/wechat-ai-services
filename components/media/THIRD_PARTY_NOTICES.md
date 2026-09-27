# Third-Party Notices

No proxy, certificate, driver, captured user data, or opaque downloader is bundled in
this repository. The separately published Windows x64 portable Release Asset contains
the fixed, hash-verified open-source runtimes listed below so first installation can
finish without an online package manager.

## Python on first-time Windows setup

The Windows portable Release Asset contains the official CPython 3.13.15 x64
embeddable package, fixed by filename and SHA-256. It installs only under the current
user's LocalAppData and does not call winget. Python is distributed under the PSF
License, whose license file remains inside the Python package. See
<https://www.python.org/downloads/release/python-31315/>.

## imageio-ffmpeg

The macOS installer downloads `imageio-ffmpeg==0.6.0` from PyPI using pinned SHA-256
wheel hashes. The Windows portable Release Asset includes the fixed x64 wheel.
The package is distributed under the BSD 2-Clause License. Its bundled ffmpeg
executable remains subject to the license configuration of that binary. See
<https://github.com/imageio/imageio-ffmpeg>.

## FFmpeg

FFmpeg is invoked as an external executable for media conversion and full-decode
verification. The source repository does not contain an FFmpeg binary; the Windows
portable Release Asset carries the binary already distributed inside the pinned
`imageio-ffmpeg` wheel. See <https://ffmpeg.org/legal.html>.

On Windows, the explicit recording fallback can address an existing DirectShow audio
input. The repository does not bundle, install, or enable a loopback/virtual-audio
driver.

## yt-dlp and EJS

The macOS installer downloads the pure-Python `yt-dlp==2026.8.19` and
`yt-dlp-ejs==0.8.0` wheels from PyPI using pinned SHA-256 hashes. The Windows portable
Release Asset carries those same fixed wheel bytes. They are used for public YouTube,
X/Twitter, and generic webpage extraction without automatic browser-cookie import.
yt-dlp is distributed under the Unlicense; the EJS wheel contains Unlicense, MIT, and
ISC components. See
<https://github.com/yt-dlp/yt-dlp> and <https://github.com/yt-dlp/ejs>.

## Deno

The macOS installer downloads the platform-specific `deno==2.9.5` PyPI wheel using a
pinned SHA-256 hash. The Windows portable Release Asset includes the corresponding
fixed x64 wheel. Its executable is used only as yt-dlp's restricted JavaScript runtime.
Deno is MIT licensed. See <https://deno.com/>.

## ISAAC / ISAAC64

The numeric-key compatibility implementation uses the ISAAC64 algorithm. Bob Jenkins'
reference ISAAC implementation was placed in the public domain and may be used for
private, educational, or commercial purposes. See
<https://burtleburtle.net/bob/rand/isaacafa.html>.

The repository does not copy or bundle the `ltaoo/wx_channels_download` application,
its proxy/certificate components, or its Commons-Clause-licensed distribution. That
project was reviewed only as an architecture/compatibility reference.

## Optional Weixin WASM assets

The string `decode_key` compatibility route may download two files at runtime from
`Evil0ctal/WeChat-Channels-Video-File-Decryption`:

- `wasm_video_decode.js`
- `wasm_video_decode.wasm`

That upstream project is MIT licensed, Copyright (c) 2025 Evil0ctal. The assets are not
committed here; they are fetched only if that optional route is used and remain in the
local private work directory. See
<https://github.com/Evil0ctal/WeChat-Channels-Video-File-Decryption>.

## Platform names

WeChat and Weixin are trademarks of their respective owners. This project is not
affiliated with or endorsed by Tencent, WeChat, OpenAI, or the projects listed above.
