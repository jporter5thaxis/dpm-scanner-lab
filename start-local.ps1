$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
  Write-Error 'Set up the isolated Python environment first. See README.md.'
}
& '.\.venv\Scripts\python.exe' scripts/run.py
