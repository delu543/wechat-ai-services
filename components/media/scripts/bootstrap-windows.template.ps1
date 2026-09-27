[CmdletBinding()]
param(
    [switch]$AcquireOnly,
    [ValidateSet("Auto", "GitHubApi", "GitBlob", "ReleaseAsset", "JsDelivr", "Codeload", "LocalArtifact")]
    [string]$Transport = "Auto",
    [ValidateSet("Auto", "PowerShell", "Curl", "Python", "Bits")]
    [string]$DownloadClient = "Auto",
    [string]$PythonExecutable = "",
    [string]$Destination = "",
    [string]$LocalArtifact = "",
    [string]$GitHubApiUrl = "https://api.github.com/repos/delu543/weixin-replay-to-mp3/contents/install-windows.ps1?ref=v__INSTALLER_VERSION__",
    [string]$GitBlobApiUrl = "https://api.github.com/repos/delu543/weixin-replay-to-mp3/git/blobs/__INSTALLER_GIT_BLOB_SHA__",
    [string]$ReleaseAssetUrl = "https://github.com/delu543/weixin-replay-to-mp3/releases/download/v__INSTALLER_VERSION__/install-windows.ps1",
    [string]$CodeloadUrl = "https://codeload.github.com/delu543/weixin-replay-to-mp3/zip/refs/tags/v__INSTALLER_VERSION__"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$InstallerVersion = "__INSTALLER_VERSION__"
$ExpectedInstallerBytes = __INSTALLER_BYTES__
$ExpectedInstallerSha256 = "__INSTALLER_SHA256__"
$ExpectedInstallerGitBlobSha = "__INSTALLER_GIT_BLOB_SHA__"
$ApiHeaders = @{
    Accept = "application/vnd.github.raw+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "weixin-replay-to-mp3-bootstrap/$InstallerVersion"
}
$JsonApiHeaders = @{
    Accept = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "weixin-replay-to-mp3-bootstrap/$InstallerVersion"
}
$JsDelivrUrls = @(
    "https://cdn.jsdelivr.net/gh/delu543/weixin-replay-to-mp3@v$InstallerVersion/install-windows.ps1",
    "https://fastly.jsdelivr.net/gh/delu543/weixin-replay-to-mp3@v$InstallerVersion/install-windows.ps1",
    "https://gcore.jsdelivr.net/gh/delu543/weixin-replay-to-mp3@v$InstallerVersion/install-windows.ps1"
)
$Failures = New-Object System.Collections.Generic.List[string]

function Test-WindowsHost {
    return [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Test-VerifiedInstaller {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne $ExpectedInstallerBytes) { return $false }
    return (Get-FileSha256 -Path $Path) -eq $ExpectedInstallerSha256
}

function Add-Failure {
    param([string]$Label, [string]$Message)
    $safe = ($Message -replace '[\r\n]+', ' ').Trim()
    if ($safe.Length -gt 240) { $safe = $safe.Substring(0, 240) }
    $Failures.Add("${Label}: $safe") | Out-Null
}

function New-CandidatePath {
    param([string]$Suffix = ".tmp")
    return Join-Path ([System.IO.Path]::GetTempPath()) (
        "weixin-replay-installer-{0}{1}" -f [System.Guid]::NewGuid().ToString("N"), $Suffix
    )
}

function Clear-Candidate {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        [System.IO.File]::Delete($Path)
    }
}

function Get-ClientOrder {
    param([bool]$AllowBits, [bool]$HasHeaders)
    if ($DownloadClient -ne "Auto") {
        if ($DownloadClient -eq "Bits" -and (-not $AllowBits -or $HasHeaders)) { return @() }
        return @($DownloadClient)
    }
    $clients = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) { $clients.Add("Python") }
    $clients.Add("PowerShell")
    if (Get-Command "curl.exe" -ErrorAction SilentlyContinue) { $clients.Add("Curl") }
    return @($clients)
}

