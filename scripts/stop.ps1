[CmdletBinding()]
param([string]$EnvFile = '')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $projectRoot '.env' }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw 'Missing environment file.'
}
Set-Location $projectRoot
docker compose --env-file $EnvFile down
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
