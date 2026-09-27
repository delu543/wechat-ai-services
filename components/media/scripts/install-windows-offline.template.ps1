[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$PackageRoot = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$PortableVersion = "__PORTABLE_VERSION__"
$ExpectedManifestSha256 = "__PACKAGE_MANIFEST_SHA256__"
$ExpectedManifestBytes = __PACKAGE_MANIFEST_BYTES__
$ManagedMarker = ".managed-by-weixin-replay-to-mp3"

function Test-WindowsHost {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Resolve-PackageFile {
    param([string]$Root, [string]$RelativePath)
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or
        $RelativePath.StartsWith("/") -or $RelativePath.StartsWith("\") -or
        $RelativePath -match '(^|[\\/])\.\.([\\/]|$)' -or $RelativePath -match '^[A-Za-z]:') {
        throw "The package manifest contains an unsafe path: $RelativePath"
    }
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $Root ($RelativePath -replace '/', '\')))
    if (-not $candidate.StartsWith($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The package path escapes the verified root: $RelativePath"
    }
    return $candidate
}

function Read-VerifiedManifest {
    param([string]$Root)
    $manifestPath = Join-Path $Root "package-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "The portable package manifest is missing."
    }
    if ((Get-Item -LiteralPath $manifestPath).Length -ne $ExpectedManifestBytes -or
        (Get-FileSha256 -Path $manifestPath) -ne $ExpectedManifestSha256) {
        throw "The portable package manifest failed its fixed length or SHA-256 check."
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format -ne 1 -or $manifest.product -ne "weixin-replay-to-mp3" -or
        $manifest.version -ne $PortableVersion -or $manifest.architecture -ne "x64") {
        throw "The portable package manifest does not match this installer."
    }
    $seen = @{}
    foreach ($entry in @($manifest.files)) {
        $path = Resolve-PackageFile -Root $Root -RelativePath ([string]$entry.path)
        if ($seen.ContainsKey($path)) { throw "The package manifest repeats a file path." }
        $seen[$path] = $true
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "A fixed portable package file is missing: $($entry.path)"
        }
        if ((Get-Item -LiteralPath $path).Length -ne [long]$entry.bytes -or
            (Get-FileSha256 -Path $path) -ne [string]$entry.sha256) {
            throw "A fixed portable package file failed verification: $($entry.path)"
        }
    }
    return $manifest
}

function Test-ManagedTarget {
    param([string]$Target)
    if (-not (Test-Path -LiteralPath $Target -PathType Container)) { return }
    if (-not (Test-Path -LiteralPath (Join-Path $Target $ManagedMarker) -PathType Leaf)) {
        throw "Refusing to replace an unmanaged directory: $Target"
    }
}

function Activate-ManagedTree {
    param([string]$Staged, [string]$Target, [string]$BackupRoot, [string]$Label)
    Test-ManagedTarget -Target $Target
    $backup = ""
    if (Test-Path -LiteralPath $Target -PathType Container) {
        New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
        $backup = Join-Path $BackupRoot (
            "{0}-{1}-{2}" -f $Label, (Get-Date -Format "yyyyMMdd-HHmmss"), [Guid]::NewGuid().ToString("N")
        )
        [System.IO.Directory]::Move($Target, $backup)
    }
    try {
        [System.IO.Directory]::Move($Staged, $Target)
    }
    catch {
        if ($backup -and -not (Test-Path -LiteralPath $Target) -and (Test-Path -LiteralPath $backup)) {
            [System.IO.Directory]::Move($backup, $Target)
        }
        throw
    }
    return $backup
}

function Invoke-Preflight {
    param([string]$RuntimeRoot)
    $python = Join-Path $RuntimeRoot "work\venv\Scripts\python.exe"
    $cli = Join-Path $RuntimeRoot "weixin_replay_cli.py"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $cli -PathType Leaf)) {
        throw "The staged portable Python or product CLI is missing."
    }
    $output = & $python $cli preflight
    if ($LASTEXITCODE -ne 0) { throw "Portable preflight exited with code $LASTEXITCODE." }
    try { $payload = ($output -join [Environment]::NewLine) | ConvertFrom-Json }
    catch { throw "Portable preflight did not return valid JSON." }
    if (-not $payload.ready -or -not $payload.web_link_ready -or
        -not $payload.ffmpeg_ready -or -not $payload.yt_dlp_ready -or
        -not $payload.javascript_runtime_ready) {
        throw "Portable preflight did not prove all fixed media dependencies ready."
    }
    return $payload
}