function Invoke-UrlDownload {
    param(
        [string]$Url,
        [string]$OutputPath,
        [hashtable]$Headers = @{},
        [bool]$AllowBits = $true,
        [string]$Label = "download"
    )
    $clients = Get-ClientOrder -AllowBits $AllowBits -HasHeaders ($Headers.Count -gt 0)
    foreach ($client in $clients) {
        Clear-Candidate -Path $OutputPath
        try {
            if ($client -eq "PowerShell") {
                Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $Headers -OutFile $OutputPath -TimeoutSec 30
            }
            elseif ($client -eq "Curl") {
                $curl = (Get-Command "curl.exe" -ErrorAction Stop).Source
                $arguments = @("--fail", "--location", "--silent", "--show-error", "--connect-timeout", "8", "--max-time", "45")
                foreach ($key in ($Headers.Keys | Sort-Object)) {
                    $arguments += @("--header", ("{0}: {1}" -f $key, $Headers[$key]))
                }
                $arguments += @("--output", $OutputPath, $Url)
                & $curl @arguments
                if ($LASTEXITCODE -ne 0) { throw "curl.exe exit code $LASTEXITCODE" }
            }
            elseif ($client -eq "Python") {
                if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
                    throw "the explicit Python executable is unavailable"
                }
                $headerJson = $Headers | ConvertTo-Json -Compress
                $headerBase64 = [System.Convert]::ToBase64String(
                    [System.Text.Encoding]::UTF8.GetBytes($headerJson)
                )
                $pythonScript = New-CandidatePath -Suffix ".py"
                $code = @'
import base64, json, shutil, sys, urllib.request
url, output, headers_base64 = sys.argv[1:4]
headers = json.loads(base64.b64decode(headers_base64).decode("utf-8"))
request = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(request, timeout=45) as response, open(output, "wb") as target:
    shutil.copyfileobj(response, target)
'@
                try {
                    [System.IO.File]::WriteAllText($pythonScript, $code, [System.Text.Encoding]::ASCII)
                    & $PythonExecutable $pythonScript $Url $OutputPath $headerBase64
                    if ($LASTEXITCODE -ne 0) { throw "Python downloader exit code $LASTEXITCODE" }
                }
                finally { Clear-Candidate -Path $pythonScript }
            }
            elseif ($client -eq "Bits") {
                Import-Module BitsTransfer -ErrorAction Stop
                Start-BitsTransfer -Source $Url -Destination $OutputPath -ErrorAction Stop
            }
            if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
                throw "client produced no file"
            }
            return $client
        }
        catch {
            Add-Failure -Label "$Label/$client" -Message $_.Exception.Message
        }
    }
    return ""
}

function Copy-VerifiedInstaller {
    param([string]$Candidate, [string]$SourceLabel)
    if (-not (Test-VerifiedInstaller -Path $Candidate)) {
        Add-Failure -Label $SourceLabel -Message "downloaded bytes did not match the fixed installer length and SHA-256"
        return $null
    }
    [System.IO.File]::Copy($Candidate, $script:ResolvedDestination, $false)
    return [PSCustomObject]@{ path = $script:ResolvedDestination; transport = $SourceLabel }
}

function Get-DirectInstaller {
    param([string]$Url, [hashtable]$Headers, [string]$Label)
    $candidate = New-CandidatePath -Suffix ".ps1"
    try {
        $client = Invoke-UrlDownload -Url $Url -OutputPath $candidate -Headers $Headers -Label $Label
        if (-not $client) { return $null }
        return Copy-VerifiedInstaller -Candidate $candidate -SourceLabel "$Label/$client"
    }
    finally { Clear-Candidate -Path $candidate }
}

function Get-GitBlobInstaller {
    $jsonPath = New-CandidatePath -Suffix ".json"
    $candidate = New-CandidatePath -Suffix ".ps1"
    try {
        $client = Invoke-UrlDownload -Url $GitBlobApiUrl -OutputPath $jsonPath -Headers $JsonApiHeaders -AllowBits $false -Label "github_blob"
        if (-not $client) { return $null }
        $payload = Get-Content -LiteralPath $jsonPath -Raw | ConvertFrom-Json
        if ($payload.sha -ne $ExpectedInstallerGitBlobSha -or $payload.encoding -ne "base64" -or
            [long]$payload.size -ne $ExpectedInstallerBytes) {
            throw "Git blob metadata did not match the fixed release"
        }
        [byte[]]$bytes = [System.Convert]::FromBase64String(([string]$payload.content -replace '\s', ''))
        [System.IO.File]::WriteAllBytes($candidate, $bytes)
        return Copy-VerifiedInstaller -Candidate $candidate -SourceLabel "github_blob/$client"
    }
    catch {
        Add-Failure -Label "github_blob" -Message $_.Exception.Message
        return $null
    }
    finally {
        Clear-Candidate -Path $jsonPath
        Clear-Candidate -Path $candidate
    }
}

function Get-InstallerFromArchive {
    param([string]$ArchivePath, [string]$Label)
    $candidate = New-CandidatePath -Suffix ".ps1"
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $matches = @($archive.Entries | Where-Object {
            $_.FullName -match '^[^/]+/install-windows\.ps1$' -and $_.Length -eq $ExpectedInstallerBytes
        })
        if ($matches.Count -ne 1) { throw "archive did not contain exactly one fixed-size root installer" }
        $inputStream = $matches[0].Open()
        $outputStream = [System.IO.File]::Create($candidate)
        try { $inputStream.CopyTo($outputStream) }
        finally { $outputStream.Dispose(); $inputStream.Dispose() }
        return Copy-VerifiedInstaller -Candidate $candidate -SourceLabel $Label
    }
    finally {
        $archive.Dispose()
        Clear-Candidate -Path $candidate
    }
}

function Get-CodeloadInstaller {
    $archivePath = New-CandidatePath -Suffix ".zip"
    try {
        $client = Invoke-UrlDownload -Url $CodeloadUrl -OutputPath $archivePath -Label "codeload"
        if (-not $client) { return $null }
        if ((Get-Item -LiteralPath $archivePath).Length -gt 20000000) {
            throw "codeload archive exceeded the bounded 20 MB limit"
        }
        return Get-InstallerFromArchive -ArchivePath $archivePath -Label "codeload/$client"
    }
    catch {
        Add-Failure -Label "codeload" -Message $_.Exception.Message
        return $null
    }
    finally { Clear-Candidate -Path $archivePath }
}

function Get-LocalArtifactInstaller {
    if ([string]::IsNullOrWhiteSpace($LocalArtifact)) { return $null }
    if (-not (Test-Path -LiteralPath $LocalArtifact -PathType Leaf)) {
        Add-Failure -Label "local_artifact" -Message "the supplied file does not exist"
        return $null
    }
    if (Test-VerifiedInstaller -Path $LocalArtifact) {
        return Copy-VerifiedInstaller -Candidate $LocalArtifact -SourceLabel "local_artifact/installer"
    }
    try { return Get-InstallerFromArchive -ArchivePath $LocalArtifact -Label "local_artifact/archive" }
    catch {
        Add-Failure -Label "local_artifact" -Message $_.Exception.Message
        return $null
    }
}

try {
    if (-not (Test-WindowsHost)) { throw "bootstrap-windows.ps1 must run on native Windows." }
    if ([string]::IsNullOrWhiteSpace($Destination)) {
        $Destination = Join-Path ([System.IO.Path]::GetTempPath()) "weixin-replay-to-mp3-v$InstallerVersion.ps1"
    }
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $script:ResolvedDestination = $Destination
    $result = $null
    if (Test-Path -LiteralPath $ResolvedDestination -PathType Leaf) {
        if (Test-VerifiedInstaller -Path $ResolvedDestination) {
            $result = [PSCustomObject]@{ path = $ResolvedDestination; transport = "verified_existing" }
        }
        else {
            $script:ResolvedDestination = Join-Path $parent (
                "weixin-replay-to-mp3-v{0}-{1}.ps1" -f $InstallerVersion, [System.Guid]::NewGuid().ToString("N")
            )
        }
    }
    if (-not $result) {
        if ($Transport -in @("Auto", "LocalArtifact")) { $result = Get-LocalArtifactInstaller }
        if (-not $result -and $Transport -in @("Auto", "GitHubApi")) {
            $result = Get-DirectInstaller -Url $GitHubApiUrl -Headers $ApiHeaders -Label "github_api"
        }
        if (-not $result -and $Transport -in @("Auto", "GitBlob")) { $result = Get-GitBlobInstaller }
        if (-not $result -and $Transport -in @("Auto", "ReleaseAsset")) {
            $result = Get-DirectInstaller -Url $ReleaseAssetUrl -Headers @{} -Label "release_asset"
        }
        if (-not $result -and $Transport -in @("Auto", "Codeload")) { $result = Get-CodeloadInstaller }
        if (-not $result -and $Transport -in @("Auto", "JsDelivr")) {
            foreach ($url in $JsDelivrUrls) {
                $result = Get-DirectInstaller -Url $url -Headers @{} -Label ([Uri]$url).Host
                if ($result) { break }
            }
        }
    }
    if (-not $result) {
        [PSCustomObject]@{
            status = "acquisition_failed"
            version = $InstallerVersion
            attempts = @($Failures)
            next_action = "Use the Codex bundled Python path, or pass a browser-downloaded tagged ZIP/installer with -LocalArtifact."
        } | ConvertTo-Json -Depth 5
        exit 1
    }
    [PSCustomObject]@{
        status = "acquired"
        version = $InstallerVersion
        path = $result.path
        bytes = $ExpectedInstallerBytes
        sha256 = $ExpectedInstallerSha256
        transport = $result.transport
    } | ConvertTo-Json -Depth 4
    if ($AcquireOnly) { exit 0 }
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $result.path)
    if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $arguments += @("-PythonExecutable", $PythonExecutable)
    }
    & powershell.exe @arguments
    exit [int]$LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message)
    exit 1
}
