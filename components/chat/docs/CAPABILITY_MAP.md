# Capability Map

“已实现”不等于“所有电脑均已适配”。下表把代码、当前机器真实证据和产品化状态分开。

| Capability | Status | Safety contract | Verification |
| --- | --- | --- | --- |
| Unified decrypted-vault doctor | active | Read-only frozen input; schema and dependency gates; no key handling | Fixtures plus current-machine real decrypted snapshot |
| Current official-session account binding | active development path | Exactly one account from read-only official-process evidence; no historical account list and no mtime/size/order guess; zero/multiple/unstable results fail closed | 17 router regressions cover multi-account, switch and race cases; current-machine double sample uniquely bound the open account with no writes |
| Exact chat/time content plan | active | Exact chat or explicit chat ID; absolute interval; every row retained; duplicate nonzero server IDs fail closed | Real plan: 34 messages in one interval; parser/scanner tests |
| Text and structured-message parsing | active | Low 32-bit type classification; bounded XML; signed URL/key/token sanitization; unknown fallback retained | Parser fixtures including type 19, legacy group sender and secret-redaction cases |
| Voice BLOB extraction and MP4 | active | Global unique `message.server_id == VoiceInfo.svr_id`; SILK/hash/duration/order validation; direct voice metadata accepts the observed frame-aligned range through 61,000 ms and rejects higher values | Real strict exports: 33/33 and 362/362 voices, 0 issues; 362-item MP4 independently decoded end-to-end; synthetic media regression |
| Voice MP4-only fast publication | active | Explicit voice-only plan; strict chat binding and source fingerprint; every SILK is revalidated and decoded to a bounded private PCM file, then hashed, sample-counted and streamed in order with exact gaps into one ffmpeg encode; one full output decode verifies exactly one H.264 and one AAC track; no partial mode. This fast path requires `pilk` plus pinned local ffmpeg, not the Swift M4A helper | Real two-item pipe integration plus process-count, ordering, timeout/BrokenPipe cleanup, no-Swift readiness, atomic-publication and full-archive compatibility regressions; full archive retains the per-item M4A path |
| Image recovery | active when local asset exists | Exact chat attachment tree and packed MD5; full > high > thumbnail; no mtime/proximity guessing; symlink/path escape rejected | Real V2 WXGF asset uniquely decoded and converted to JPEG; V1/V2/XOR fixtures |
| Ordinary file recovery | implemented, asset-dependent | Normalized exact basename, bounded month candidates and available MD5/size evidence; unresolved metadata is not success | Resolver/archive fixtures; no claim that every file body is cached locally |
| Sticker recovery | conditional | Exact message MD5 plus verified plaintext image magic/content; proprietary opaque cache stays unsupported/metadata-only | Plain-cache fixtures; one inspected proprietary encrypted cache remains unresolved |
| Video messages | metadata only by policy | Message is retained as `excluded_by_policy`; video body is never exported | Parser/archive fixtures |
| Unknown/unsupported messages | active metadata fallback | Bounded, sanitized raw preview; never silently dropped | Parser fixtures |
| Per-account one-time database-key initializer | active development path; target expansion consent pending | Current session is uniquely bound first; explicit exact targets and fresh per-account consent only; official WeChat exits normally; private random app copy; exact-salt first-page validation; keys saved owner-only `0600`; bounded cleanup | 42 initializer regressions cover scoped state, legacy exact validation, route mismatch and capture binding; existing current-account keys were revalidated read-only, with no new capture |
| `message_resource` key/snapshot target | implemented, consent pending | Exact alias only; no wildcard; `MessageResourceInfo` expected-table gate | 27 live-tools regression tests; no new real key capture performed without separate consent |
| Coordinated online snapshot/decryption | active with integrity limitation | After exact chat resolution, selects the minimal initialized DB set; OFD-locks SQLite WAL coordination bytes, validates the locked SHM/WAL anchor, APFS-clones DB+WAL and the WAL-materialization base, replays only committed frames, decrypts at most two independent DBs concurrently, validates the decrypted vault, then atomically updates the profile; never writes live DB | Snapshot/online-refresh regressions, real SQLite writer-lock/APFS-clone probes, and one open-official-WeChat 362-voice scan; page HMAC is not verified |
| Source-development Codex Plugin/Skills | Plugin-ready | Normal current-account scan refreshes online only after one exact chat ID; ambiguous candidates and explicit development vaults do not refresh; profile must match the current official session; per-account setup is explicit-only; thin client preserves validated virtual-environment launchers and accepts only doctor/scan/export/direct-voice-mp4 with no secret arguments. The direct command performs the readiness gates, exact online scan and same-plan strict MP4 publication in one call, reports safe per-stage timing, and hands off verified media before any separate release workflow | Package validator plus portable routing, launcher, online-refresh, single-request orchestration, timing, cleanup and documentation-policy tests |
| Signed/notarized universal Companion | not shipped | Future allowlisted high-level IPC, Keychain ownership, Developer ID/notarization and signed updater | Product architecture only; must not be called product-ready |
| UI/application-audio fallback | experimental | Dry scan and approval first; restricted voice `AXPress`; no keyboard; no message sending | Unit/self-tests; requires per-machine supervised calibration |
| WeChat sending/input | absent | No typing, paste, return, send or arbitrary click capability in the normal export path | Static forbidden-symbol checks |

