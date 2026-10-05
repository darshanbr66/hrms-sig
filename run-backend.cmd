@echo off
rem Starts the HRMS backend for local development: services, migrations, worker and API.
setlocal
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running. Start Docker Desktop, wait until it is ready, then run this again.
  exit /b 1
)

echo [1/4] Starting local services...
docker compose --env-file .env -f infra/compose.yaml up -d --wait
if errorlevel 1 goto :failed

cd backend

echo [2/4] Installing backend dependencies...
uv sync
if errorlevel 1 goto :failed

echo [3/4] Applying database migrations...
uv run --env-file ../.env alembic upgrade head
if errorlevel 1 goto :failed

echo [4/4] Starting the worker in a new window and the API here...
start "HRMS worker" cmd /k uv run --env-file ../.env python -m app.worker
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop
exit /b %errorlevel%

:failed
echo.
echo The step above failed. Read the error message, fix it, then run this again.
exit /b 1