try {
    if (-not (Test-WindowsHost)) { throw "install-offline.ps1 must run on native Windows." }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "This fixed portable package requires 64-bit Windows."
    }
    if ([string]::IsNullOrWhiteSpace($PackageRoot)) { $PackageRoot = $PSScriptRoot }
    $PackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
    $manifest = Read-VerifiedManifest -Root $PackageRoot
    if ($CheckOnly) {
        [PSCustomObject]@{
            status = "checked"
            version = $PortableVersion
            platform = "Windows"
            architecture = "x64"
            python_embedded = $true
            ffmpeg_embedded = $true
            yt_dlp_embedded = $true
            ejs_embedded = $true
            deno_embedded = $true
            git_required = $false
            winget_required = $false
            online_pip_required = $false
        } | ConvertTo-Json -Depth 4
        exit 0
    }

    $localAppData = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    }
    $appRoot = Join-Path $localAppData "WeixinReplayToMP3"
    $runtimeTarget = Join-Path $appRoot "runtime"
    $skillTarget = Join-Path $HOME ".codex\skills\weixin-replay-to-mp3"
    Test-ManagedTarget -Target $runtimeTarget
    Test-ManagedTarget -Target $skillTarget

    $installId = [Guid]::NewGuid().ToString("N")
    $stageRoot = Join-Path $appRoot ("install-staging\" + $installId)
    $expandedSource = Join-Path $stageRoot "source"
    $runtimeStage = Join-Path $appRoot ("runtime-stage-" + $installId)
    $skillParent = Split-Path -Parent $skillTarget
    $skillStage = Join-Path $skillParent (".weixin-replay-to-mp3-stage-" + $installId)
    New-Item -ItemType Directory -Path $expandedSource, $skillParent -Force | Out-Null

    $runtimeSourceArchive = Join-Path $PackageRoot "packages\runtime-source.zip"
    Expand-Archive -LiteralPath $runtimeSourceArchive -DestinationPath $expandedSource
    $payloadRuntime = Join-Path $expandedSource "runtime"
    $payloadSkill = Join-Path $expandedSource "skill"
    if (-not (Test-Path -LiteralPath (Join-Path $payloadRuntime $ManagedMarker)) -or
        -not (Test-Path -LiteralPath (Join-Path $payloadSkill $ManagedMarker))) {
        throw "The verified runtime-source archive is missing its ownership markers."
    }
    Copy-Item -LiteralPath $payloadRuntime -Destination $runtimeStage -Recurse
    Copy-Item -LiteralPath $payloadSkill -Destination $skillStage -Recurse

    $scriptsRoot = Join-Path $runtimeStage "work\venv\Scripts"
    $sitePackages = Join-Path $runtimeStage "work\venv\Lib\site-packages"
    New-Item -ItemType Directory -Path $scriptsRoot, $sitePackages -Force | Out-Null
    $pythonArchive = Join-Path $PackageRoot ("packages\" + [string]$manifest.python.filename)
    Expand-Archive -LiteralPath $pythonArchive -DestinationPath $scriptsRoot
    $pthPath = Join-Path $scriptsRoot ([string]$manifest.python.pth_filename)
    $pthText = (
        [string]$manifest.python.stdlib_zip + "`r`n" +
        ".`r`n..\Lib\site-packages`r`n..\..\..`r`n"
    )
    [IO.File]::WriteAllText($pthPath, $pthText, [Text.Encoding]::ASCII)

    $python = Join-Path $scriptsRoot "python.exe"
    $wheelInstaller = Join-Path $runtimeStage "tools\install_offline_wheels.py"
    $wheelArguments = @(
        $wheelInstaller,
        "--site-packages", $sitePackages,
        "--scripts", $scriptsRoot
    )
    foreach ($wheel in @($manifest.wheels)) {
        $wheelArguments += Join-Path $PackageRoot ("packages\wheels\" + [string]$wheel.filename)
    }
    $wheelOutput = & $python @wheelArguments
    if ($LASTEXITCODE -ne 0) { throw "Fixed wheel expansion failed with code $LASTEXITCODE." }
    $wheelStatus = ($wheelOutput -join [Environment]::NewLine) | ConvertFrom-Json
    if ($wheelStatus.status -ne "ready") { throw "Fixed wheel expansion was not ready." }

    $stagedPreflight = Invoke-Preflight -RuntimeRoot $runtimeStage
    $runtimeBackup = Activate-ManagedTree `
        -Staged $runtimeStage -Target $runtimeTarget `
        -BackupRoot (Join-Path $appRoot "runtime-backups") -Label "runtime"
    $skillBackup = Activate-ManagedTree `
        -Staged $skillStage -Target $skillTarget `
        -BackupRoot (Join-Path $HOME ".codex\skill-backups") -Label "weixin-replay-to-mp3"
    $preflight = Invoke-Preflight -RuntimeRoot $runtimeTarget
    $launcher = Join-Path $runtimeTarget "weixin-replay-to-mp3.cmd"
    $outputRoot = Join-Path $HOME "Downloads\WeixinReplayMP3"
    [PSCustomObject]@{
        status = "ready"
        version = $PortableVersion
        platform = "Windows"
        architecture = "x64"
        install_mode = "offline_portable"
        runtime_root = $runtimeTarget
        launcher = $launcher
        skill_root = $skillTarget
        mp3_output_root = $outputRoot
        python_version = $preflight.python
        ffmpeg = $preflight.ffmpeg
        yt_dlp_version = $preflight.yt_dlp_version
        javascript_runtime = $preflight.javascript_runtime
        runtime_backup = $runtimeBackup
        skill_backup = $skillBackup
        git_required = $false
        winget_required = $false
        online_pip_required = $false
    } | ConvertTo-Json -Depth 5
    Write-Output "READY: you can send a link now"
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message)
    exit 1
}
