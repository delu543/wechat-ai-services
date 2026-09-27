# Privacy

This project is local-first. Its scope is one user-supplied authorized replay, video,
course, webpage, or direct-media link at a time.

## Data the workflow may use locally

- The supplied link and provider metadata needed to locate its media.
- For a Weixin link on macOS, process, accessibility, WindowServer, and power-assertion
  metadata needed to prove the official WeChat target and playback state.
- For a Weixin link on Windows, only after explicit user confirmation, newly changed
  files in bounded playback/runtime roots.
- Encrypted media bytes, a same-context decode value, resumable download checksums,
  the decrypted MP4 working file, and the final MP3.

These items stay on the current computer and inside that OS account/profile namespace.
Signed media query strings and decode values must not be printed in normal reports or
committed.

## Data the workflow does not read

- WeChat chat or contact databases.
- Unrelated conversations or message history.
- Browser cookies, Login Data, Web Data, Keychain/Credential Manager secrets. The
  YouTube, X/Twitter, and generic webpage routes do not import them automatically.
- Arbitrary account tokens or passwords.
- Global network traffic.

## Storage

Final MP3 on both systems:

```text
<current account Downloads>/WeixinReplayMP3/<opaque-namespace>/
```

Private runtime/data:

- macOS: `~/Library/Application Support/WeixinReplayToMP3/`
- Windows: `%LOCALAPPDATA%\WeixinReplayToMP3\`

The namespace is a one-way short hash of the local OS principal and an optional
validated profile label. Raw usernames and profile labels are not directory names.
Different computers and different OS accounts have no shared backend or automatic data
exchange. A direct checkout uses the same user-local roots instead of writing private
run data into the Git repository.

macOS applies private POSIX modes to managed directories. Windows uses the current
account's LocalAppData and inherited NTFS account permissions; the project does not
misrepresent POSIX `chmod` as a Windows ACL.

Multiple humans sharing one OS login also share its WeChat desktop session. An optional
`--profile` separates tool files inside that account, but true identity and credential
isolation requires separate OS accounts.

The project never auto-deletes these files. Legacy files remain in place and are not
automatically imported into a new namespace. Cleanup or migration requires a separate,
itemized user decision.

## Model boundary

Conversion does not require uploading media to an AI model. Codex sees commands,
sanitized diagnostics, and final paths. A later request to transcribe or summarize an
MP3 is a separate data-processing decision governed by the user's Codex account and
data settings.
