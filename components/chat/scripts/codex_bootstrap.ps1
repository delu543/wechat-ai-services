param([ValidateSet('doctor','install')][string]$Action = 'doctor')
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
# No policy change, elevation, UI control, account scan or key initialization.
$sourceRoot = Split-Path -Parent $PSScriptRoot
$support = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'WeChatAIServicesChat'
$runtime = Join-Path $support 'tools\python\Scripts\python.exe'
$python = $null
if (Test-Path -LiteralPath $runtime) {
  $python = $runtime
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $python = & py -3.12 -c 'import sys; print(sys.executable)' 2>$null
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $python = & python -c 'import sys; print(sys.executable)' 2>$null
}
if (-not $python -or -not (Test-Path -LiteralPath $python)) {
  Write-Output '{"status":"needs_python","required":"CPython 3.12 x64","next_action":"Ask before installing official Python.Python.3.12; then retry bootstrap."}'
  exit 2
}
& $python -c 'import sys; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32'
if ($LASTEXITCODE -ne 0) {
  Write-Output '{"status":"needs_python","required":"CPython 3.12 x64"}'
  exit 2
}
if ($Action -eq 'install' -and -not (Test-Path -LiteralPath $support)) {
  # Secure only the new managed root. Never replace permissions on a foreign root.
  $directory = [System.IO.Directory]::CreateDirectory($support)
  $acl = New-Object System.Security.AccessControl.DirectorySecurity
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
  $acl.SetOwner($sid)
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($identity in @($sid, (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')))) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $directory.FullName -AclObject $acl
  [System.IO.File]::WriteAllText((Join-Path $support '.windows-bootstrap-owner'), 'wechat-local-export-windows-v1')
}
if ($Action -eq 'install') {
  $cursor = Get-Item -LiteralPath $support -Force
  while ($null -ne $cursor) {
    if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      Write-Output '{"status":"blocked","detail":"Managed path contains a reparse point."}'
      exit 2
    }
    $cursor = $cursor.Parent
  }
  $ownerFile = Join-Path $support '.windows-bootstrap-owner'
  $acl = Get-Acl -LiteralPath $support
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $allowed = @($sid, 'S-1-5-18')
  $actualOwner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  $safe = $actualOwner -eq $sid -and $acl.AreAccessRulesProtected
  foreach ($rule in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
    if ($rule.AccessControlType -ne 'Allow' -or $allowed -notcontains $rule.IdentityReference.Value) { $safe = $false }
  }
  if (-not $safe -or -not (Test-Path -LiteralPath $ownerFile) -or
      [System.IO.File]::ReadAllText($ownerFile) -ne 'wechat-local-export-windows-v1') {
    Write-Output '{"status":"blocked","detail":"Existing managed root is foreign or has unsafe ACLs; unchanged."}'
    exit 2
  }
}
& $python (Join-Path $PSScriptRoot 'windows_bootstrap.py') $Action
exit $LASTEXITCODE
