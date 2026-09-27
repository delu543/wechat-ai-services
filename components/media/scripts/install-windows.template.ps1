[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$SourceRoot = "",
    [string]$PythonExecutable = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$MinimumPython = "3.10"
$PreferredPythonPackage = "Python.Python.3.12"
$EmbeddedSourceVersion = "__EMBEDDED_SOURCE_VERSION__"
$EmbeddedSourceSha256 = "__EMBEDDED_SOURCE_SHA256__"
$EmbeddedSourceBytes = __EMBEDDED_SOURCE_BYTES__
$EmbeddedSourceRootName = "weixin-replay-to-mp3-bundle"
$EmbeddedSourceBase64 = @'
__EMBEDDED_SOURCE_BASE64__
'@

function Test-WindowsHost {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Get-ByteArraySha256 {
    param([byte[]]$Bytes)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-EmbeddedSourceBytes {
    $compact = $EmbeddedSourceBase64 -replace '\s', ''
    try {
        [byte[]]$bytes = [System.Convert]::FromBase64String($compact)
    }
    catch {
        throw "The embedded source package is not valid base64."
    }
    if ($bytes.Length -ne $EmbeddedSourceBytes) {
        throw "The embedded source package length does not match its release metadata."
    }
    $actual = Get-ByteArraySha256 -Bytes $bytes
    if ($actual -ne $EmbeddedSourceSha256) {
        throw "The embedded source package SHA-256 verification failed."
    }
    return ,$bytes
}

function Test-EmbeddedSourceBundle {
    [byte[]]$bytes = Get-EmbeddedSourceBytes
    Add-Type -AssemblyName System.IO.Compression
    $memory = New-Object System.IO.MemoryStream
    $memory.Write($bytes, 0, $bytes.Length)
    $memory.Position = 0
    $archive = New-Object System.IO.Compression.ZipArchive -ArgumentList @(
        $memory,
        [System.IO.Compression.ZipArchiveMode]::Read,
        $false
    )
    try {
        $prefix = $EmbeddedSourceRootName + "/"
        $required = @{}
        foreach ($name in @(
            "AGENTS.md",
            "README.md",
            "VERSION",
            "bundle-manifest.json",
            "requirements-windows.txt",
            "scripts/bootstrap.py",
            "weixin_replay_cli.py",
            "replay_mp3_studio/platform_support.py",
            "portable_skill/weixin-replay-to-mp3/SKILL.md"
        )) {
            $required[$prefix + $name] = $false
        }
        $seen = @{}
        [long]$uncompressedBytes = 0
        foreach ($entry in $archive.Entries) {
            $name = [string]$entry.FullName
            if (-not $name.StartsWith($prefix) -or $name.StartsWith("/") -or
                $name -match '(^|/)\.\.(/|$)' -or $name -match '^[A-Za-z]:') {
                throw "The embedded source package contains an unsafe path: $name"
            }
            if ($seen.ContainsKey($name)) {
                throw "The embedded source package contains a duplicate path: $name"
            }
            $seen[$name] = $true
            $uncompressedBytes += [long]$entry.Length
            if ($required.ContainsKey($name)) {
                $required[$name] = $true
            }
        }
        if ($archive.Entries.Count -gt 1000 -or $uncompressedBytes -gt 20000000) {
            throw "The embedded source package exceeds its bounded release limits."
        }
        foreach ($name in $required.Keys) {
            if (-not $required[$name]) {
                throw "The embedded source package is missing required file: $name"
            }
        }
        $versionEntry = $archive.GetEntry($prefix + "VERSION")
        $reader = New-Object System.IO.StreamReader($versionEntry.Open())
        try {
            $archiveVersion = $reader.ReadToEnd().Trim()
        }
        finally {
            $reader.Dispose()
        }
        if ($archiveVersion -ne $EmbeddedSourceVersion) {
            throw "The embedded source package version does not match the installer."
        }
        return [PSCustomObject]@{
            ready = $true
            version = $archiveVersion
            sha256 = $EmbeddedSourceSha256
            compressed_bytes = $bytes.Length
            uncompressed_bytes = $uncompressedBytes
            entries = $archive.Entries.Count
        }
    }
    finally {
        $archive.Dispose()
        $memory.Dispose()
    }
}

function Test-SourceRoot {
    param([string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root)) {
        return $false
    }
    foreach ($required in @("AGENTS.md", "VERSION", "scripts\bootstrap.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $required) -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

function Find-LocalSourceRoot {
    param([string]$RequestedRoot)
    if ([string]::IsNullOrWhiteSpace($RequestedRoot)) {
        return ""
    }
    if (Test-SourceRoot -Root $RequestedRoot) {
        return (Resolve-Path -LiteralPath $RequestedRoot).Path
    }
    throw "The explicitly requested SourceRoot is not a usable release source."
}

function Get-VerifiedEmbeddedSourceRoot {
    $bundle = Test-EmbeddedSourceBundle
    $localAppData = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        $localAppData = [System.Environment]::GetFolderPath("LocalApplicationData")
    }
    $cache = Join-Path $localAppData "WeixinReplayToMP3\source-cache"
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $shortHash = $bundle.sha256.Substring(0, 12)
    $destination = Join-Path $cache (
        "{0}-{1}-{2}" -f $bundle.version, $shortHash, [System.Guid]::NewGuid().ToString("N")
    )
    $expanded = Join-Path $destination $EmbeddedSourceRootName
    New-Item -ItemType Directory -Path $destination | Out-Null
    $archivePath = Join-Path $destination "verified-source.zip"
    [byte[]]$bytes = Get-EmbeddedSourceBytes
    [System.IO.File]::WriteAllBytes($archivePath, $bytes)
    Expand-Archive -LiteralPath $archivePath -DestinationPath $destination
    if (-not (Test-SourceRoot -Root $expanded)) {
        throw "The verified embedded source package did not expand into a usable source root."
    }
    $actualVersion = (Get-Content -LiteralPath (Join-Path $expanded "VERSION") -Raw).Trim()
    if ($actualVersion -ne $EmbeddedSourceVersion) {
        throw "The expanded embedded source version does not match the installer."
    }
    [System.IO.File]::WriteAllText(
        (Join-Path $destination ".verified-source-sha256"),
        $EmbeddedSourceSha256 + [System.Environment]::NewLine,
        [System.Text.Encoding]::ASCII
    )
    return $expanded
}

function Refresh-ProcessPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (($machinePath, $userPath) | Where-Object { $_ }) -join ";"
}

function Invoke-PythonProbe {
    param([string]$Executable, [string[]]$PrefixArguments = @())
    if ([string]::IsNullOrWhiteSpace($Executable)) {
        return $null
    }
    try {
        $arguments = @($PrefixArguments) + @(
            "-c",
            "import platform,ssl,sys,venv; print(sys.executable + '|' + platform.python_version()); raise SystemExit(0 if sys.version_info >= (3,10) else 3)"
        )
        $probe = & $Executable @arguments 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $probe) {
            return $null
        }
        $parts = ([string]$probe).Trim() -split '\|', 2
        if ($parts.Count -ne 2) {
            return $null
        }
        return [PSCustomObject]@{
            executable = $Executable
            prefix_arguments = @($PrefixArguments)
            resolved_executable = $parts[0]
            version = $parts[1]
        }
    }
    catch {
        return $null
    }
}

function Find-CompatiblePython {
    param([string]$RequestedExecutable = "")
    if (-not [string]::IsNullOrWhiteSpace($RequestedExecutable)) {
        if (-not (Test-Path -LiteralPath $RequestedExecutable -PathType Leaf)) {
            throw "The explicitly requested Python executable does not exist."
        }
        $requested = Invoke-PythonProbe -Executable (Resolve-Path -LiteralPath $RequestedExecutable).Path
        if (-not $requested) {
            throw "The explicitly requested Python executable is not Python $MinimumPython+ with ssl and venv support."
        }
        return $requested
    }
    $localPrograms = Join-Path $env:LOCALAPPDATA "Programs\Python"
    foreach ($suffix in @("314", "313", "312", "311", "310")) {
        $candidate = Join-Path $localPrograms ("Python{0}\python.exe" -f $suffix)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $probe = Invoke-PythonProbe -Executable $candidate
            if ($probe) { return $probe }
        }
    }
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @("-3.14", "-3.13", "-3.12", "-3.11", "-3.10", "-3")) {
            $probe = Invoke-PythonProbe -Executable $launcher.Source -PrefixArguments @($selector)
            if ($probe) { return $probe }
        }
    }
    foreach ($name in @("python.exe", "python3.exe")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            $probe = Invoke-PythonProbe -Executable $command.Source
            if ($probe) { return $probe }
        }
    }
    return $null
}