## Change classification

## Additive replay-to-Word module

| Capability | Status | Boundary | Evidence |
| --- | --- | --- | --- |
| Creator replay pagination | source preview | Exact author/seed binding, native page API, terminal cursor, private local catalog; explicit narrow temporary interception | Original adapter real single-account 219 unique replays / 5 pages; distribution wrapper needs fresh-machine acceptance |
| Audio-only batch and recovery | source preview | HTTPS allowlist, exact ranges, full decode, identity/hash, bounded prefetch, probe inside retry budget, shared-outage stop | Multiple real original audio validations; recovered failed replay complete at 3573.38s; synthetic outage and access-denial regressions |
| Offline ASR and Word | source preview, Apple Silicon | Local MLX, all-block coverage, word-time overlap ownership, body round-trip, separate page-review gate | Original multi-replay Word outputs; not a claim of all219 delivered or human-verbatim accuracy |
| Live local progress panel | active local prototype | Loopback/private route, GET-only, no queue control, safe aggregate/progress fields, per-file download verification | Live running queue plus status/paused/error/corruption tests and browser checks |
| Windows replay capture/ASR | not implemented | Do not inherit chat Windows tests | Requires a separate native implementation and machine validation |

Removed / Changed Existing Capabilities: none removed. Chat clients, key initialization and exports
remain separate. Replay capture is not a silent fallback for chat export.

The unified content archive and current-account online refresh are additive. The direct voice path,
explicit frozen development vault, and existing supervised UI/audio fallback remain available; no
prior export capability was removed. The historical-directory account choice UX is intentionally
replaced by fail-closed current official-session binding. Strict export is the default. Partial output
requires a separate `--allow-partial` decision and keeps every unresolved state visible in
`manifest.json`.

## Windows additive preview

| Capability | Status | Safety contract | Verification gate |
| --- | --- | --- | --- |
| Native Windows install and Codex discovery | experimental | CPython 3.12 x64, pinned wheels; no Swift/Frida; no WeChat access at install; preserve foreign skills and prior releases | Native Windows bootstrap and idempotency CI; clean end-user Windows install remains external |
| Windows current-account setup | experimental | Signed Tencent 4.x same-user process, exact open-file two-sample binding; explicit per-account capture; bounded VM_READ only; salt/first-page HMAC check; DPAPI/ACL state | Synthetic routing/key/ACL/DPAPI tests; actual WeChat build/handle/key-layout acceptance not yet recorded |
| Windows latest snapshot | experimental | Native SQLite SHM shared locks or deny-write handles for inactive shards; committed WAL replay; initialized shard census; no source writes, no stale fallback | Native SQLite writer lock + synthetic encrypted latest-commit test; large live WeChat database acceptance remains external |
| Windows shared export pipeline | experimental | Shared exact plans and strict atomic archives; native SILK/FFmpeg including per-item M4A and MP4; unchanged media availability restrictions | Native generated-audio integration + Mac regression; Windows real media cache compatibility remains external |

Removed / Changed Existing Capabilities: none removed. Mac routing, profile storage, capture,
APFS/OFD coordination, Swift and legacy command entry points remain active. Shared changes are
platform-aware privacy operations, binary-safe I/O and a Windows FFmpeg branch. Windows does not
inherit the Mac real-data verification counts above. See [Windows scope](WINDOWS_SUPPORT.md).
