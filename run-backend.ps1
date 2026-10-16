<#
Starts the HRMS backend for local development: services, a target check, migrations, the
audit anchor and files buckets, worker and API (docs/local-development.md, "Windows shortcut").

  .\run-backend.cmd                                   database and buckets from .env
  .\run-backend.cmd -Database NAME -AnchorBucket NAME -FilesBucket NAME
                                                      another existing local database

When CLAMAV_HOST is set in .env, ClamAV (Compose profile `scan`) is started too.

The database and bucket are set explicitly for the migrations, the worker and the API, so
variables left over in the window cannot point them somewhere else. The script never creates
or resets a database, and it stops before migrating if the target is not the one asked for.
#>
param(
  [ValidatePattern('^[a-z][a-z0-9_]{0,62}$')][string]$Database,
  [ValidatePattern('^[a-z0-9][a-z0-9-]{2,62}$')][string]$AnchorBucket,
  [ValidatePattern('^[a-z0-9][a-z0-9-]{2,62}$')][string]$FilesBucket
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
# Documents and exports: optional locally. Each database keeps its files in its own bucket.
$envFilesBucket = $dotenv['FILES_BUCKET']
if (-not $FilesBucket -and $envFilesBucket) {
  if ($Database -ne $envDatabases[0]) {
    Stop-WithMessage "Database $Database needs its own files bucket. Add -FilesBucket NAME (not $envFilesBucket, which belongs to $($envDatabases[0]))."
  }
  $FilesBucket = $envFilesBucket
}
if ($FilesBucket -and $Database -ne $envDatabases[0] -and $FilesBucket -eq $envFilesBucket) {
  Stop-WithMessage "Files bucket $envFilesBucket belongs to $($envDatabases[0]). Give $Database a bucket of its own."
}
if ($FilesBucket -and $FilesBucket -eq $AnchorBucket) { Stop-WithMessage 'The files bucket must differ from the anchor bucket.' }
$scanProfile = @()
if ($dotenv['CLAMAV_HOST']) { $scanProfile = @('--profile', 'scan') }

# Process variables take precedence over --env-file, so these reach every uv command below.
foreach ($name in $urlNames) {
  $null = $dotenv[$name] -match $urlPattern
  Set-Item "env:$name" ($Matches['base'] + $Database + $Matches['rest'])
}
$env:AUDIT_ANCHOR_BUCKET = $AnchorBucket
$checkArguments = @('--database', $Database, '--bucket', $AnchorBucket)
if ($FilesBucket) {
  $env:FILES_BUCKET = $FilesBucket
  $checkArguments += @('--files-bucket', $FilesBucket)
  Write-Host "Target: database $Database, anchor bucket $AnchorBucket, files bucket $FilesBucket"
} else {
  Write-Host "Target: database $Database, anchor bucket $AnchorBucket (no files bucket: documents and exports are off)"
}

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

$servicesTitle = '[1/7] Starting local services...'
if ($scanProfile) { $servicesTitle = '[1/7] Starting local services with ClamAV (its first start downloads virus signatures and can take several minutes)...' }
Invoke-Step $servicesTitle { docker compose --env-file .env -f infra/compose.yaml @scanProfile up -d --wait }
Set-Location -LiteralPath (Join-Path $PSScriptRoot 'backend')
Invoke-Step '[2/7] Installing backend dependencies...' { uv sync }
Invoke-Step "[3/7] Checking the target database..." { uv run --env-file ../.env python -m scripts.check_local_target @checkArguments }
Invoke-Step '[4/7] Applying database migrations...' { uv run --env-file ../.env alembic upgrade head }
Invoke-Step '[5/7] Creating the audit anchor bucket if it does not exist...' { uv run --env-file ../.env python -m scripts.create_local_anchor_bucket }
if ($FilesBucket) {
  Invoke-Step '[6/7] Creating the private files bucket if it does not exist...' { uv run --env-file ../.env python -m scripts.create_local_files_bucket }
} else {
  Write-Host '[6/7] No files bucket set: skipped.'
}

Write-Host "[7/7] Starting the worker in a new window and the API here ($Database)..."
$workerCommand = "`$host.UI.RawUI.WindowTitle = 'HRMS worker ($Database)'; uv run --env-file ../.env python -m app.worker"
Start-Process -FilePath 'powershell.exe' -WorkingDirectory (Get-Location) -ArgumentList '-NoExit', '-NoProfile', '-Command', $workerCommand
$host.UI.RawUI.WindowTitle = "HRMS API ($Database)"
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
exit $LASTEXITCODE
