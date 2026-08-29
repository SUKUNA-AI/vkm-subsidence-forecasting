$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python .\scripts\verify_inputs.py --root .
Write-Host "Input verification passed. Open Codex in: $Root"