function Install-CompatiblePython {
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw (
            "Python $MinimumPython or newer is missing and Windows Package Manager (winget) " +
            "is unavailable. Restore Microsoft App Installer, or transfer a Python 3.10+ installer, then rerun."
        )
    }
    $arguments = @(
        "install", "--id", $PreferredPythonPackage, "--exact", "--source", "winget",
        "--accept-package-agreements", "--accept-source-agreements", "--silent", "--scope", "user"
    )
    & $winget.Source @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $PreferredPythonPackage. Exit code: $LASTEXITCODE"
    }
    Refresh-ProcessPath
}

function Invoke-WithPython {
    param([PSCustomObject]$Python, [string[]]$Arguments)
    $allArguments = @($Python.prefix_arguments) + $Arguments
    & $Python.executable @allArguments | ForEach-Object { Write-Host $_ }
    return [int]$LASTEXITCODE
}

try {
    if (-not (Test-WindowsHost)) {
        throw "install-windows.ps1 must run on a native Windows host."
    }
    $bundle = Test-EmbeddedSourceBundle
    $python = Find-CompatiblePython -RequestedExecutable $PythonExecutable
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    $git = Get-Command "git.exe" -ErrorAction SilentlyContinue
    $localSource = Find-LocalSourceRoot -RequestedRoot $SourceRoot

    if ($CheckOnly) {
        [PSCustomObject]@{
            status = "checked"
            platform = "Windows"
            python_ready = [bool]$python
            python_version = $(if ($python) { $python.version } else { "" })
            python_strategy = $(if ($PythonExecutable) { "explicit_codex_or_user_runtime" } elseif ($python) { "discovered" } else { "missing" })
            winget_ready = [bool]$winget
            git_available = [bool]$git
            git_required = $false
            github_source_required = $false
            ffmpeg_installed_by_private_runtime = $true
            local_source_ready = -not [string]::IsNullOrWhiteSpace($localSource)
            source_strategy = $(if ($localSource) { "local_repository" } else { "embedded_verified_bundle" })
            embedded_source_ready = $bundle.ready
            embedded_source_version = $bundle.version
            embedded_source_sha256 = $bundle.sha256
            embedded_source_bytes = $bundle.compressed_bytes
        } | ConvertTo-Json -Depth 4
        exit 0
    }

    if (-not $python) {
        Write-Host "Python $MinimumPython+ is missing; installing a current user-local Python runtime."
        Install-CompatiblePython
        $python = Find-CompatiblePython
        if (-not $python) {
            throw "Python installation completed but no compatible interpreter was discovered."
        }
    }

    $resolvedSource = $localSource
    $sourceStrategy = "local_repository"
    if ([string]::IsNullOrWhiteSpace($resolvedSource)) {
        Write-Host "Git and GitHub source downloads are not required; expanding the verified embedded source."
        $resolvedSource = Get-VerifiedEmbeddedSourceRoot
        $sourceStrategy = "embedded_verified_bundle"
    }

    $bootstrap = Join-Path $resolvedSource "scripts\bootstrap.py"
    $installExit = Invoke-WithPython -Python $python -Arguments @($bootstrap, "install")
    if ($installExit -ne 0) {
        throw "Private runtime installation failed with exit code $installExit."
    }
    $doctorExit = Invoke-WithPython -Python $python -Arguments @($bootstrap, "doctor")
    if ($doctorExit -ne 0) {
        throw "Post-install readiness check failed with exit code $doctorExit."
    }

    [PSCustomObject]@{
        status = "ready"
        platform = "Windows"
        version = $EmbeddedSourceVersion
        python_version = $python.version
        source_strategy = $sourceStrategy
        git_required = $false
        github_source_required = $false
        ffmpeg_strategy = "pinned_private_runtime"
    } | ConvertTo-Json -Depth 4
    Write-Output "READY: you can send a link now"
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message)
    exit 1
}
