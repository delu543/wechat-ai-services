# Codex project instructions

This repository is “微信数据提取项目”: existing macOS support plus Windows 10/11 x64 + WeChat 4.x source preview.

## Replay transcription routing

For video-channel live replays, whole-creator replay catalogs, complete Word transcripts or
their progress dashboard, first read `portable_skill/skills/wechat-replay-word/SKILL.md`.
That additive route does not use chat databases or chat initialization. Its scoped, explicitly
approved temporary catalog interception is separate from all chat-export code below.
Do not install chat dependencies, run chat doctor or request database-key setup for replay tasks.
Live catalog capture and MLX transcription currently have Apple Silicon macOS evidence only;
do not imply Windows replay support from the Windows chat-export preview.

## First relevant request

When the user asks to install, check, use, scan, summarize, or export WeChat data:

For Windows first-use or onboarding help, read [Windows first-run guidance](docs/WINDOWS_FIRST_RUN.md)
completely before choosing the next user action. It defines the copyable first prompt, staged
Chinese replies, login timing and separate consent prompts. These examples are not actual consent.
When the user asks to revise these docs or code, do not run WeChat setup or real-data checks.

On native Windows, use `scripts/codex_bootstrap.ps1 doctor` first. If installation is needed,
explain pinned public wheel downloads and then use `scripts/codex_bootstrap.ps1 install`.
Missing CPython 3.12 x64 or VC++ runtime requires clear installation guidance and approval.
Do not run Mac scripts, Swift, Frida, WSL or change security policies to substitute for native support.
Read the selected Skill's `references/windows.md` completely before its platform actions.
Windows native CI uses synthetic data, not a live WeChat account; keep this limitation visible.
This route does not authorize key initialization, snapshot retention, or real-data testing.

Keep the Windows first-run conversation short and state-based:

- Installation can finish before WeChat login. Distinguish environment-ready from account-ready
  and export-complete. Ask for login/open-any-chat only when account diagnosis requires it.
- Run commands and resolve fixed private paths internally; do not make users copy terminal
  commands, database paths, account IDs or digests. Explain ordinary dependency downloads and
  reuse an existing matching installation authorization; extra Python/VC++ installation still
  needs clear approval. Never ask for blanket permissions or weakened Windows security.
- If the same current account is already ready for the requested types, skip setup. Otherwise
  require the user's explicit setup invocation, then separate current-scope capture and retained
  snapshot confirmations. Explain that internal snapshots can cover other chats in that account;
  only final exports are filtered by the specified chat/time. Never default content types to all.
- Retain the original export request and valid same-scope decisions across steps. Ask only for
  missing inputs, resume that request after setup, and do not turn examples into authorization.
- Do not ask Windows users to quit/relaunch WeChat, create a Mac app copy, grant Mac-only
  permissions or restart Codex routinely. User-controlled login stays manual; restart Codex
  only if newly installed Skills are not discovered. Keep all existing failure/strict-export gates.

On macOS, preserve the existing flow:

1. Run `./scripts/codex_bootstrap.sh doctor`.
2. If the result is `needs_install`, explain that the installer downloads pinned public Python
   packages into a private user directory and builds the local Swift helper, then run
   `./scripts/codex_bootstrap.sh install`.
3. Do not install or initialize key-capture dependencies during bootstrap.
4. After bootstrap, use the `wechat-local-export` Skill for normal natural-language requests.

Opening the repository must never execute code by itself. Installation begins only after a user
asks Codex to install or use this project.

## Account initialization boundary

- Normal export may be invoked from natural language.
- First-time database-key initialization is explicit-only. Never infer consent.
- If the current account is not initialized, ask the user to invoke
  `$wechat-local-export-setup`.
- Dependency installation, one-time key capture, and retention of a private decrypted snapshot are
  separate decisions.

## Safety boundaries

- Existing validated scope is local macOS with official Mac WeChat 4.x. Windows is a source preview; do not claim Windows live acceptance without actual evidence.
- Never upload chat content, database files, keys, profiles, or exported media.
- Never commit files from `work/`, `outputs/`, `.codex/`, `.build/`, task directories, application
  support directories, or WeChat containers.
- Never send a WeChat message, type into WeChat, paste, or press Return.
- Use exact chat and absolute time boundaries. Ambiguity must stop before refresh or export.
- Keep WeChat open for the normal online snapshot path. Quitting is recovery-only.
- Run `./scripts/release_check.sh` before any public push.

## Natural-language example

```text
把“示例讨论群”2026 年 7 月 24 日 14:00 到 15:30 的所有语音直接合成 MP4。
```

Codex should perform readiness checks, resolve the exact chat and time range, and use the
high-level `direct-voice-mp4` workflow. Do not ask the user to pass database paths, plan digests, or
internal account identifiers.
