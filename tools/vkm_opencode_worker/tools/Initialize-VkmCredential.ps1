# Run by Codex in a visible, one-time AUTHORIZATION terminal; no transcript.
# Uses Windows DPAPI for the current Windows account. No key in argv/chat/Git.
[CmdletBinding()]
param([string]$CredentialPath = (Join-Path $env:LOCALAPPDATA 'VKM\opencode-worker\credentials\openrouter.dpapi'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Native Windows is required.' }
if (Test-Path -LiteralPath $CredentialPath) { throw 'Existing credential is preserved. Reuse it; do not rotate/reset an authorized key.' }
$directory = Split-Path -Parent $CredentialPath
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$propagation = [Security.AccessControl.PropagationFlags]::None
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', $inherit, $propagation, 'Allow')))
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule('SYSTEM', 'FullControl', $inherit, $propagation, 'Allow')))
Set-Acl -LiteralPath $directory -AclObject $acl
$key = Read-Host 'Authorize the existing limited OpenRouter key (input hidden)' -AsSecureString
if ($key.Length -lt 10) { throw 'Credential was not supplied.' }
try {
    $encrypted = ConvertFrom-SecureString -SecureString $key
    $bytes = [Text.Encoding]::UTF8.GetBytes($encrypted)
    $file = [IO.File]::Open($CredentialPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $file.Write($bytes, 0, $bytes.Length) } finally { $file.Dispose() }
    Write-Host 'Authorization stored for this Windows account. The key was not displayed.'
} finally { $key.Dispose(); $encrypted = $null; $bytes = $null }
