# Codex invokes this; normal worker runs are headless. Preserves the parent env.
[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$Python = 'python',
    [string]$CredentialEnvFile = (Join-Path $env:USERPROFILE '.vkm\openrouter\vkm.env'),
    [string]$CredentialPath = (Join-Path $env:LOCALAPPDATA 'VKM\opencode-worker\credentials\openrouter.dpapi'),
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$WorkerArguments
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use native Windows, not WSL.' }
$old = $env:OPENROUTER_API_KEY
$secure = $null; $pointer = [IntPtr]::Zero; $exitCode = 2
try {
    $needsCredential = $WorkerArguments.Count -gt 0 -and $WorkerArguments[0] -in @('run','reconcile')
    if (-not $old -and $needsCredential) {
        if (Test-Path -LiteralPath $CredentialEnvFile) {
            # Data, never dot-source/evaluate a .env file. Do not print lines.
            $keyLines = @([IO.File]::ReadAllLines($CredentialEnvFile) | Where-Object { $_ -match '^OPENROUTER_API_KEY=' })
            if ($keyLines.Count -ne 1) { throw 'Existing credential file must contain one exact key assignment.' }
            $env:OPENROUTER_API_KEY = $keyLines[0].Substring('OPENROUTER_API_KEY='.Length).Trim().Trim('"').Trim("'")
            $keyLines = $null
        } else {
            if (-not (Test-Path -LiteralPath $CredentialPath)) { throw 'Codex must open the one-time authorization flow.' }
            $secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($CredentialPath))
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $env:OPENROUTER_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        }
    }
    $launcher = Join-Path $PSScriptRoot 'vkm_opencode_worker.py'
    & $Python -B -X utf8 $launcher @WorkerArguments
    $exitCode = $LASTEXITCODE
} finally {
    $env:OPENROUTER_API_KEY = $old
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($secure) { $secure.Dispose() }
}
exit $exitCode
