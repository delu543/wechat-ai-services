param([Parameter(Mandatory=$true)][string]$Executable)
# A fixed script with a literal argument; never evaluate text supplied by a chat.
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $Executable
$version = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Executable)
$allowed = @(
  'Tencent Technology(Shenzhen) Company Limited',
  'Tencent Technology (Shenzhen) Company Limited',
  'Tencent Technology(Shenzhen)Company Limited',
  'Tencent Technology (Shenzhen) Co., Ltd.'
)
$valid = $signature.Status -eq 'Valid' -and $null -ne $signature.SignerCertificate
$organization = ''
if ($valid) {
  $subject = $signature.SignerCertificate.Subject
  if ($subject -match '(?:^|,\s*)O="([^"]+)"') { $organization = $Matches[1] }
  elseif ($subject -match '(?:^|,\s*)O=([^,]+)') { $organization = $Matches[1] }
}
$valid = $valid -and ($allowed -contains $organization) -and $version.FileMajorPart -eq 4
@{ valid = [bool]$valid; major = $version.FileMajorPart; version = $version.FileVersion } |
  ConvertTo-Json -Compress
