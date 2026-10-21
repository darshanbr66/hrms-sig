<#
Starts the HRMS backend for local development: local MongoDB (a one-member replica set) and
Mailpit, a target check, migrations, then the API with the scheduled jobs running inside it,
as on Render (docs/local-development.md, "Windows shortcut"; ADR-030).

  .\run-backend.cmd                          database hrms_local on the local MongoDB
  .\run-backend.cmd -Database NAME           another local database
  .\run-backend.cmd -Database NAME -Atlas    a database of its own on the cluster in MONGODB_URI
                                             (never hrms, the production database)

When CLAMAV_HOST is set in .env, ClamAV (Compose profile `scan`) is started too.

The database is set explicitly for the migrations and the API, so variables left over in the
window cannot point them somewhere else. The script never drops or resets a database, and it
stops before migrating if the target is not the one asked for.
#>
param(
  [ValidatePattern('^[a-z][a-z0-9_]{0,62}$')][string]$Database = 'hrms_local',
  [switch]$Atlas
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$LocalMongoUri = 'mongodb://127.0.0.1:27017/?replicaSet=rs0&directConnection=true'

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

if ($Database -eq 'hrms') { Stop-WithMessage 'hrms is the production database. Use another name, such as hrms_local.' }
if (-not (Test-Path -LiteralPath '.env')) { Stop-WithMessage '.env is missing. Create it from .env.example as described in docs/local-development.md section 2.' }
$dotenv = @{}
foreach ($line in Get-Content -LiteralPath '.env') {
  if ($line -match '^\s*([A-Z][A-Z0-9_]*)=(.*)$') { $dotenv[$Matches[1]] = $Matches[2] }
}
$services = @('mongo', 'mailpit')
$scanProfile = @()
if ($dotenv['CLAMAV_HOST']) {
  $scanProfile = @('--profile', 'scan')
  $services += 'clamav'
}

$apiListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($apiListener) {
  Stop-WithMessage "Port 8000 is in use (process $($apiListener[0].OwningProcess)), probably an API that is already running. Stop it with Ctrl+C in its window, then run this again."
}
$workers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match '\s-m\s+app\.worker\b' })
if ($workers.Count -gt 0) {
  Stop-WithMessage "A separate worker is already running (process $($workers[0].ProcessId)). Close its window: the API runs the jobs itself."
}

# Process variables take precedence over --env-file, so these reach every uv command below.
$env:MONGODB_DATABASE = $Database
$env:RUN_JOBS_IN_API = 'true'
$checkArguments = @('--database', $Database)
if ($Atlas) {
  if (-not $dotenv['MONGODB_URI']) { Stop-WithMessage 'MONGODB_URI is not set in .env.' }
  Remove-Item Env:MONGODB_URI -ErrorAction SilentlyContinue
  $checkArguments += '--allow-remote'
  Write-Host "Target: database $Database on the cluster in MONGODB_URI (.env)"
} else {
  $env:MONGODB_URI = $LocalMongoUri
  Write-Host "Target: database $Database on the local MongoDB (127.0.0.1:27017)"
}

# Windows PowerShell 5.1 turns redirected native stderr into an error under 'Stop'.
$ErrorActionPreference = 'Continue'
docker info *> $null
$dockerExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($dockerExit -ne 0) { Stop-WithMessage 'Docker is not running. Start Docker Desktop, wait until it is ready, then run this again.' }

$servicesTitle = '[1/5] Starting local services (MongoDB, Mailpit)...'
if ($scanProfile) { $servicesTitle = '[1/5] Starting local services with ClamAV (its first start downloads virus signatures and can take several minutes)...' }
Invoke-Step $servicesTitle { docker compose -f infra/compose.yaml @scanProfile up -d --wait @services }
Set-Location -LiteralPath (Join-Path $PSScriptRoot 'backend')
Invoke-Step '[2/5] Installing backend dependencies...' { uv sync }
Invoke-Step '[3/5] Checking the target database...' { uv run --env-file ../.env python -m scripts.check_local_target @checkArguments }
Invoke-Step '[4/5] Creating collections and indexes, applying migrations...' { uv run --env-file ../.env python -m app.cli migrate --confirm-database $Database }

Write-Host "[5/5] Starting the API with the scheduled jobs ($Database)..."
Write-Host "      First run on a new database: in another window, from backend\, create the organization and its two super admins:"
Write-Host "        `$env:MONGODB_DATABASE='$Database'; `$env:MONGODB_URI='$(if ($Atlas) { '<from .env>' } else { $LocalMongoUri })'"
Write-Host "        uv run --env-file ../.env python -m app.cli create-organization --name 'Local test company' --slug local-test --confirm-database $Database"
Write-Host "        uv run --env-file ../.env python -m app.cli bootstrap-super-admins --organization local-test --email A --email B --confirm-database $Database"
$host.UI.RawUI.WindowTitle = "HRMS API ($Database)"
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
exit $LASTEXITCODE
