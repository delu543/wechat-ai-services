# Capability Map

This map separates implemented code, real-machine evidence, and portability.

| Capability | macOS | Windows | Safety contract and evidence |
| --- | --- | --- | --- |
| Codex-first link intake | active | active | One CLI classifies Weixin, Xiaohongshu, YouTube, X/Twitter, Songy, direct media, and generic http/https webpages; rejects embedded credentials and non-web schemes |
| Xiaohongshu live-replay metadata route | active | active | Shared Python route, share-link resolution, media conversion, and full MP3 decode; no WeChat dependency |
| YouTube public extraction | active | active | Pinned yt-dlp + local EJS + Deno, no automatic browser cookies; individual site/account/network behavior remains external |
| X/Twitter and generic public webpage extraction | active | active | Shared pinned yt-dlp route, no automatic browser cookies or tokens; unsupported/restricted URLs fail visibly |
| Direct media URL conversion | active | active | Shared FFmpeg path and full-decode completion gate |
| Songy direct provider route | active with artifact fallback | active with artifact fallback | Bounded direct request only; login-restricted courses require a user-authorized artifact/local file |
| File Transfer Assistant targeting | active, guarded automatic route | manual user step | macOS verifies left name, right header, icon, and exact newest link before input; Windows never sends/clicks blindly |
| Protected-window fallback | active with limits | not used by the manual route | macOS treats white pixels as unavailable and uses process/AX/WindowServer metadata; it never disables protection |
| Exact-link playback proof | active automatic assertions | explicit user confirmation | Windows recent-runtime scan is disabled until `--manual-playback`; mocked fail-closed regressions cover the gate |
| Causal/recent runtime capture | active | implemented for known safe roots; real-machine validation pending | Exclude chat/contact/cookie/history stores and bind private run state to the short ID |
| Encrypted candidate proof | active | shared platform-neutral code | Pair URL/key from the same context; decrypt a bounded prefix and require MP4 `ftyp`; real macOS runs plus decode tests |
| Resumable source download | active | shared platform-neutral code | Bounded range workers, per-span checksums, declared final byte count; real macOS runs plus range tests |
| MP3 conversion and full verification | active | active | FFmpeg conversion followed by full decode; Windows CI exercises the same Python surface |
| Authorized local media conversion | active | active | `convert-file` preserves an existing output and requires full decode before completion |
| Same-link reuse/resume | active | active | Fixed short-ID output and target-bound private run state; pipeline-state tests |
| Per-user data isolation | active | active | Opaque namespace binds local OS principal + validated profile; macOS modes and Windows LocalAppData/NTFS account boundary |
| First-time prerequisite recovery | Python must already exist | active | One fixed Windows x64 Release Asset embeds Python 3.13.15, source, Skill, FFmpeg, yt-dlp, EJS, and Deno; the literal first-message capsule fetches it without requiring a checkout, and an in-app browser download is the bounded fallback |
| Automatic dependency install | active | active | User-requested only; Windows portable install is offline after the single asset transfer and calls no Git, winget, or online pip; macOS retains its pinned Python 3.10+ venv route |
| Explicit audio-recording fallback | ScreenCaptureKit/AVFoundation | DirectShow device selected by user | Last resort only; never install/enable a driver; implementation tests, Windows real-device validation pending |
| Windows automatic WeChat UI control | n/a | not implemented | Must not be advertised; a missing adapter stops before sending or scanning |
| Linux/local cloud WeChat control | not implemented | not implemented | Unsupported platform fails closed |
| Consumer-grade signed app | not shipped | not shipped | Source + Codex workflow, not signed/notarized GUI software |

## macOS active workflow

1. Validate the supplied link and reuse a verified target-bound MP3 if present.
2. Check authorized local source artifacts and bounded direct routes.
3. Baseline safe WeChat runtime files.
4. Open only the verified File Transfer Assistant conversation, send/reuse the exact
   link, open it, and prove playback.
5. Freeze the causal increment, prove the encrypted MP4 candidate, resume/download,
   convert to MP3, and full-decode verify.

This section is Weixin-specific. Non-Weixin links use the shared provider workflow
below and never open WeChat.

## Windows active workflow

1. Validate the same link and reuse an existing verified output/resumable state.
2. Try the same authorized local source and bounded direct routes.
3. If no source is available, stop before UI control or recent-runtime scanning and
   ask the user to open `文件传输助手`, send/open the exact newest link, and start it.
4. Only after explicit confirmation, run with `--manual-playback` and inspect the
   bounded safe Windows playback/runtime roots.
5. Use the same candidate proof, resume/download, conversion, and full-decode gates.
6. If the runtime layout is not compatible, offer an authorized local media file;
   explicit audio-device recording remains the last resort.

This section is Weixin-specific. Non-Weixin links do not require the manual playback
gate.

## Shared non-Weixin workflow

1. Validate one user-supplied http/https URL and classify the provider without reading
   browser credentials.
2. Use the Xiaohongshu metadata route, Songy direct route, direct-media converter, or
   the pinned yt-dlp/EJS/Deno webpage extractor.
3. Write only to the current local namespace, preserve an existing output, and never
   operate WeChat.
4. Require a completed provider conversion and full FFmpeg decode before reporting an
   MP3. Login-, region-, subscription-, age-, DRM-, or site-change failures remain
   explicit rather than triggering credential or protection bypasses.

All steps use one local storage namespace. Another namespace cannot reuse its output or
checkpoint. There is no shared service or Git-backed runtime state.

## Evidence boundary

- macOS automatic File Transfer Assistant and causal capture have current-machine real
  run evidence.
- Windows path selection, portable installer, safe-stop/manual routing, DirectShow
  command construction, and shared media core have offline tests and Windows CI
  coverage.
- Windows CI builds the exact portable ZIP from fixed, hash-verified archives. It then
  removes Git, system Python, and winget from `PATH`, points HTTP clients at a closed
  local endpoint, disables pip indexes, and installs from only that ZIP. The installed
  portable Python must prove FFmpeg, yt-dlp, EJS, and Deno readiness and convert a local
  audio fixture into a fully decoded MP3 before the artifact is published.
- macOS and Windows CI install the same pinned yt-dlp/EJS/Deno surface and offline
  tests prove that YouTube, X/Twitter, Xiaohongshu, Songy, and generic URLs select the
  same non-WeChat routes. CI does not guarantee a particular external URL remains
  available or publicly extractable.
- This release has not yet completed a real Windows WeChat playback/cache extraction.
  Until that happens, Windows UI remains manual and Windows runtime capture is marked
  real-machine validation pending rather than “fully proven.”

## Intentionally excluded

- No certificate proxy, system proxy change, app hook, package modification,
  chat/contact database read, opaque downloader binary, or protection-disabling
  mechanism is bundled.
- A small number of provider, Studio, and black-box compatibility modules are retained
  for regression coverage; they do not broaden the one-link authorization scope.
