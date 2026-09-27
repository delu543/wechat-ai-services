# Windows explicit setup route

This replaces the Mac temporary-copy/Frida/Keychain/quit steps only. The shared
entry gate still requires the user to explicitly invoke `$wechat-local-export-setup`.
Ordinary export, a missing profile, or possession of encrypted files is not consent.

Scope: Windows 10/11 x64, official WeChat 4.x, CPython 3.12 x64, source preview.
This is not a signed consumer Companion, and no live Windows WeChat acceptance is
claimed merely because native fixture tests pass.

For first-use guidance, read `docs/WINDOWS_FIRST_RUN.md` completely from the
complete source kit or verified installed source release resolved through the
sibling Windows reference. Use its Windows-specific disclosures and concise
Chinese prompts, not the shared Skill's Mac temporary-copy/Frida/quit disclosures.
The examples are not consent. Reuse already-provided content types and the
original export request; never default to all or request this invocation again
if it is already explicit in the current task. If ordinary doctor proves this
account ready for those types, skip capture/retention and return to that request.
Always separate environment installation, explicit setup entry, capture consent,
retained-snapshot consent and actual export completion in user-facing status.
Explain before retention that category-level snapshots may include other chats
in the current account; chat/time filtering applies to final exports. Keep the
two actual-scope confirmations separate even when the user wants fewer steps.

## 1. Read-only diagnosis

Use the installed private Python runtime and source release determined by the
sibling export Skill's `references/windows.md`. Read that reference completely.
Run the normal client doctor, then (only within this explicit setup Skill):

```text
<private-python> -X utf8 -m live_tools.windows.setup doctor
```

Set working directory to the verified installed source root. Commands have no
account/path/key argument. Doctor binds the current account using exact open-file
evidence from the same Windows user's signed official WeChat 4.x process, twice.
No historical account directory list, mtime/size guess, screenshot, UI control or
automatic privilege escalation is permitted. If binding fails, ask the user to
log in to the intended official account, open any chat and retry once; otherwise stop.

Run bootstrap only for missing ordinary dependencies after explaining downloads.
Windows setup uses the same pinned native packages; it does not install Frida or
download/inject a third-party credential extractor.

## 2. Prepare exact content scope

If the user has not named content types, ask what to export. Do not default to all.
Map Chinese types consistently with the ordinary Skill (文字 → text,system).

```text
<private-python> -X utf8 -m live_tools.windows.setup prepare --types <explicit-comma-separated-types>
```

This writes only a private prerequisite/consent record, no keys or chat data; it
does not modify WeChat. Present the returned types, target aliases and count in
plain language, never internal account identifiers, file paths, salts or digests.
Every applicable discovered message/media shard is included; missing required
classes stop. The preparation is valid for 15 minutes and the same process,
account, app version, database identities, salts and target set.

## 3. Separate current-account key-capture consent

Explain and obtain a fresh explicit confirmation for this prepared scope:

- Read only the currently bound same-user WeChat process for one initialization.
- No process writes, injection, program modification, memory dump, new login,
  forced exit or screenshot. Keep official WeChat open; user controls login.
- The helper validates each target's salt and SQLCipher first-page HMAC/structure.
  It stops if incomplete/unsupported (90 seconds, 1 GiB read, 512 candidate cap).
- Matching initialization material is encrypted with current-user Windows DPAPI,
  stored under private ACLs for this account, and never displayed or uploaded.
- Capture permission is not permission to create/retain plaintext snapshots.

Only after that confirmation:

```text
<private-python> -X utf8 -m live_tools.windows.setup capture --confirm-capture
```

Do not read the DPAPI file, print candidate values, or manually extract credentials.
If the prepared identity changes or expires, rerun prepare and get new confirmation.
Access denied is not authorization to elevate or weaken Windows protections.

## 4. Separate retained-snapshot consent

After capture succeeds, explain that a private decrypted snapshot for the same
initialized categories will remain in the application's current-user support
storage, separate from downloadable exports. This consent also permits normal
on-demand refreshes for those categories. Keys remain protected until separately
authorized removal/reinitialization; no historical export or snapshot is deleted.

Only after explicit creation-and-retention confirmation:

```text
<private-python> -X utf8 -m live_tools.windows.setup snapshot --confirm-retain
```

The helper coordinates Windows SQLite locks, copies DB/WAL, replays only committed
frames, validates the private plaintext snapshot, and writes the account's non-key
profile atomically. Failed snapshot creation must not replace an existing ready
profile. No automatic recovery to uncoordinated copy or stale data is allowed.

## 5. Verify and hand off

Rerun the ordinary client doctor. Report current-account readiness, requested type
support and remaining restrictions. The whole snapshot still reports
`page_hmac_verified: false`; first-page verification alone is not full authentication.
Leave official WeChat open. Future exports use the normal Skill and the same exact
chat/time/type scan-and-confirm contract, without asking users for database paths.

Different accounts, new database shards or expanded content types require a fresh
scoped explicit setup. Never reuse another account's state or silently broaden scope.
