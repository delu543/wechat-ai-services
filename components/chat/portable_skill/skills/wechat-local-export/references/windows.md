# Windows ordinary export route

Applies only to Windows 10/11 x64 + official WeChat 4.x source preview. Follow the
shared Skill's exact-chat/time/type, dry-scan, confirmation, strict verification,
no-upload, no-key-handling and no-WeChat-UI rules. This reference replaces only
Mac filesystem, runtime, Swift and APFS-specific steps.

For first-use conversation, read `docs/WINDOWS_FIRST_RUN.md` completely from the
complete source repository, or from the verified installed source release once
its root is resolved below. That guide supplies concise Chinese next-action and
consent wording; it is included in the full source install, not fetched remotely.
Installation may precede WeChat login. Ask the user to log in and open any chat
only when needed for account checks; already-ready accounts skip setup. Preserve
the original chat/time/type request through onboarding and ask only for missing
inputs. Guide examples are never user consent. Do not request screenshots, paths,
routine restarts, blanket permission or Mac-specific setup actions on Windows.

1. From the complete source repository, run `scripts/codex_bootstrap.ps1 doctor`.
   Installation is an explicit action: explain pinned PyPI wheel downloads before
   `install`. Missing CPython 3.12 x64 / VC++ runtime needs a separate clear remedy;
   never disable security software, change execution policy, or auto-elevate.
2. Derive the support root with Windows Known Folder APIs (LocalApplicationData),
   plus `WeChatAIServicesChat`. Never print its resolved private path. The runtime is
   `tools/python/Scripts/python.exe` beneath it. Use Python's `-X utf8` option.
3. The normal client remains `scripts/wechat_local_export_client.py` relative to
   this Skill. It discovers the managed source release through the fixed
   `app/current.json` marker. For internal module calls, use that exact installed
   release root as the working directory; the marker contains a validated 64-hex
   release directory name, not an arbitrary path. Never search other projects.
4. Run the ordinary client `doctor`. If the current account needs initialization,
   stop and request explicit `$wechat-local-export-setup`. The normal Skill must
   not import/call Windows key capture or setup modules.
5. Requests go in a fresh directory beneath `support_root()/tasks`; default outputs
   beneath `support_root()/exports`. Use `live_tools.platform_support.private_mkdir`
   and `private_chmod` before writing request bytes, not POSIX chmod emulation.
   Their ACL allows only the current Windows user and SYSTEM. Existing unsafe ACLs,
   network paths, junctions and symlinks are blockers. The JSON request and client
   commands are otherwise identical to the shared Skill. Retain names/time/count
   only as authorized; no raw message bodies in shell arguments.
6. Keep official WeChat open. The adapter refreshes all initialized message shards
   to include possible chat movement, with a 30-second maximum copy lock window.
   New shards require explicit expansion setup. No screenshot, clicking, forced
   exit or timestamp-based account guessing is allowed.
7. Windows uses pysilk-mod and pinned local FFmpeg, not Swift. Trust doctor flags:
   complete archives retain individual M4A; voice-only direct MP4 uses the same
   strict one-request flow and requires no extra confirmation when its conditions
   are satisfied. Missing media cannot silently become a partial archive.
8. Return only verified local output links and counts. State preview status when
   relevant: Windows native fixture CI is not official-WeChat live validation.
   Never claim every WeChat 4.x build, all images, remote history, or missing media
   is supported. Database page HMAC is not verified across the full snapshot.

All conversation, account isolation and bounded-cleanup rules in the shared Skill
remain in force. Only explicit per-task temporary files may be removed; never
delete initialization state, installed releases, snapshots, or exported archives.
