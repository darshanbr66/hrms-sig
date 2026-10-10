# Docker
"/c/Program Files/Docker/Docker/Docker Desktop.exe" &

# backend
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop