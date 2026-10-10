@echo off
rem Starts the HRMS backend for local development: services, migrations, the audit anchor
rem bucket, worker and API.
setlocal
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running. Start Docker Desktop, wait until it is ready, then run this again.
  exit /b 1
)

echo [1/5] Starting local services...
docker compose --env-file .env -f infra/compose.yaml up -d --wait
if errorlevel 1 goto :failed

cd backend

echo [2/5] Installing backend dependencies...
uv sync
if errorlevel 1 goto :failed

echo [3/5] Applying database migrations...
uv run --env-file ../.env alembic upgrade head
if errorlevel 1 goto :failed

echo [4/5] Creating the audit anchor bucket if it does not exist...
uv run --env-file ../.env python -m scripts.create_local_anchor_bucket
if errorlevel 1 goto :failed

echo [5/5] Starting the worker in a new window and the API here...
start "HRMS worker" cmd /k uv run --env-file ../.env python -m app.worker
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
exit /b %errorlevel%

:failed
echo.
echo The step above failed. Read the error message, fix it, then run this again.
exit /b 1
