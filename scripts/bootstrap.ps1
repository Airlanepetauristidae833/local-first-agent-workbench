[CmdletBinding()]
param(
    [string]$EnvFile = '',
    [string]$StateRoot = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $projectRoot '.env' }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python 3 is required to bootstrap this project.'
}
$arguments = @(
    (Join-Path $PSScriptRoot 'bootstrap.py'),
    '--env-file', $EnvFile
)
if ($StateRoot) { $arguments += @('--state-root', $StateRoot) }
& $python.Source @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host 'Bootstrap complete. Review the environment file and install its configured Ollama model.'
