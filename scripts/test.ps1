[CmdletBinding()]
param([string]$EnvFile = '')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $projectRoot '.env' }
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw 'Missing environment file. Run bootstrap first.'
}
Set-Location $projectRoot

docker compose --env-file $EnvFile config --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$parseFailures = @()
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'scripts') -Filter '*.ps1' |
    ForEach-Object {
        $tokens = $null
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $_.FullName,
            [ref]$tokens,
            [ref]$errors
        )
        if ($errors.Count -gt 0) {
            $parseFailures += $errors | ForEach-Object {
                "$($_.Extent.File):$($_.Extent.StartLineNumber): $($_.Message)"
            }
        }
    }
if ($parseFailures.Count -gt 0) { throw ($parseFailures -join [Environment]::NewLine) }

python scripts/validate_sources.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker compose --env-file $EnvFile --profile test run --build --rm api-tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
docker compose --env-file $EnvFile --profile test run --rm api-tests `
    ruff check --target-version py312 --select E4,E7,E9,F,I app tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
docker compose --env-file $EnvFile --profile test run --build --rm knowledge-tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python scripts/privacy_scan.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ((Get-Command git -ErrorAction SilentlyContinue) -and
    (Test-Path -LiteralPath (Join-Path $projectRoot '.git'))) {
    git diff --check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
