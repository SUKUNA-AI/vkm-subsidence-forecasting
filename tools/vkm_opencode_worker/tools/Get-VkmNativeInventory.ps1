# Read-only inventory for Codex. Does not install, log in, spend money or expose keys.
[CmdletBinding()]
param([string]$Output = (Join-Path $env:LOCALAPPDATA 'VKM\opencode-worker\setup\inventory.json'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Native Windows PowerShell required.' }
if (Test-Path -LiteralPath $Output) { throw 'Inventory exists; use a new receipt path, never overwrite evidence.' }
$tools = @{}
foreach ($name in @('git.exe','python.exe','py.exe','uv.exe','uvx.exe','opencode.exe','serena.exe','ast-grep.exe','graphify.exe','graphify-mcp.exe','node.exe','npm.cmd','pwsh.exe')) {
    $item = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($item) { $tools[$name] = @{ path = $item.Source; sha256 = (Get-FileHash -LiteralPath $item.Source -Algorithm SHA256).Hash.ToLowerInvariant() } }
    else { $tools[$name] = $null }
}
$configs = @{}
foreach ($path in @((Join-Path $HOME '.codex\config.toml'), (Join-Path $HOME '.config\opencode\opencode.json'), (Join-Path $HOME '.config\opencode\opencode.jsonc'))) {
    if (Test-Path -LiteralPath $path -PathType Leaf) { $configs[$path] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
}
$inventory = @{
    schema='vkm.native.inventory/2'; created_at=[DateTimeOffset]::UtcNow.ToString('o'); platform='win32'
    powershell=$PSVersionTable.PSVersion.ToString(); architecture=$env:PROCESSOR_ARCHITECTURE
    tools=$tools; config_hashes=$configs
    public_root_hint=$env:VKM_PUBLIC_ROOT; resources_root_hint=$env:VKM_RESOURCES_ROOT
    openrouter_key_present=[bool]$env:OPENROUTER_API_KEY
    codex_subscription_modified=$false; inference_performed=$false
}
New-Item -ItemType Directory -Path (Split-Path -Parent $Output) -Force | Out-Null
$data = $inventory | ConvertTo-Json -Depth 8
$bytes = [Text.Encoding]::UTF8.GetBytes($data)
$f = [IO.File]::Open($Output, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $f.Write($bytes, 0, $bytes.Length) } finally { $f.Dispose() }
Write-Host "Inventory receipt: $Output"
