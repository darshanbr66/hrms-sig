<#
Starts the HRMS backend for local development: services, a target check, migrations, the
audit anchor bucket, worker and API (docs/local-development.md, "Windows shortcut").

  .\run-backend.cmd                                   database and bucket from .env
  .\run-backend.cmd -Database NAME -AnchorBucket NAME another existing local database

The database and bucket are set explicitly for the migrations, the worker and the API, so
variables left over in the window cannot point them somewhere else. The script never creates
or resets a database, and it stops before migrating if the target is not the one asked for.
#>
param(
  [ValidatePattern('^[a-z][a-z0-9_]{0,62}$')][string]$Database,
  [ValidatePattern('^[a-z0-9][a-z0-9-]{2,62}$')][string]$AnchorBucket
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Stop-WithMessage([string]$Message) {
  Write-Host ''
  Write-Host $Message -ForegroundColor Red
  exit 1
}

function Invoke-Step([string]$Title, [scriptblock]$Command) {
  Write-Host $Title
  & $Command
  if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'The step above failed. Read the error message, fix it, then run this again.' }
}

if (-not (Test-Path -LiteralPath '.env')) { Stop-WithMessage '.env is missing. Create it as described in docs/local-development.md section 2.' }
$dotenv = @{}
foreach ($line in Get-Content -LiteralPath '.env') {
  if ($line -match '^\s*([A-Z][A-Z0-9_]*)=(.*)$') { $dotenv[$Matches[1]] = $Matches[2] }
}

$urlNames = 'DATABASE_URL_APP', 'DATABASE_URL_WORKER', 'DATABASE_URL_MIGRATOR', 'DATABASE_URL_AUDIT_RETENTION'
$urlPattern = '^(?<base>postgres(ql)?://[^/]+/)(?<db>[^/?#]+)(?<rest>\?[^#]*)?$'
$envDatabases = @()
foreach ($name in $urlNames) {
  if (-not ($dotenv[$name] -match $urlPattern)) { Stop-WithMessage "$name in .env is missing or is not a postgresql:// URL with a database name." }
  $envDatabases += $Matches['db']
}
$envDatabases = @($envDatabases | Sort-Object -Unique)
if ($envDatabases.Count -ne 1) { Stop-WithMessage 'The DATABASE_URL_* settings in .env name different databases. Make them name the same one.' }
$envBucket = $dotenv['AUDIT_ANCHOR_BUCKET']

if (-not $Database) { $Database = $envDatabases[0] }
if (-not $AnchorBucket) {
  if ($Database -ne $envDatabases[0]) {
    Stop-WithMessage "Database $Database needs its own anchor bucket. Add -AnchorBucket NAME (not $envBucket, which belongs to $($envDatabases[0]))."
  }
  $AnchorBucket = $envBucket
}
if ($Database -ne $envDatabases[0] -and $AnchorBucket -eq $envBucket) {
  Stop-WithMessage "Anchor bucket $envBucket belongs to $($envDatabases[0]). Give $Database a bucket of its own."
}

# Process variables take precedence over --env-file, so these reach every uv command below.
foreach ($name in $urlNames) {
  $null = $dotenv[$name] -match $urlPattern
  Set-Item "env:$name" ($Matches['base'] + $Database + $Matches['rest'])
}
$env:AUDIT_ANCHOR_BUCKET = $AnchorBucket

Write-Host "Target: database $Database, anchor bucket $AnchorBucket"

# Windows PowerShell 5.1 turns redirected native stderr into an error under 'Stop'.
$ErrorActionPreference = 'Continue'
docker info *> $null
$dockerExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($dockerExit -ne 0) { Stop-WithMessage 'Docker is not running. Start Docker Desktop, wait until it is ready, then run this again.' }

$apiListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($apiListener) {
  Stop-WithMessage "Port 8000 is in use (process $($apiListener[0].OwningProcess)), probably an API that is already running. Stop it with Ctrl+C in its window, then run this again."
}
$workers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match '\s-m\s+app\.worker\b' })
if ($workers.Count -gt 0) {
  Stop-WithMessage "A worker is already running (process $($workers[0].ProcessId)). Close its window, then run this again."
}

Invoke-Step '[1/6] Starting local services...' { docker compose --env-file .env -f infra/compose.yaml up -d --wait }
Set-Location -LiteralPath (Join-Path $PSScriptRoot 'backend')
Invoke-Step '[2/6] Installing backend dependencies...' { uv sync }
Invoke-Step "[3/6] Checking the target database..." { uv run --env-file ../.env python -m scripts.check_local_target --database $Database --bucket $AnchorBucket }
Invoke-Step '[4/6] Applying database migrations...' { uv run --env-file ../.env alembic upgrade head }
Invoke-Step '[5/6] Creating the audit anchor bucket if it does not exist...' { uv run --env-file ../.env python -m scripts.create_local_anchor_bucket }

Write-Host "[6/6] Starting the worker in a new window and the API here ($Database)..."
$workerCommand = "`$host.UI.RawUI.WindowTitle = 'HRMS worker ($Database)'; uv run --env-file ../.env python -m app.worker"
Start-Process -FilePath 'powershell.exe' -WorkingDirectory (Get-Location) -ArgumentList '-NoExit', '-NoProfile', '-Command', $workerCommand
$host.UI.RawUI.WindowTitle = "HRMS API ($Database)"
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
exit $LASTEXITCODE
