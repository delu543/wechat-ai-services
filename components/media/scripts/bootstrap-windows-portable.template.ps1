[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$LocalArtifact = "",
    [string]$Destination = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Version = "__PORTABLE_VERSION__"
$AssetName = "__PORTABLE_ASSET_NAME__"
$AssetUrl = "__PORTABLE_ASSET_URL__"
$ExpectedBytes = __PORTABLE_ASSET_BYTES__
$ExpectedSha256 = "__PORTABLE_ASSET_SHA256__"
$Failures = New-Object System.Collections.Generic.List[string]

function Test-WindowsHost {
    return [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
}

function Test-VerifiedAsset {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne $ExpectedBytes) { return $false }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() -eq $ExpectedSha256
}

function Add-Failure {
    param([string]$Client, [string]$Message)
    $safe = ($Message -replace '[\r\n]+', ' ').Trim()
    if ($safe.Length -gt 240) { $safe = $safe.Substring(0, 240) }
    $Failures.Add("${Client}: $safe") | Out-Null
}

function New-Candidate {
    param([string]$Suffix)
    return Join-Path ([IO.Path]::GetTempPath()) (
        "weixin-portable-{0}{1}" -f [Guid]::NewGuid().ToString("N"), $Suffix
    )
}

function Get-WithPython {
    param([string]$OutputPath)
    if ([string]::IsNullOrWhiteSpace($PythonExecutable) -or
        -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { return $false }
    $script = New-Candidate -Suffix ".py"
    $code = @'
import shutil, sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "weixin-replay-to-mp3-portable"})
with urllib.request.urlopen(request, timeout=60) as response, open(sys.argv[2], "wb") as target:
    shutil.copyfileobj(response, target, length=1024 * 1024)
'@
    try {
        [IO.File]::WriteAllText($script, $code, [Text.Encoding]::ASCII)
        & $PythonExecutable $script $AssetUrl $OutputPath
        if ($LASTEXITCODE -ne 0) { throw "Python downloader exit code $LASTEXITCODE" }
        return $true
    }
    catch { Add-Failure -Client "bundled_python" -Message $_.Exception.Message; return $false }
}

function Get-WithCurl {
    param([string]$OutputPath)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { return $false }
    try {
        & $curl.Source --fail --location --silent --show-error --retry 2 `
            --retry-all-errors --connect-timeout 10 --max-time 1200 `
            --output $OutputPath $AssetUrl
        if ($LASTEXITCODE -ne 0) { throw "curl.exe exit code $LASTEXITCODE" }
        return $true
    }
    catch { Add-Failure -Client "curl" -Message $_.Exception.Message; return $false }
}

function Get-WithPowerShell {
    param([string]$OutputPath)
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $AssetUrl -OutFile $OutputPath -TimeoutSec 1200
        return $true
    }
    catch { Add-Failure -Client "powershell" -Message $_.Exception.Message; return $false }
}

function Get-WithBits {
    param([string]$OutputPath)
    try {
        Import-Module BitsTransfer -ErrorAction Stop
        Start-BitsTransfer -Source $AssetUrl -Destination $OutputPath -ErrorAction Stop
        return $true
    }
    catch { Add-Failure -Client "bits" -Message $_.Exception.Message; return $false }
}

function Accept-Candidate {
    param([string]$Path, [string]$Client)
    if (-not (Test-VerifiedAsset -Path $Path)) {
        Add-Failure -Client $Client -Message "bytes or SHA-256 did not match the fixed portable asset"
        return $false
    }
    [IO.File]::Copy($Path, $script:ResolvedDestination, $true)
    $script:SelectedClient = $Client
    return $true
}

try {
    if (-not (Test-WindowsHost) -or -not [Environment]::Is64BitOperatingSystem) {
        throw "The portable package requires native 64-bit Windows."
    }
    if ([string]::IsNullOrWhiteSpace($Destination)) {
        $Destination = Join-Path ([IO.Path]::GetTempPath()) $AssetName
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $script:ResolvedDestination = [IO.Path]::GetFullPath($Destination)
    $script:SelectedClient = ""
    $ready = $false

    if (Test-VerifiedAsset -Path $ResolvedDestination) {
        $ready = $true
        $SelectedClient = "verified_existing"
    }
    if (-not $ready -and -not [string]::IsNullOrWhiteSpace($LocalArtifact)) {
        if (Test-VerifiedAsset -Path $LocalArtifact) {
            $ready = Accept-Candidate -Path $LocalArtifact -Client "browser_local_artifact"
        }
        else { Add-Failure -Client "browser_local_artifact" -Message "file did not match fixed metadata" }
    }
    foreach ($client in @("bundled_python", "curl", "powershell", "bits")) {
        if ($ready) { break }
        $candidate = New-Candidate -Suffix ".zip"
        $downloaded = $false
        if ($client -eq "bundled_python") { $downloaded = Get-WithPython -OutputPath $candidate }
        elseif ($client -eq "curl") { $downloaded = Get-WithCurl -OutputPath $candidate }
        elseif ($client -eq "powershell") { $downloaded = Get-WithPowerShell -OutputPath $candidate }
        elseif ($client -eq "bits") { $downloaded = Get-WithBits -OutputPath $candidate }
        if ($downloaded) { $ready = Accept-Candidate -Path $candidate -Client $client }
    }
    if (-not $ready) {
        [PSCustomObject]@{
            status = "asset_acquisition_failed"
            version = $Version
            attempts = @($Failures)
            browser_fallback = "Download the fixed asset from the v$Version release page, then rerun this capsule with -LocalArtifact."
        } | ConvertTo-Json -Depth 5
        exit 2
    }

    $expanded = Join-Path ([IO.Path]::GetTempPath()) (
        "weixin-portable-expanded-" + [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $expanded | Out-Null
    Expand-Archive -LiteralPath $ResolvedDestination -DestinationPath $expanded
    $installers = @(Get-ChildItem -LiteralPath $expanded -Filter "install-offline.ps1" -Recurse)
    if ($installers.Count -ne 1) { throw "The fixed asset did not contain exactly one offline installer." }
    [PSCustomObject]@{
        status = "asset_ready"
        version = $Version
        path = $ResolvedDestination
        bytes = $ExpectedBytes
        sha256 = $ExpectedSha256
        client = $SelectedClient
    } | ConvertTo-Json -Depth 4
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installers[0].FullName
    exit [int]$LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message)
    exit 1
}
