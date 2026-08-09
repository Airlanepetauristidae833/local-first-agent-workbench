[CmdletBinding()]
param(
    [string]$EnvFile = '',
    [switch]$SkipProviderConfig
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $projectRoot '.env' }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw 'Missing environment file. Run scripts/bootstrap.ps1 first.'
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3 is required to start this project.' }

$settings = @{}
Get-Content -LiteralPath $EnvFile -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*?)\s*$') {
        $settings[$matches[1]] = $matches[2].Trim('"').Trim("'")
    }
}
$startupTimeout = if ($settings.ContainsKey('STARTUP_TIMEOUT_SECONDS')) {
    [int]$settings['STARTUP_TIMEOUT_SECONDS']
}
else { 360 }

Set-Location $projectRoot
docker compose --env-file $EnvFile up -d --build --wait `
    --wait-timeout $startupTimeout api knowledge search open-webui
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipProviderConfig) {
    & $python.Source (Join-Path $PSScriptRoot 'configure_personal_agent.py') `
        --env-file $EnvFile --allow-pending-admin
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python.Source (Join-Path $PSScriptRoot 'status.py') --env-file $EnvFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
