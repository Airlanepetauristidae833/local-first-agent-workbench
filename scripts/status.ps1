[CmdletBinding()]
param([string]$EnvFile = '')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $projectRoot '.env' }
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3 is required.' }
& $python.Source (Join-Path $PSScriptRoot 'status.py') --env-file $EnvFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
